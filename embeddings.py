"""
Local embedding client for the semantic cache (see semantic_match.py and
query_cache.py). Uses the same local Ollama daemon app.py already depends
on for SQL generation -- no external API, no new network dependency.

Requires an embedding-capable model to be available on the Ollama daemon
(e.g. `ollama pull nomic-embed-text`). Configurable via EMBEDDING_MODEL so
it can be swapped without code changes.

This call is NOT required for the app to function: every caller treats a
None return as "embeddings unavailable right now" and falls back to
exact-match-only caching. That fallback is deliberate -- a demo/dev Ollama
instance may not have an embedding model pulled, or may be a build that
doesn't expose the embeddings endpoint at all, and the cache must not
regress into an error just because a nice-to-have is missing.
"""

import logging
import os
from openai import OpenAI

logger = logging.getLogger(__name__)

EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "nomic-embed-text")

# Same override as app.py's client -- see that module for why "localhost"
# isn't safe to hardcode once this runs inside a container.
_client = OpenAI(
    base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
    api_key="local-bypass",
)

# Logged at most once per process so a missing embedding model doesn't
# spam the console on every single cache-miss request.
_warned_unavailable = False


def get_embedding(text):
    """
    Returns a list[float] embedding for `text`, or None if embeddings
    are unavailable (model not pulled, daemon doesn't support the
    endpoint, connection error, etc.). Callers must treat None as
    "skip semantic matching for this request."
    """
    global _warned_unavailable
    try:
        response = _client.embeddings.create(model=EMBEDDING_MODEL, input=text)
        return response.data[0].embedding
    except Exception as e:
        if not _warned_unavailable:
            logger.warning(
                "Semantic cache disabled -- embedding call failed; falling back to "
                "exact-match caching only. To enable semantic matching, run: "
                f"ollama pull {EMBEDDING_MODEL}",
                extra={"embedding_model": EMBEDDING_MODEL, "error": str(e)},
            )
            _warned_unavailable = True
        return None
