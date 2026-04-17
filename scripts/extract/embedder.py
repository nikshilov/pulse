"""Pluggable embedding generator for Pulse POC.

Two backends:

- **fake-local** (DEFAULT): deterministic 128-dim vector derived from SHA-256 of
  the input text, mapped to floats in [-1, 1]. No API calls, no cost, fully
  reproducible. Semantically meaningless — but exercises the whole pipeline
  (store → load → cosine → seed union → BFS → rank) end-to-end without any
  external dependency. This is the plumbing substrate. Real semantic wins
  require the second backend.

- **openai-text-embedding-3-large**: real OpenAI embedding (3072-dim). Guarded
  import of the `openai` SDK — we do NOT add it as a required dependency. If a
  caller requests this model and `openai` is not installed, a clear
  `RuntimeError` is raised pointing at the install command. Requires the
  `OPENAI_API_KEY` environment variable.

Cosine similarity is computed pure-Python in `retrieval.py` (no numpy).
"""

from __future__ import annotations

import hashlib
import os
import struct
from typing import Iterable


FAKE_LOCAL_MODEL = "fake-local"
FAKE_LOCAL_DIM = 128

OPENAI_MODEL = "openai-text-embedding-3-large"
OPENAI_DIM = 3072


def embed_texts(texts: list[str], model: str = FAKE_LOCAL_MODEL) -> list[list[float]]:
    """Return one vector per input text.

    model options:
      - 'fake-local' — deterministic hash-based fake embedding for testing.
        128-dim, stable across runs. No API calls, no costs. DEFAULT.
      - 'openai-text-embedding-3-large' — real OpenAI embedding (requires
        OPENAI_API_KEY). 3072-dim. Only call if explicitly requested.
    """
    if not isinstance(texts, list):
        # Accept any iterable defensively — but normalise to list so batching
        # and length checks are deterministic.
        texts = list(texts)

    if model == FAKE_LOCAL_MODEL:
        return [_fake_local_embed(t) for t in texts]
    if model == OPENAI_MODEL:
        return _openai_embed(texts)
    raise ValueError(
        f"Unknown embedding model: {model!r}. "
        f"Known: {FAKE_LOCAL_MODEL!r}, {OPENAI_MODEL!r}."
    )


def embedding_dim(model: str) -> int:
    """Return the expected vector dimension for a given model."""
    if model == FAKE_LOCAL_MODEL:
        return FAKE_LOCAL_DIM
    if model == OPENAI_MODEL:
        return OPENAI_DIM
    raise ValueError(f"Unknown embedding model: {model!r}")


# ---------------------------------------------------------------------------
# Fake-local: SHA-256 → 128 floats in [-1, 1]
# ---------------------------------------------------------------------------

def _fake_local_embed(text: str) -> list[float]:
    """Deterministic 128-dim pseudo-embedding derived from SHA-256 of the text.

    Each 4-byte chunk of the hash stream is unpacked as an unsigned 32-bit int
    and mapped to a float in [-1, 1]. We repeatedly re-hash (with a counter
    suffix) to generate enough bytes for 128 dims × 4 bytes = 512 bytes.

    Properties we actually rely on in tests and in the retrieval pipeline:
      - same input → same vector across processes/runs
      - different inputs → (almost surely) different vectors
      - output length == 128, every element finite and in [-1, 1]

    What we explicitly do NOT get: any semantic structure. Cosine similarity
    between 'тревожно' and 'тревога' is random. The fake backend is for
    plumbing tests only.
    """
    needed_bytes = FAKE_LOCAL_DIM * 4
    buf = bytearray()
    counter = 0
    # Mix the raw text plus a monotonically-growing counter so the SHA stream
    # can be extended indefinitely without repeating blocks.
    while len(buf) < needed_bytes:
        h = hashlib.sha256(f"{text}||{counter}".encode("utf-8")).digest()
        buf.extend(h)
        counter += 1

    vec: list[float] = []
    for i in range(FAKE_LOCAL_DIM):
        (u,) = struct.unpack_from(">I", buf, i * 4)
        # Map unsigned 32-bit int [0, 2**32 - 1] → float in [-1, 1].
        f = (u / 0xFFFFFFFF) * 2.0 - 1.0
        vec.append(f)
    return vec


# ---------------------------------------------------------------------------
# OpenAI: optional real backend. Imports guarded so the dep stays optional.
# ---------------------------------------------------------------------------

def _openai_embed(texts: list[str]) -> list[list[float]]:
    try:
        from openai import OpenAI  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "openai package is required for model "
            f"{OPENAI_MODEL!r}. Install with: pip install openai"
        ) from e

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY environment variable is required for model "
            f"{OPENAI_MODEL!r}."
        )

    client = OpenAI(api_key=api_key)
    # The SDK accepts a list[str] in one call; batching happens client-side
    # up to the model's input token limit. For POC scale (hundreds of
    # entities) this is fine — caller batches at 50 to be safe.
    resp = client.embeddings.create(model="text-embedding-3-large", input=texts)
    # resp.data is list[Embedding]; each has .embedding (list[float])
    return [list(item.embedding) for item in resp.data]
