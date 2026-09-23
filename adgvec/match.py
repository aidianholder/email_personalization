"""Daily match: choose each reader's stories and render the email payload.

Slots are filled by a waterfall rather than a fixed "3 editor + 2
personalized" split, because personalization routinely yields fewer than two
usable picks -- a reader with no profile, a reader whose only cluster is
exhausted, a pick that duplicates an editor choice, a pick filtered out for
lacking a thumbnail. The invariant is that every reader gets a full email:

    1. editor picks, in the order the desk supplied them
    2. personalized picks, best-scoring first
    3. remaining editor picks (reserves)
    4. a recency tail, so "full email" never depends on the desk filling in
       more than three slots

Anything already chosen is skipped at every tier, so duplicates cannot occur.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Sequence

import numpy as np

log = logging.getLogger(__name__)

DEFAULT_LAYOUT = "EEEPP"      # E = editor pick, P = personalized
DEFAULT_ALPHA = 0.4           # strength exponent in strength^alpha * cosine
# Ceiling on similarity between the two personalized picks when they come
# from the same cluster. Prevents two versions of one game recap while still
# filling the slot -- most readers have a single cluster on thin history.
DEFAULT_MAX_PAIR_SIM = 0.70
# Weight on the personal match score when blended with pageview popularity;
# the remainder goes to popularity. Swept against the temporal backtest:
# recall@2 peaks at 0.6 (34.2%) versus 26.6% for popularity alone and 24.8%
# for the model alone. Both signals are min-max normalised within the
# candidate pool before blending.
DEFAULT_HYBRID_WEIGHT = 0.6


@dataclass
class Candidate:
    story_id: int
    headline: str
    tease: str
    share_url: str
    thumbnail_url: str | None
    thumbnails: dict | None
    pub_date: Any
    vector: np.ndarray | None = None


@dataclass
class Pick:
    story_id: int
    source: str                      # editor | personalized | tail
    score: float | None = None
    cluster_index: int | None = None


@dataclass
class MatchStats:
    readers: int = 0
    personalized_filled: dict[int, int] = field(default_factory=dict)
    source_counts: dict[str, int] = field(default_factory=dict)

    def record(self, picks: Sequence[Pick]) -> None:
        self.readers += 1
        n = sum(1 for p in picks if p.source == "personalized")
        self.personalized_filled[n] = self.personalized_filled.get(n, 0) + 1
        for p in picks:
            self.source_counts[p.source] = self.source_counts.get(p.source, 0) + 1


def eligible_candidates(
    candidates: Sequence[Candidate], require_thumbnail: bool = True
) -> list[Candidate]:
    """Drop anything the email template cannot render."""
    out = []
    for c in candidates:
        if not c.share_url or not c.headline:
            continue
        if require_thumbnail and not c.thumbnail_url:
            continue
        out.append(c)
    return out


def rank_personalized(
    candidates: Sequence[Candidate],
    centroids: np.ndarray,
    strengths: np.ndarray,
    alpha: float = DEFAULT_ALPHA,
) -> tuple[np.ndarray, np.ndarray]:
    """Score candidates as max over clusters of strength^alpha * cosine.

    Max rather than mean: a story matching one strong interest should win even
    if it is unrelated to the reader's others.
    """
    C = np.vstack([c.vector for c in candidates])
    sims = C @ centroids.T                     # candidates x clusters
    weighted = sims * (strengths ** alpha)
    return weighted.max(axis=1), weighted.argmax(axis=1)


def _minmax(values: np.ndarray) -> np.ndarray:
    lo, hi = float(values.min()), float(values.max())
    return (values - lo) / (hi - lo) if hi > lo else np.zeros_like(values)


def blend_with_popularity(
    model_scores: np.ndarray,
    candidates: Sequence[Candidate],
    popularity: dict[int, float] | None,
    weight: float = DEFAULT_HYBRID_WEIGHT,
) -> np.ndarray:
    """Blend the personal score with pageview popularity.

    Neither signal wins alone: on a temporal backtest popularity scored 26.6%
    recall@2 and the personal match 24.8%, while blending them reaches 34.2%.
    They fail on different readers -- popularity collapses for heavy readers
    (15.7% at 20+ reads) exactly where the personal score holds steady.
    """
    if not popularity or weight >= 1.0:
        return model_scores
    pop = np.array([float(popularity.get(c.story_id, 0.0)) for c in candidates],
                   dtype=np.float64)
    return (1.0 - weight) * _minmax(pop) + weight * _minmax(model_scores)


def choose_personalized(
    candidates: Sequence[Candidate],
    centroids: np.ndarray,
    strengths: np.ndarray,
    n_wanted: int,
    exclude: set[int],
    alpha: float = DEFAULT_ALPHA,
    max_pair_sim: float = DEFAULT_MAX_PAIR_SIM,
    popularity: dict[int, float] | None = None,
    hybrid_weight: float = DEFAULT_HYBRID_WEIGHT,
) -> list[Pick]:
    if n_wanted <= 0 or not candidates or centroids.size == 0:
        return []

    model_scores, best_cluster = rank_personalized(
        candidates, centroids, strengths, alpha)
    scores = blend_with_popularity(
        model_scores, candidates, popularity, hybrid_weight)
    order = np.argsort(-scores)

    picks: list[Pick] = []
    taken_clusters: set[int] = set()
    taken_vectors: list[np.ndarray] = []

    for pass_no in (1, 2):
        # Pass 1 insists on a fresh cluster so the two picks represent
        # different interests. Pass 2 relaxes that -- most readers have only
        # one cluster on thin history, and refusing to fill the slot there
        # would leave the majority of emails short.
        for i in order:
            if len(picks) >= n_wanted:
                break
            cand = candidates[i]
            if cand.story_id in exclude:
                continue
            cluster = int(best_cluster[i])
            if pass_no == 1 and cluster in taken_clusters:
                continue
            if pass_no == 2:
                # Same-cluster fallback still refuses near-duplicates.
                if any(float(cand.vector @ v) > max_pair_sim for v in taken_vectors):
                    continue
            picks.append(Pick(cand.story_id, "personalized",
                              float(scores[i]), cluster))
            exclude.add(cand.story_id)
            taken_clusters.add(cluster)
            taken_vectors.append(cand.vector)
        if len(picks) >= n_wanted:
            break
    return picks


def fill_slots(
    layout: str,
    editor_ids: Sequence[int],
    personalized: Sequence[Pick],
    tail_ids: Sequence[int],
    exclude: set[int],
) -> list[Pick]:
    """Apply the waterfall and return exactly len(layout) picks (or fewer if
    the pools are genuinely exhausted)."""
    chosen: list[Pick] = []
    used: set[int] = set()
    editors = list(editor_ids)
    pers = list(personalized)
    tail = list(tail_ids)

    def take_editor() -> Pick | None:
        while editors:
            sid = editors.pop(0)
            if sid not in used and sid not in exclude:
                return Pick(sid, "editor")
        return None

    def take_personalized() -> Pick | None:
        while pers:
            p = pers.pop(0)
            if p.story_id not in used:
                return p
        return None

    def take_tail() -> Pick | None:
        while tail:
            sid = tail.pop(0)
            if sid not in used and sid not in exclude:
                return Pick(sid, "tail")
        return None

    for kind in layout:
        primary = take_editor if kind == "E" else take_personalized
        secondary = take_personalized if kind == "E" else take_editor
        pick = primary() or secondary() or take_tail()
        if pick is None:
            break
        used.add(pick.story_id)
        chosen.append(pick)
    return chosen


def render_payload(
    picks: Sequence[Pick],
    stories: dict[int, Candidate],
    edition_id: int,
    edition_date,
    name: str = "Morning News Update",
) -> dict:
    """Build the Connected Content response body.

    Mirrors the shape of Ellington's /api/sectionfronts/<id>/ so the same
    Liquid template renders either an editor-curated front or this feed --
    which also makes an A/B test a matter of swapping the URL. The full
    `story` body that a section front carries is deliberately omitted: five
    complete articles would be a large response and the template never uses
    it.
    """
    items = []
    for slot, p in enumerate(picks, start=1):
        s = stories.get(p.story_id)
        if s is None:
            continue
        items.append({
            "id": s.story_id,
            "slot": slot,
            "source": p.source,
            "headline": s.headline,
            "tease": s.tease,
            "share_url": s.share_url,
            "thumbnails": s.thumbnails or {},
            "thumbnail_url": s.thumbnail_url,
            "pub_date": s.pub_date.isoformat() if s.pub_date else None,
        })
    return {
        "id": edition_id,
        "name": name,
        "date_effective": str(edition_date),
        "pieces": {"stories": items},
    }
