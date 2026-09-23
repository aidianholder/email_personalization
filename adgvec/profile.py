"""Build reader interest profiles from matched reading history.

The shape of a profile is a list of (centroid, strength) pairs rather than a
single average vector. Averaging a reader's whole history blends distinct
interests into a point that represents none of them: the mean of a Razorbacks
story and a legislature story is generic local news, and retrieving against it
returns articles that are moderately close to everything and relevant to
nothing. Clustering first preserves the separate interests.

Two corrections are applied before any of that:

* **Corpus centering.** Every ADG story shares a large common direction
  (Arkansas, local, this paper's voice). Left in, it dominates every cosine
  comparison and every reader's matches converge on the same stories. The
  corpus mean is subtracted first.
* **Rarity weighting.** A story everyone read carries little information about
  any individual. Contributions are damped by overall readership.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

import numpy as np
from sklearn.cluster import AgglomerativeClustering

log = logging.getLogger(__name__)

# Clustering is OFF by default. A temporally honest backtest on 30 days of
# real history put multi-centroid profiles at 20.4% recall@2 against 23.9%
# for a plain mean, and the gap widened with more history rather than
# closing: capping discards reads, and even uncapped, max-similarity is a
# worse estimator than the average. Diversity between the two email slots --
# the original reason for clustering -- is handled by the max_pair_sim
# ceiling in match.py, which needs no clusters. Opt back in with
# `profiles --cluster` to re-test.
DEFAULT_CLUSTER = False

# Both weighting schemes measured negative once read timestamps made them
# testable. Recency decay is monotonically harmful (every half-life from
# 0.25d to 30d scored below no decay at all -- interests are stable across a
# 30-day window and there are only ~13 matched reads to spend), and rarity
# weighting costs about 1.4 points on its own.
DEFAULT_USE_RECENCY = False
DEFAULT_USE_RARITY = False

# Cosine similarity above which two stories are treated as the same interest.
# Only consulted when clustering is enabled. Calibrate by reading clustered
# headlines (`adgvec profiles --inspect --cluster`), not by optimizing a
# metric -- "is this a coherent beat" is a human judgment.
DEFAULT_SIM_THRESHOLD = 0.55
# Below this many matched reads, clustering is noise; use one weighted mean.
MIN_READS_TO_CLUSTER = 4
# Recency half-life, in days, measured from the reader's most recent read.
DEFAULT_HALF_LIFE_DAYS = 14.0
# Cap on clusters stored per reader, keeping the strongest. Readers have a
# long tail of one-off topics -- measured at ~55% of reads across every
# threshold tried -- so hard-dropping singletons throws away real signal,
# while keeping all of them bloats the profile and turns max-similarity
# scoring into plain nearest-neighbour over the whole history. Ranking by
# strength and truncating keeps the interests that actually recur.
DEFAULT_MAX_CLUSTERS = 8
# Optional hard drop of one-story clusters for readers with at least this
# much history. 0 disables it; superseded in practice by the cap above.
DEFAULT_SINGLETON_DROP_MIN_READS = 0


def as_array(value) -> np.ndarray:
    """Coerce a pgvector value to float64 ndarray.

    psycopg hands back pgvector.Vector objects here rather than raw arrays,
    so normalize at the boundary instead of at every call site.
    """
    if hasattr(value, "to_numpy"):
        return value.to_numpy().astype(np.float64)
    return np.asarray(value, dtype=np.float64)


@dataclass
class Read:
    story_id: int
    vector: np.ndarray
    published: datetime | None
    headline: str
    # When the reader actually opened it, from the reads table. Falls back to
    # `published` when absent; the two differ by a median of 13 hours, and by
    # far more in the tail where readers find older stories.
    read_at: datetime | None = None

    @property
    def when(self) -> datetime | None:
        return self.read_at or self.published


@dataclass
class Cluster:
    centroid: np.ndarray
    strength: float
    story_ids: list[int]
    label: str

    @property
    def story_count(self) -> int:
        return len(self.story_ids)


def recency_weights(
    reads: Sequence[Read], half_life_days: float = DEFAULT_HALF_LIFE_DAYS
) -> np.ndarray:
    """Exponential decay measured from this reader's most recent read.

    Anchored to the reader's own most recent activity rather than wall-clock
    now, so a historical sample decays internally instead of collapsing to
    zero uniformly. Uses actual read time when the reads table supplies it.
    """
    dates = [r.when for r in reads if r.when is not None]
    if not dates or half_life_days <= 0:
        return np.ones(len(reads), dtype=np.float64)
    newest = max(dates)
    out = np.empty(len(reads), dtype=np.float64)
    for i, r in enumerate(reads):
        when = r.when
        if when is None:
            out[i] = 0.5  # unknown date: neither fresh nor stale
            continue
        age_days = max(0.0, (newest - when).total_seconds() / 86400.0)
        out[i] = 0.5 ** (age_days / half_life_days)
    return out


def rarity_weights(
    reads: Sequence[Read], popularity: dict[int, int]
) -> np.ndarray:
    """Damp stories that everybody read.

    Reading history reflects what the homepage promoted as much as genuine
    interest; a story 800 readers opened says little about any one of them.
    """
    return np.array(
        [1.0 / math.log(1.0 + max(1, popularity.get(r.story_id, 1))) for r in reads],
        dtype=np.float64,
    )


def _weighted_unit_mean(X: np.ndarray, w: np.ndarray) -> np.ndarray:
    v = (X * w[:, None]).sum(axis=0)
    norm = np.linalg.norm(v)
    # Degenerate only if members cancel exactly; fall back to the plain mean.
    if norm < 1e-9:
        v = X.mean(axis=0)
        norm = np.linalg.norm(v)
    return v / norm if norm > 1e-9 else v


def build_profile(
    reads: Sequence[Read],
    corpus_mean: np.ndarray,
    popularity: dict[int, int],
    sim_threshold: float = DEFAULT_SIM_THRESHOLD,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
    min_reads_to_cluster: int = MIN_READS_TO_CLUSTER,
    singleton_drop_min_reads: int = DEFAULT_SINGLETON_DROP_MIN_READS,
    max_clusters: int = DEFAULT_MAX_CLUSTERS,
    use_recency: bool = DEFAULT_USE_RECENCY,
    use_rarity: bool = DEFAULT_USE_RARITY,
    cluster: bool = DEFAULT_CLUSTER,
) -> list[Cluster]:
    """Turn one reader's matched reads into a list of (centroid, strength).

    Returns a single-element list by default: one unweighted mean of the
    reader's corpus-centered read vectors. See DEFAULT_CLUSTER above for why.
    """
    if not reads:
        return []

    # De-duplicate: a repeated URL in the history should not double-count.
    seen: dict[int, Read] = {}
    for r in reads:
        seen.setdefault(r.story_id, r)
    reads = list(seen.values())

    X = np.vstack([r.vector for r in reads]).astype(np.float64)

    # Center against the corpus, then renormalize onto the unit sphere. This
    # is what strips the shared "local Arkansas news" direction out of the
    # geometry so the remaining signal is what distinguishes readers.
    X = X - corpus_mean
    norms = np.linalg.norm(X, axis=1, keepdims=True)
    norms[norms < 1e-9] = 1.0
    X = X / norms

    w = np.ones(len(reads), dtype=np.float64)
    if use_recency:
        w *= recency_weights(reads, half_life_days)
    if use_rarity:
        w *= rarity_weights(reads, popularity)
    # Normalize to mean 1 so `strength` reads as an effective story count and
    # is comparable across readers.
    mean_w = w.mean()
    w = w / mean_w if mean_w > 1e-12 else np.ones_like(w)

    def make(indices: np.ndarray) -> Cluster:
        members = [reads[i] for i in indices]
        weights = w[indices]
        top = members[int(np.argmax(weights))]
        return Cluster(
            centroid=_weighted_unit_mean(X[indices], weights),
            strength=float(weights.sum()),
            story_ids=[m.story_id for m in members],
            label=top.headline,
        )

    if not cluster or len(reads) < min_reads_to_cluster:
        # Default path: one centroid over every read. Also the fallback when
        # clustering is on but history is too thin for structure to mean
        # anything.
        return [make(np.arange(len(reads)))]

    labels = AgglomerativeClustering(
        n_clusters=None,
        distance_threshold=1.0 - sim_threshold,
        metric="cosine",
        linkage="average",
    ).fit_predict(X)

    clusters = [make(np.flatnonzero(labels == c)) for c in np.unique(labels)]

    if singleton_drop_min_reads and len(reads) >= singleton_drop_min_reads:
        kept = [c for c in clusters if c.story_count > 1]
        if kept:
            clusters = kept

    # Strongest interests first, then truncate. Strength already folds in
    # recency and rarity, so a lone but recent and unusual read can still
    # earn a slot over a stale multi-story cluster -- which is the intent.
    clusters.sort(key=lambda c: c.strength, reverse=True)
    if max_clusters and len(clusters) > max_clusters:
        clusters = clusters[:max_clusters]
    return clusters
