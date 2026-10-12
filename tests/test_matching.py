import math

import pytest

from echo.matching import MatchConfig, MatchEngine, TasteSignal, affinity, fingerprint, rank_weight


def listens(*artists: str, time_range: str = "long_term") -> list[TasteSignal]:
    """A ranked list: first artist is rank 1."""
    return [TasteSignal(a, time_range, rank) for rank, a in enumerate(artists, start=1)]


def indie_world() -> dict[str, list[TasteSignal]]:
    """Background listeners establish that i1..i8 belong together and j1..j8 belong together.

    Everyone also has the superstar, so it should carry almost no weight.
    """
    pool: dict[str, list[TasteSignal]] = {}
    indie_sets = [
        ("i1", "i5", "i6", "i2"),
        ("i2", "i6", "i7", "i3"),
        ("i3", "i5", "i8", "i1"),
        ("i4", "i7", "i5", "i2"),
        ("i1", "i8", "i6", "i4"),
        ("i3", "i6", "i5", "i7"),
    ]
    jazz_sets = [
        ("j1", "j2", "j3", "j4"),
        ("j5", "j6", "j1", "j7"),
        ("j2", "j8", "j3", "j5"),
    ]
    for n, artists in enumerate(indie_sets):
        pool[f"bg_indie_{n}"] = listens(*artists, "star")
    for n, artists in enumerate(jazz_sets):
        pool[f"bg_jazz_{n}"] = listens(*artists, "star")

    pool["alice"] = listens("i1", "i2", "i3", "i4", "star")
    pool["twin"] = listens("i1", "i2", "i3", "i4", "star")  # identical: nothing to offer
    pool["comp"] = listens("i1", "i2", "i5", "i6", "star")  # shares the core, brings new indie
    pool["jazzy"] = listens("i1", "j1", "j2", "j3", "j4")  # little overlap, unrelated picks
    pool["stranger"] = listens("j5", "j6", "j7", "j8", "star")
    return pool


# ----------------------------------------------------------------- profiles


def test_rank_weight_decays_but_stays_positive():
    assert rank_weight(1) == 1.0
    assert math.isclose(rank_weight(3), 0.5)
    assert rank_weight(2) > rank_weight(10) > rank_weight(50) > 0


def test_rank_weight_rejects_zero():
    with pytest.raises(ValueError):
        rank_weight(0)


def test_affinity_blends_time_ranges():
    signals = [
        TasteSignal("a", "long_term", 1),
        TasteSignal("a", "short_term", 1),
        TasteSignal("b", "short_term", 1),
    ]
    scores = affinity(signals)
    assert math.isclose(scores["a"], 0.5 + 0.2)
    assert math.isclose(scores["b"], 0.2)


def test_affinity_rejects_unknown_time_range():
    with pytest.raises(ValueError):
        affinity([TasteSignal("a", "all_time", 1)])


# ------------------------------------------------------------------- overlap


def test_shared_niche_artist_outweighs_shared_superstar():
    pool = {f"bg{n}": listens("star") for n in range(10)}
    pool["me"] = [TasteSignal("star", "long_term", 1), TasteSignal("niche", "long_term", 1)]
    pool["star_fan"] = listens("star")
    pool["niche_fan"] = listens("niche")
    engine = MatchEngine.build(pool)
    assert engine.overlap("me", "niche_fan") > engine.overlap("me", "star_fan")


def test_overlap_is_symmetric_and_bounded():
    engine = MatchEngine.build(indie_world())
    a_b = engine.overlap("alice", "comp")
    assert math.isclose(a_b, engine.overlap("comp", "alice"))
    assert 0.0 < a_b < 1.0
    assert math.isclose(engine.overlap("alice", "twin"), 1.0)


# ----------------------------------------------------------------- discovery


def test_identical_twin_offers_no_discovery():
    engine = MatchEngine.build(indie_world())
    assert engine.discovery("alice", "twin") == 0.0


def test_related_new_artists_beat_unrelated_ones():
    engine = MatchEngine.build(indie_world())
    assert engine.discovery("alice", "comp") > engine.discovery("alice", "jazzy")


def test_candidate_cannot_vouch_for_their_own_picks():
    # The only evidence that "z" goes with "x1" is the candidate's own profile.
    # Leave-one-out similarity must ignore it, so there is nothing to discover.
    pool = {"me": listens("x1", "x2"), "them": listens("x1", "z"), "other": listens("x2", "y")}
    engine = MatchEngine.build(pool)
    assert engine.discovery("me", "them") == 0.0


# ------------------------------------------------------------------- ranking


