"""Discovery-weighted matching between listeners.

The goal is not "find your musical twin". A twin can't put you onto anything.
A good match shares enough taste that you trust their picks AND loves artists
you haven't heard that are likely to suit you. So every pair gets two scores:

overlap(a, b)
    Cosine similarity of IDF-weighted affinity vectors. IDF means sharing a
    niche artist counts for much more than sharing a superstar everyone has.
    Symmetric. This is the "can I trust their taste?" part.

discovery(a <- b)
    How much b could put a onto. For each artist n that b listens to and a does
    not, take n's weight in b's profile and multiply it by how close n is to a's
    taste. Closeness comes from item-item co-occurrence across the whole pool
    ("people who listen to X also listen to n"), computed with b left out, so
    b's own profile can't vouch for itself. Both profiles are IDF-weighted here
    too: a superstar is neither a discovery (everyone already knows them) nor
    good evidence of what fits you (they co-occur with everything). Directional.

score(a, b) = overlap ** alpha * discovery ** (1 - alpha)

By default discovery is mutual: min(discovery(a <- b), discovery(b <- a)), so
both people get something out of the connection.

Retrieval is two-stage. Overlap for every candidate is one sparse
matrix-vector product, which is cheap; the more expensive discovery score is
then computed only for the top `candidate_pool_size` candidates by overlap.
At the scale this app runs at (dozens to thousands of users) exact scoring is
the right choice. An approximate nearest-neighbour index only pays off far
beyond that.
"""

from __future__ import annotations

from collections.abc import Collection, Hashable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field

import numpy as np
from scipy import sparse

from echo.matching.profiles import TIME_RANGE_WEIGHTS, TasteSignal, affinity


@dataclass(frozen=True)
class MatchConfig:
    alpha: float = 0.5
    """Weight of overlap vs. discovery in the final score (0..1)."""

    mutual: bool = True
    """Require discovery in both directions (min of the two) rather than just for the viewer."""

    similarity_shrinkage: float = 2.0
    """Added to the cosine denominator so artist pairs seen together by only one or two
    listeners don't look perfectly similar. Matters a lot on small pools."""

    candidate_pool_size: int | None = 200
    """Stage-1 cut: only the top-N candidates by overlap get the full discovery score.
    None scores everyone."""

    min_overlap: float = 0.05
    """Candidates at or below this overlap are never shown (filters out people whose only
    common ground is a superstar everyone listens to)."""

    explain_top_n: int = 5
    """How many artists to list in each part of a match explanation."""

    explain_min_strength: float = 0.3
    """Only suggest artists at least this fraction as strong a fit as the best suggestion.
    Trims the weak tail, which is mostly superstars you've surely heard already.
    Affects the explanation lists only, never the score."""

    time_range_weights: Mapping[str, float] = field(default_factory=lambda: dict(TIME_RANGE_WEIGHTS))

    def __post_init__(self) -> None:
        if not 0.0 <= self.alpha <= 1.0:
            raise ValueError("alpha must be between 0 and 1")
        if self.similarity_shrinkage < 0:
            raise ValueError("similarity_shrinkage must be >= 0")


@dataclass(frozen=True)
class Match:
    user_id: str
    score: float
    overlap: float
    discovery_for_you: float
    discovery_for_them: float
    shared_artists: list[Hashable]
    """Shared artists that contribute most to overlap, niche ones first."""
    they_could_put_you_on: list[Hashable]
    """Artists the match loves that you don't know, ranked by fit with your taste."""
    you_could_put_them_on: list[Hashable]
    because_for_you: Mapping[Hashable, Hashable] = field(default_factory=dict)
    """For each artist in `they_could_put_you_on`, the artist of yours it is most often
    heard alongside, favouring niche links (with this match left out). Reads as
    "because you like X"."""


