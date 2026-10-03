from app.audio import plan_chunks


def check(plan, duration, max_len=29.0):
    assert plan[0][0] == 0.0
    assert abs(plan[-1][1] - duration) < 1e-6
    for (a, b), (c, _) in zip(plan, plan[1:]):
        assert b == c, "chunks must be contiguous"
    for a, b in plan:
        assert 0 < b - a <= max_len + 1e-6


def test_short_audio_is_one_chunk():
    assert plan_chunks(20.0, []) == [(0.0, 20.0)]


def test_cuts_land_in_pauses():
    # A pause every 7 s (speech 6 s, silence 1 s).
    silences = [(t + 6.0, t + 7.0) for t in range(0, 300, 7)]
    plan = plan_chunks(300.0, silences)
    check(plan, 300.0)
    mids = {round(a + 0.5, 3) for a, _ in silences}
    for _, b in plan[:-1]:
        assert round(b, 3) in mids, f"cut at {b} is not in a pause"


def test_no_pauses_falls_back_to_hard_cuts():
    plan = plan_chunks(125.0, [])
    check(plan, 125.0)
    assert len(plan) == 5


def test_no_tiny_tail():
    plan = plan_chunks(51.0, [])
    check(plan, 51.0)
    assert plan[-1][1] - plan[-1][0] >= 3.0


def test_long_recording_stays_bounded():
    plan = plan_chunks(3 * 3600.0, [(t, t + 0.4) for t in range(5, 10800, 11)])
    check(plan, 3 * 3600.0)
