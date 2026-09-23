"""Turn story text into vectors via the OpenAI embeddings API."""
from __future__ import annotations

import hashlib
import logging
import time
from typing import Sequence

from openai import OpenAI

log = logging.getLogger(__name__)

# The API accepts larger batches, but 128 keeps each request well inside the
# per-request token ceiling for headline+tease-sized inputs.
BATCH_SIZE = 128
MAX_RETRIES = 5


def build_embed_input(headline: str, tease: str, kicker: str = "") -> str:
    """The exact text handed to the embedding model.

    Headline plus tease, per the agreed scope. Kicker is included when
    present because it is usually the beat name ("RAZORBACKS", "LEGISLATURE")
    and is a cheap, high-signal topic token.

    Deliberately a single function: if the backtest later says to add the
    lead paragraphs or a generated summary, this is the only place to change,
    and every affected row re-embeds automatically on the next run via the
    input hash.
    """
    parts = [p.strip() for p in (kicker, headline, tease) if p and p.strip()]
    return "\n".join(parts)


def input_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Embedder:
    def __init__(self, token: str, model: str, dimensions: int) -> None:
        self.client = OpenAI(api_key=token)
        self.model = model
        self.dimensions = dimensions

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed one batch, preserving input order."""
        last: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                response = self.client.embeddings.create(
                    model=self.model,
                    input=list(texts),
                    dimensions=self.dimensions,
                )
                # The API documents order preservation, but sort on .index
                # anyway -- a silent misalignment here would attach the wrong
                # vector to the wrong story, which is near-impossible to spot.
                ordered = sorted(response.data, key=lambda d: d.index)
                vectors = [d.embedding for d in ordered]
                if len(vectors) != len(texts):
                    raise RuntimeError(
                        f"expected {len(texts)} embeddings, got {len(vectors)}"
                    )
                return vectors
            except Exception as exc:  # noqa: BLE001 - retry any transient failure
                last = exc
                if attempt == MAX_RETRIES - 1:
                    break
                sleep_for = 2**attempt
                log.warning("embedding batch failed (%s); retry in %ss", exc, sleep_for)
                time.sleep(sleep_for)
        raise RuntimeError(f"embedding failed after {MAX_RETRIES} attempts: {last}")

    def embed_all(self, texts: Sequence[str], batch_size: int = BATCH_SIZE):
        """Yield (offset, vectors) per batch so callers can commit as they go."""
        for start in range(0, len(texts), batch_size):
            chunk = texts[start : start + batch_size]
            yield start, self.embed(chunk)
