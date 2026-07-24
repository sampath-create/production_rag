import logfire
from langchain_groq import ChatGroq
from nemoguardrails import RailsConfig, LLMRails

from app.config import settings
from app.guardrails.colang_rules import COLANG_CONTENT, YAML_CONTENT, RAIL_INDICATORS


_rails: LLMRails | None = None
_guard_model: ChatGroq | None = None

# Off-topic refusal message — must match RAIL_INDICATORS[0]
_OFF_TOPIC_MSG = (
    "I'm a Tumor & Oncology RAG Assistant focused exclusively on cancer and oncology topics. "
    "I can't help with that — but please ask me any tumor biology, oncology treatment, or cancer-related questions!"
)


def initialize_rails() -> None:
    """
    Build the NeMo LLMRails singleton and the Llama Guard 3 classifier at startup.

    Architecture:
      Layer 1 — NeMo Colang (llama-3.1-8b-instant): fast similarity matching
                for greetings, farewells, jailbreaks, and known off-topic examples.
      Layer 2 — Llama Guard 3 (meta-llama/llama-guard-3-8b on Groq): fine-tuned
                safety classifier that gives a structured safe/unsafe verdict for
                anything Layer 1 misses (e.g. "who was Mahatma Gandhi").
    """
    global _rails, _guard_model

    guard_llm = ChatGroq(
        api_key=settings.GROQ_API_KEY,
        model="llama-3.1-8b-instant",
        temperature=0
    )

    config = RailsConfig.from_content(
        colang_content=COLANG_CONTENT,
        yaml_content=YAML_CONTENT
    )
    _rails = LLMRails(config, llm=guard_llm)

    # Llama Guard 3 — purpose-built safety classifier by Meta, hosted on Groq.
    # Unlike a general model, it was fine-tuned exclusively to classify content
    # as safe/unsafe against policy categories. No prompt tricks needed.
    _guard_model = ChatGroq(
        api_key=settings.GROQ_API_KEY,
        model="meta-llama/llama-guard-3-8b",
        temperature=0,
        max_tokens=20,
    )

    logfire.info("🛡️ NeMo Guardrails + Llama Guard 3 initialised.")


def _is_on_topic(message: str) -> bool:
    """
    Uses Llama Guard 3 to classify whether the message is on-topic for an
    oncology RAG system.

    Llama Guard 3 always responds with:
      "safe"           → the input is acceptable (on-topic for oncology)
      "unsafe\nS<N>"  → the input violates a policy (off-topic)

    We define one custom policy category:
      S1 = Off-topic (not about cancer, tumors, or oncology)

    Returns True if the message IS about oncology (safe to proceed).
    Returns False if the message is off-topic (should be blocked).
    """
    if _guard_model is None:
        return True  # fail open if not initialised
    try:
        messages = [
            {
                "role": "user",
                "content": (
                    "Task: Classify if the following message is safe (on-topic) or unsafe "
                    "(off-topic) for a Tumor & Oncology medical RAG assistant.\n\n"
                    "Policy:\n"
                    "S1: Off-topic — the message is NOT about cancer, tumors, or oncology.\n"
                    "    Off-topic includes: history, biography, sports, entertainment, cooking,\n"
                    "    programming, geography, finance, general science, politics.\n\n"
                    "Oncology topics (safe): tumor biology, cancer types, chemotherapy, immunotherapy,\n"
                    "    targeted therapy, radiation, staging (TNM), cancer genetics, clinical trials,\n"
                    "    PET-CT, MRI, biopsy, BI-RADS, oncogenes, tumor suppressors, angiogenesis.\n\n"
                    "Respond ONLY with 'safe' if on-topic, or 'unsafe' if off-topic.\n\n"
                    f"Message: {message}"
                )
            }
        ]
        result = _guard_model.invoke(messages)
        verdict = result.content.strip().lower()
        logfire.info(f"🛡️ [Llama Guard 3] verdict='{verdict}' | query='{message[:60]}'")
        return verdict.startswith("safe")
    except Exception as e:
        logfire.warning(f"⚠️ Llama Guard 3 error — failing open: {e}")
        return True  # fail open so the app stays available on transient errors


def guard(message: str) -> tuple[bool, str | None]:
    """
    Two-layer guardrails gate.

    Layer 1 — NeMo Colang:
      Catches greetings, farewells, jailbreaks, and explicit off-topic examples
      using similarity matching against the Colang intent definitions.

    Layer 2 — Llama Guard 3 (meta-llama/llama-guard-3-8b):
      Fine-tuned safety classifier. Gives a hard safe/unsafe verdict for anything
      NeMo's similarity matching misses (e.g. unseen off-topic queries like
      "who was Mahatma Gandhi" or "tell me about the Roman Empire").

    Returns:
        (True,  rail_response) — a rail fired; skip the RAG pipeline entirely.
        (False, None)          — message is clean; proceed to LangGraph.
    """
    if _rails is None:
        logfire.warning("⚠️ Guardrails not initialised — skipping gate.")
        return False, None

    with logfire.span("🛡️ Guardrails Check"):
        # ── Layer 1: NeMo Colang ────────────────────────────────────────────
        result = _rails.generate(messages=[{"role": "user", "content": message}])
        content = result.get("content", "") if isinstance(result, dict) else str(result)

        fired = any(indicator in content for indicator in RAIL_INDICATORS)
        if fired:
            logfire.info(f"🛡️ [L1-NeMo] Guardrails fired | query='{message[:80]}'")
            return True, content

        # ── Layer 2: Llama Guard 3 ──────────────────────────────────────────
        if not _is_on_topic(message):
            logfire.info(f"🛡️ [L2-LlamaGuard3] Off-topic blocked | query='{message[:80]}'")
            return True, _OFF_TOPIC_MSG

        logfire.info("✅ Guardrails passed.")
        return False, None