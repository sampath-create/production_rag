import time
import random
import logfire
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from app.config import settings

BATCH_SIZE = 50
_gemini_dim = 3072
_FALLBACK_DIM = 768  # all-mpnet-base-v2

_active_model = None
_model_type: str | None = None  # "gemini" or "fallback"


# ── Model initialisation ───────────────────────────────────────────────────────

def _probe_gemini():
    """Try one embed call to verify Gemini is reachable. Returns model or None."""
    global _gemini_dim
    try:
        model_name = getattr(settings, "GEMINI_EMBEDDING_MODEL", "models/gemini-embedding-2-preview")
        model = GoogleGenerativeAIEmbeddings(
            model=model_name,
            google_api_key=settings.GEMINI_API_KEY,
        )
        # Probe the API to verify connection and dynamically detect dimension size
        probe_vector = model.embed_query("probe")
        _gemini_dim = len(probe_vector)
        logfire.info(f"Gemini embeddings ready ({model_name}, {_gemini_dim}-dim).")
        return model
    except Exception as e:
        logfire.warning(f"Gemini probe failed: {e}. Will use sentence-transformers fallback.")
        return None


def _load_fallback():
    from sentence_transformers import SentenceTransformer
    logfire.info("Loading sentence-transformers fallback (all-mpnet-base-v2, 768-dim).")
    return SentenceTransformer("all-mpnet-base-v2")


def _init():
    """Initialise embedding model once per process. Called lazily on first use."""
    global _active_model, _model_type
    if _active_model is not None:
        return

    provider = getattr(settings, "EMBEDDING_PROVIDER", "gemini").lower()
    if provider in ("local", "fallback"):
        _active_model = _load_fallback()
        _model_type = "fallback"
        return

    gemini = _probe_gemini()
    if gemini:
        _active_model = gemini
        _model_type = "gemini"
    else:
        _active_model = _load_fallback()
        _model_type = "fallback"


# ── Public helpers ─────────────────────────────────────────────────────────────

def get_embedding_dim() -> int:
    """Return the vector dimension for the active model. Call after _init()."""
    _init()
    return _gemini_dim if _model_type == "gemini" else _FALLBACK_DIM


# ── Batch embedding with retry ─────────────────────────────────────────────────

def _embed_batch(batch: list[str]) -> list[list[float]]:
    if _model_type == "gemini":
        # Exponential backoff with jitter: retry up to 6 times
        # wait starts at 2s and increases: 2s -> 4s -> 8s -> 16s -> 32s (with random jitter)
        for attempt in range(6):
            try:
                # Add a small delay between consecutive batch requests to stay under free tier RPM
                if attempt == 0:
                    time.sleep(1.0)
                return _active_model.embed_documents(batch)
            except Exception as e:
                err = str(e).lower()
                is_rate_limit = any(x in err for x in ("429", "rate", "quota", "resource_exhausted"))
                if is_rate_limit and attempt < 5:
                    wait = (2 ** (attempt + 1)) + random.uniform(0.5, 1.5)
                    logfire.warning(
                        f"Gemini rate limit hit — retrying in {wait:.2f}s "
                        f"(attempt {attempt + 1}/6)."
                    )
                    time.sleep(wait)
                else:
                    logfire.error(f"Gemini embedding failed: {e}")
                    raise
        raise RuntimeError("Gemini rate limit persisted after 6 attempts.")
    else:
        return _active_model.encode(batch, show_progress_bar=False).tolist()


# ── Public API (same signatures as before) ─────────────────────────────────────

def embed_query(query: str) -> list[float]:
    _init()
    if _model_type == "gemini":
        return _active_model.embed_query(query)
    return _active_model.encode([query])[0].tolist()


def embed_texts(texts: list[str]) -> list[list[float]]:
    _init()
    all_embeddings: list[list[float]] = []
    for i in range(0, len(texts), BATCH_SIZE):
        batch = texts[i : i + BATCH_SIZE]
        with logfire.span("Embed batch", model=_model_type, start=i, size=len(batch)):
            all_embeddings.extend(_embed_batch(batch))
    return all_embeddings