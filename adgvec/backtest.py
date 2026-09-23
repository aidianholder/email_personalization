"""Offline evaluation of the ranking model.

The question this answers is the one that gates the whole project: does
matching a reader's profile against the day's stories rank what they actually
read above what a dumb baseline would pick? If it cannot beat "most-read
story of the day", the personalization is decorative and should not ship.

**Design: leave-one-out.** The reader histories here span a median of 5
publication days, so a train/holdout split by date would leave most readers
with nothing to hold out. Instead, for each reader one read is withheld, the
profile is rebuilt from the rest, and we measure where the withheld story
lands among the stories published that same day. Pool size (~68) matches the
production candidate pool (69 eligible), so recall@2 here means the same
thing it will mean in the email: did the story the reader chose make it into
one of the two personalized slots?

Every variant is scored on identical (reader, held-out story, pool) instances
so the comparison is like-for-like.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Sequence

import numpy as np

from .profile import Read, build_profile

log = logging.getLogger(__name__)

DEFAULT_ALPHA = 0.4
MAX_EVALS_PER_READER = 10   # keep heavy readers from dominating the average


@dataclass
class Instance:
    """One evaluation: a reader, the story withheld, and the day's pool."""
    user_id: str
    train: list[Read]
    held_out_id: int
    pool_ids: np.ndarray          # candidate story ids, held-out included
    pool_vecs: np.ndarray         # raw (uncentered) embeddings
    pool_published: np.ndarray    # for the recency baseline


@dataclass
class Metrics:
    name: str
    n: int = 0
    hits1: int = 0
    hits2: int = 0
    hits5: int = 0
    rr_sum: float = 0.0
    pct_sum: float = 0.0
    pool_sum: int = 0

    def add(self, rank: int, pool_size: int) -> None:
        """rank is 1-based."""
        self.n += 1
        self.hits1 += rank <= 1
        self.hits2 += rank <= 2
        self.hits5 += rank <= 5
        self.rr_sum += 1.0 / rank
        # Fraction of the pool ranked below the true story; 1.0 is perfect.
        self.pct_sum += (pool_size - rank) / max(1, pool_size - 1)
        self.pool_sum += pool_size

    def row(self) -> dict:
        n = max(1, self.n)
        return {
            "variant": self.name, "n": self.n,
            "recall@1": 100.0 * self.hits1 / n,
            "recall@2": 100.0 * self.hits2 / n,
            "recall@5": 100.0 * self.hits5 / n,
            "MRR": self.rr_sum / n,
            "pctile": 100.0 * self.pct_sum / n,
            "pool": self.pool_sum / n,
        }


def _rank_of(scores: np.ndarray, target_index: int) -> int:
    """1-based rank of target under descending score, ties broken against us."""
    target = scores[target_index]
    better = int((scores > target).sum())
    ties = int((scores == target).sum()) - 1
    return better + ties + 1          # pessimistic on ties


# --------------------------------------------------------------------------- #
# scorers: each returns a score per pool entry, higher is better
# --------------------------------------------------------------------------- #

def profile_scorer(
    corpus_mean: np.ndarray,
    popularity: dict[int, int],
    *,
    center: bool = True,
    cluster: bool = True,
    weights: bool = True,
    use_recency: bool | None = None,
    use_rarity: bool | None = None,
    alpha: float = DEFAULT_ALPHA,
    sim_threshold: float = 0.55,
    max_clusters: int = 8,
    half_life_days: float = 14.0,
) -> Callable[[Instance], np.ndarray]:
    zero = np.zeros_like(corpus_mean)

    def score(inst: Instance) -> np.ndarray:
        mean = corpus_mean if center else zero
        clusters = build_profile(
            inst.train, mean, popularity,
            sim_threshold=sim_threshold,
            half_life_days=half_life_days,
            # min_reads_to_cluster above any history size collapses the
            # profile to one weighted mean -- the "no clustering" control.
            min_reads_to_cluster=4 if cluster else 10**6,
            max_clusters=max_clusters,
            use_recency=weights if use_recency is None else use_recency,
            use_rarity=weights if use_rarity is None else use_rarity,
        )
        if not clusters:
            return np.zeros(len(inst.pool_ids))
        C = inst.pool_vecs - mean
        n = np.linalg.norm(C, axis=1, keepdims=True)
        n[n < 1e-9] = 1.0
        C = C / n
        cent = np.vstack([c.centroid for c in clusters])
        stre = np.array([c.strength for c in clusters], dtype=np.float64)
        return (C @ cent.T * (stre ** alpha)).max(axis=1)

    return score


def popularity_scorer(
    popularity: dict[int, int], reader_reads: dict[str, set[int]]
) -> Callable[[Instance], np.ndarray]:
    """Most-read story of the day -- the baseline that matters.

    The current reader's own read is subtracted, or the held-out story would
    score its own positive label.
    """
    def score(inst: Instance) -> np.ndarray:
        mine = reader_reads.get(inst.user_id, set())
        return np.array([
            popularity.get(int(sid), 0) - (1 if int(sid) in mine else 0)
            for sid in inst.pool_ids
        ], dtype=np.float64)
    return score


def recency_scorer() -> Callable[[Instance], np.ndarray]:
    return lambda inst: inst.pool_published.astype(np.float64)


def random_scorer(seed: int = 0) -> Callable[[Instance], np.ndarray]:
    rng = np.random.default_rng(seed)
    return lambda inst: rng.random(len(inst.pool_ids))


def pageview_scorer(
    popularity: dict[int, dict],
    field: str = "views",
    subtract_self: bool = True,
) -> Callable[[Instance], np.ndarray]:
    """Rank by real pageview counts.

    `subtract_self` removes one view from the held-out story. The reader being
    evaluated did read it, so their own pageview sits in that count while no
    negative gets the same bump -- with a p10 of 7 views that bias is not
    negligible.
    """
    def score(inst: Instance) -> np.ndarray:
        out = np.zeros(len(inst.pool_ids), dtype=np.float64)
        for i, sid in enumerate(inst.pool_ids):
            row = popularity.get(int(sid))
            if row is None:
                continue
            value = float(row.get(field) or 0.0)
            if subtract_self and int(sid) == inst.held_out_id and field in ("views", "uniques"):
                value = max(0.0, value - 1.0)
            out[i] = value
        return out
    return score


def hybrid_scorer(
    base: Callable[[Instance], np.ndarray],
    overlay: Callable[[Instance], np.ndarray],
    weight: float,
) -> Callable[[Instance], np.ndarray]:
    """Blend two scorers after min-max normalizing each within the pool.

    The head-to-head question ("does personalization beat popularity?") is
    the wrong one if the two signals are complementary. This measures whether
    personalization adds anything *on top of* the popularity ranking, which
    is how it would actually be deployed.
    """
    def norm(v: np.ndarray) -> np.ndarray:
        lo, hi = v.min(), v.max()
        return (v - lo) / (hi - lo) if hi > lo else np.zeros_like(v)

    return lambda inst: (1 - weight) * norm(base(inst)) + weight * norm(overlay(inst))


def evaluate(
    instances: Sequence[Instance],
    scorers: dict[str, Callable[[Instance], np.ndarray]],
) -> list[Metrics]:
    results = {name: Metrics(name) for name in scorers}
    for inst in instances:
        target = int(np.flatnonzero(inst.pool_ids == inst.held_out_id)[0])
        for name, fn in scorers.items():
            results[name].add(_rank_of(fn(inst), target), len(inst.pool_ids))
    return list(results.values())