def test_complementary_listener_ranks_above_twin_and_stranger():
    engine = MatchEngine.build(indie_world())
    people = {"twin", "comp", "jazzy", "stranger"}
    matches = engine.matches_for("alice", candidates=people)
    ranked = [m.user_id for m in matches]
    assert ranked[0] == "comp"
    by_id = {m.user_id: m for m in matches}
    assert by_id["twin"].score == 0.0
    assert by_id["comp"].score > by_id.get("stranger", by_id["twin"]).score


def test_match_explains_itself():
    engine = MatchEngine.build(indie_world())
    comp = engine.matches_for("alice", candidates={"comp"})[0]
    assert set(comp.shared_artists) >= {"i1", "i2"}
    assert set(comp.they_could_put_you_on) <= {"i5", "i6"} and comp.they_could_put_you_on
    assert set(comp.you_could_put_them_on) <= {"i3", "i4"} and comp.you_could_put_them_on
    assert "star" not in comp.they_could_put_you_on


def test_each_suggestion_says_which_of_your_artists_it_goes_with():
    engine = MatchEngine.build(indie_world())
    comp = engine.matches_for("alice", candidates={"comp"})[0]
    assert set(comp.because_for_you) == set(comp.they_could_put_you_on)
    # One of alice's indie artists, never the superstar everyone plays.
    assert set(comp.because_for_you.values()) <= {"i1", "i2", "i3", "i4"}


def test_no_suggestions_means_no_reasons():
    engine = MatchEngine.build(indie_world())
    twin = engine.matches_for("alice", candidates={"twin"})[0]
    assert twin.they_could_put_you_on == [] and twin.because_for_you == {}


def test_fingerprint_scales_the_strongest_artist_to_one_in_a_stable_order():
    scores = {"a": 2.0, "b": 1.0, "c": 0.5}
    bars = fingerprint(scores)
    assert sorted(bars) == [0.25, 0.5, 1.0]
    assert fingerprint(dict(reversed(list(scores.items())))) == bars


def test_fingerprint_keeps_only_the_strongest_artists():
    scores = {f"a{n}": float(n) for n in range(1, 81)}
    bars = fingerprint(scores, size=50)
    assert len(bars) == 50
    assert min(bars) == round(31 / 80, 3)
    assert fingerprint({}) == []


def test_engine_reports_each_users_affinities():
    engine = MatchEngine.build(indie_world())
    assert engine.affinities("alice") == affinity(indie_world()["alice"])


def test_shared_artists_list_niche_before_superstar():
    engine = MatchEngine.build(indie_world())
    shared = engine.matches_for("alice", candidates={"twin"})[0].shared_artists
    assert shared[-1] == "star"


def test_mutual_mode_requires_both_people_to_gain():
    pool = indie_world()
    pool["fan"] = listens("i1", "i2", "i3", "i4", "i5", "i6", "star")  # alice has nothing new for them
    mutual = MatchEngine.build(pool).matches_for("alice", candidates={"fan"})[0]
    one_way = MatchEngine.build(pool, MatchConfig(mutual=False)).matches_for("alice", candidates={"fan"})[0]
    assert mutual.discovery_for_them == 0.0
    assert mutual.score == 0.0
    assert one_way.score > 0.0


def test_candidates_never_include_yourself_or_outsiders():
    engine = MatchEngine.build(indie_world())
    matches = engine.matches_for("alice", candidates={"alice", "comp", "nobody"})
    assert [m.user_id for m in matches] == ["comp"]


def test_stage_one_cut_keeps_highest_overlap():
    engine = MatchEngine.build(indie_world(), MatchConfig(candidate_pool_size=1))
    matches = engine.matches_for("alice", candidates={"twin", "comp", "jazzy"})
    assert [m.user_id for m in matches] == ["twin"]  # twin has the highest overlap


def test_min_overlap_filters_weak_candidates():
    engine = MatchEngine.build(indie_world(), MatchConfig(min_overlap=0.2))
    ids = {m.user_id for m in engine.matches_for("alice", candidates={"comp", "stranger"})}
    assert "stranger" not in ids


def test_user_with_no_signals_gets_no_matches_and_does_not_crash():
    pool = indie_world()
    pool["newbie"] = []
    engine = MatchEngine.build(pool)
    assert engine.matches_for("newbie") == []
    assert "newbie" not in {m.user_id for m in engine.matches_for("alice")}


def test_unknown_user_raises_key_error():
    engine = MatchEngine.build(indie_world())
    with pytest.raises(KeyError):
        engine.matches_for("ghost")


def test_k_limits_results():
    engine = MatchEngine.build(indie_world())
    assert len(engine.matches_for("alice", k=2)) == 2