class MatchEngine:
    """Immutable snapshot of a listener pool, ready to answer match queries."""

    def __init__(
        self,
        user_ids: Sequence[str],
        artists: Sequence[Hashable],
        raw: sparse.csr_matrix,
        config: MatchConfig,
    ) -> None:
        if raw.shape != (len(user_ids), len(artists)):
            raise ValueError("raw matrix shape does not match users x artists")
        self.config = config
        self.user_ids = list(user_ids)
        self.artists = list(artists)
        self._user_index = {uid: i for i, uid in enumerate(self.user_ids)}

        raw = raw.astype(np.float64).tocsr()
        raw.eliminate_zeros()
        self._raw = raw
        n_users = raw.shape[0]

        self._presence = (raw > 0).astype(np.float64).tocsr()
        self._doc_freq = np.asarray(self._presence.sum(axis=0)).ravel()

        # Smoothed IDF (as in scikit-learn): rare artists weigh more, nothing goes to zero.
        self.idf = np.log((1.0 + n_users) / (1.0 + self._doc_freq)) + 1.0

        weighted = raw @ sparse.diags(self.idf)
        self._unit = _normalize_rows(weighted, order=2)  # rows have unit L2 norm -> dot = cosine
        # IDF-weighted profile with rows summing to 1, used on both sides of discovery.
        self._taste = _normalize_rows(weighted, order=1)
        self._cooc = (self._presence.T @ self._presence).tocsr()  # artist x artist co-listeners

    # ------------------------------------------------------------------ build

    @classmethod
    def build(
        cls,
        pool: Mapping[str, Iterable[TasteSignal]],
        config: MatchConfig | None = None,
    ) -> MatchEngine:
        """Build from {user_id: signals}. Users with no signals are kept but never match."""
        config = config or MatchConfig()
        user_ids = list(pool)
        artist_index: dict[Hashable, int] = {}
        rows: list[int] = []
        cols: list[int] = []
        vals: list[float] = []
        for row, uid in enumerate(user_ids):
            for artist, score in affinity(pool[uid], config.time_range_weights).items():
                col = artist_index.setdefault(artist, len(artist_index))
                rows.append(row)
                cols.append(col)
                vals.append(score)
        raw = sparse.csr_matrix(
            (vals, (rows, cols)), shape=(len(user_ids), len(artist_index)), dtype=np.float64
        )
        return cls(user_ids, list(artist_index), raw, config)

    # ---------------------------------------------------------------- queries

    def __contains__(self, user_id: object) -> bool:
        return user_id in self._user_index

    def overlap(self, a: str, b: str) -> float:
        ia, ib = self._idx(a), self._idx(b)
        return float(self._unit[ia].multiply(self._unit[ib]).sum())

    def discovery(self, target: str, source: str) -> float:
        """How much `source` could put `target` onto new music (0..1)."""
        score, _, _ = self._discovery(self._idx(target), self._idx(source))
        return score

    def affinities(self, user_id: str) -> dict[Hashable, float]:
        """A user's affinity score per artist, before IDF weighting."""
        cols, vals = self._row(self._raw, self._idx(user_id))
        return {self.artists[c]: float(v) for c, v in zip(cols, vals, strict=True)}

    def matches_for(
        self,
        user_id: str,
        candidates: Collection[str] | None = None,
        k: int = 10,
    ) -> list[Match]:
        """Best matches for `user_id`, optionally restricted to `candidates`.

        The whole pool is always used for IDF and co-occurrence statistics, even when
        `candidates` is narrower (e.g. only people who opted in to be discoverable).
        """
        cfg = self.config
        ia = self._idx(user_id)

        overlaps = np.asarray((self._unit @ self._unit[ia].T).todense()).ravel()
        overlaps[ia] = -np.inf  # never match yourself
        if candidates is not None:
            allowed = np.zeros(len(self.user_ids), dtype=bool)
            for uid in candidates:
                j = self._user_index.get(uid)
                if j is not None:
                    allowed[j] = True
            overlaps[~allowed] = -np.inf
        overlaps[overlaps <= cfg.min_overlap] = -np.inf

        eligible = np.flatnonzero(np.isfinite(overlaps))
        if cfg.candidate_pool_size is not None and eligible.size > cfg.candidate_pool_size:
            top = np.argpartition(-overlaps[eligible], cfg.candidate_pool_size - 1)
            eligible = eligible[top[: cfg.candidate_pool_size]]

        results: list[Match] = []
        for ib in eligible:
            overlap = float(overlaps[ib])
            disc_you, for_you, because_you = self._discovery(ia, ib)
            disc_them, for_them, _ = self._discovery(ib, ia)
            disc = min(disc_you, disc_them) if cfg.mutual else disc_you
            score = overlap**cfg.alpha * disc ** (1.0 - cfg.alpha)
            results.append(
                Match(
                    user_id=self.user_ids[ib],
                    score=score,
                    overlap=overlap,
                    discovery_for_you=disc_you,
                    discovery_for_them=disc_them,
                    shared_artists=self._shared(ia, ib),
                    they_could_put_you_on=for_you,
                    you_could_put_them_on=for_them,
                    because_for_you=because_you,
                )
            )
        results.sort(key=lambda m: (m.score, m.overlap), reverse=True)
        return results[:k]

    # -------------------------------------------------------------- internals

    def _idx(self, user_id: str) -> int:
        try:
            return self._user_index[user_id]
        except KeyError:
            raise KeyError(f"unknown user {user_id!r}") from None

    @staticmethod
    def _row(matrix: sparse.csr_matrix, i: int) -> tuple[np.ndarray, np.ndarray]:
        start, end = matrix.indptr[i], matrix.indptr[i + 1]
        return matrix.indices[start:end], matrix.data[start:end]

    def _shared(self, ia: int, ib: int) -> list[Hashable]:
        a_cols, a_vals = self._row(self._unit, ia)
        b_cols, b_vals = self._row(self._unit, ib)
        common, a_pos, b_pos = np.intersect1d(a_cols, b_cols, assume_unique=True, return_indices=True)
        if common.size == 0:
            return []
        contribution = a_vals[a_pos] * b_vals[b_pos]
        order = np.argsort(-contribution)[: self.config.explain_top_n]
        return [self.artists[c] for c in common[order]]

    def _discovery(
        self, target: int, source: int
    ) -> tuple[float, list[Hashable], dict[Hashable, Hashable]]:
        """Discovery score, the best suggestions, and the target artist closest to each suggestion."""
        t_cols, t_taste = self._row(self._taste, target)
        s_cols, s_taste = self._row(self._taste, source)
        if t_cols.size == 0 or s_cols.size == 0:
            return 0.0, [], {}

        novel_mask = ~np.isin(s_cols, t_cols, assume_unique=True)
        novel = s_cols[novel_mask]
        if novel.size == 0:
            return 0.0, [], {}

        similarity = self._similarity_without(novel, t_cols, excluded_user=source)
        relevance = similarity @ t_taste  # weighted mean closeness to the target's taste
        contribution = s_taste[novel_mask] * relevance

        order = np.argsort(-contribution)
        floor = max(contribution.max() * self.config.explain_min_strength, np.finfo(float).tiny)
        picked = [i for i in order[: self.config.explain_top_n] if contribution[i] >= floor]
        top = [self.artists[novel[i]] for i in picked]
        # "Because you like X": X is the target's artist most often heard alongside the suggestion,
        # weighted by IDF so a niche link beats "everyone also plays this superstar". Not weighted
        # by the target's taste, or X would nearly always be their #1 artist.
        closest = similarity * self.idf[t_cols]
        because = {
            self.artists[novel[i]]: self.artists[t_cols[int(np.argmax(closest[i]))]]
            for i in picked
            if similarity[i].max() > 0
        }
        return float(contribution.sum()), top, because

    def _similarity_without(self, rows: np.ndarray, cols: np.ndarray, excluded_user: int) -> np.ndarray:
        """Shrunk cosine similarity between artist sets, ignoring one user's listening."""
        cooc = self._cooc[rows][:, cols].toarray()
        excluded = self._presence[excluded_user]
        in_rows = excluded[:, rows].toarray().ravel()
        in_cols = excluded[:, cols].toarray().ravel()

        cooc = np.clip(cooc - np.outer(in_rows, in_cols), 0.0, None)
        deg_rows = self._doc_freq[rows] - in_rows
        deg_cols = self._doc_freq[cols] - in_cols
        denom = np.sqrt(np.outer(deg_rows, deg_cols)) + self.config.similarity_shrinkage
        with np.errstate(divide="ignore", invalid="ignore"):
            sim = np.where(denom > 0, cooc / denom, 0.0)
        return sim


def _normalize_rows(matrix: sparse.csr_matrix, order: int) -> sparse.csr_matrix:
    if order == 2:
        norms = np.sqrt(np.asarray(matrix.multiply(matrix).sum(axis=1)).ravel())
    else:
        norms = np.asarray(abs(matrix).sum(axis=1)).ravel()
    scale = np.divide(1.0, norms, out=np.zeros_like(norms), where=norms > 0)
    return (sparse.diags(scale) @ matrix).tocsr()
