"""The engine on a realistic synthetic pool: do matches make musical sense?"""

from collections import Counter

from echo.demo import SCENES, SUPERSTARS, generate_listeners
from echo.matching import MatchEngine, TasteSignal


def demo_pool(count=150, seed=7):
    listeners = generate_listeners(count, seed)
    pool = {
        listener.name: [TasteSignal(a.name, a.time_range, a.rank) for a in listener.artists]
        for listener in listeners
    }
    return listeners, pool


def test_generation_is_deterministic():
    first = generate_listeners(20, seed=3)
    second = generate_listeners(20, seed=3)
    assert [x.artists for x in first] == [x.artists for x in second]


def test_every_listener_has_three_valid_time_ranges():
    for listener in generate_listeners(50):
        ranges = Counter(a.time_range for a in listener.artists)
        assert set(ranges) == {"long_term", "medium_term", "short_term"}
        long_term = [a for a in listener.artists if a.time_range == "long_term"]
        assert [a.rank for a in long_term] == list(range(1, len(long_term) + 1))
        assert len({a.name for a in long_term}) == len(long_term)  # no duplicates in a list


def test_superstars_are_common_and_get_low_idf():
    listeners, pool = demo_pool()
    engine = MatchEngine.build(pool)
    idf = dict(zip(engine.artists, engine.idf, strict=True))
    niche = SCENES["jazz"][-1]
    assert all(idf[star] < idf[niche] for star in SUPERSTARS if star in idf)


def test_top_matches_share_a_scene_with_you():
    listeners, pool = demo_pool()
    engine = MatchEngine.build(pool)
    by_name = {listener.name: listener for listener in listeners}

    def scenes(listener):
        return {listener.primary_scene, listener.secondary_scene} - {None}

    hits = total = 0
    for listener in listeners[:40]:
        matches = engine.matches_for(listener.name, k=3)
        for match in matches:
            total += 1
            hits += bool(scenes(listener) & scenes(by_name[match.user_id]))
    assert total > 0
    assert hits / total > 0.9


def test_suggested_artists_are_always_new_to_you():
    listeners, pool = demo_pool()
    engine = MatchEngine.build(pool)
    for listener in listeners[:20]:
        mine = {a.name for a in listener.artists}
        for match in engine.matches_for(listener.name, k=3):
            assert not set(match.they_could_put_you_on) & mine


def test_superstars_are_rarely_suggested_as_discoveries():
    listeners, pool = demo_pool()
    engine = MatchEngine.build(pool)
    suggestions = [
        artist
        for listener in listeners
        for match in engine.matches_for(listener.name, k=3)
        for artist in match.they_could_put_you_on
    ]
    share = sum(artist in SUPERSTARS for artist in suggestions) / len(suggestions)
    assert share < 0.1
