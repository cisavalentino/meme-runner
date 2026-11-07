from datetime import datetime, timedelta, timezone

from runner_research import strategy as S

T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)
GOOD = {"grad_mcap": 35000, "bonding_minutes": 12, "x_kind": "tweet", "tool": "J7 Tracker", "hot_copy": False,
        "m15": 1.1, "m60": 2.0, "max_jump_1h": 3.0}


def test_evaluate_rule():
    assert S.evaluate(GOOD) == (True, [])
    ok, why = S.evaluate({**GOOD, "bonding_minutes": 0, "x_kind": "none", "m60": 6.0, "max_jump_1h": 80})
    assert not ok and "graduation instan" in why and "tanpa link X" in why
    assert any("60m" in w for w in why) and any("lonjakan" in w for w in why)
    assert not S.evaluate({**GOOD, "grad_mcap": 50})[0] and not S.evaluate({**GOOD, "tool": "Irys"})[0]


def test_simulate_exits():
    after = [(T0, 1.0, 1.0), (T0, 1.5, 2.1), (T0, 0.4, 0.5)]           # (ts, close, high)
    assert S.simulate(after, 1.0, ((2.0, 1.0),), 0.6, False) == 2.0          # limit fills at the level on the high
    assert S.simulate(after, 1.0, ((3.0, 1.0),), 0.6, False) == 0.4          # stop on the close
    half = S.simulate(after, 1.0, ((2.0, 0.5), (5.0, 0.5)), 0.6, True)
    assert abs(half - (0.5 * 2.0 + 0.5 * 0.4)) < 1e-9                        # breakeven stop hit at the close
    assert S.simulate([(T0, 0.9, 0.9)], 1.0, (), 0.0, False) == 0.9           # hold to the end


def test_first_hour_features():
    rows = [(T0 + timedelta(minutes=5 * i), o, c) for i, (o, c) in enumerate([(1, 1.2), (1.2, 1.1), (1.1, 1.3)] + [(1.3, 2.0)] * 10)]
    f = S.first_hour(T0, rows)
    assert f["grad_price"] == 1 and f["m15"] == 1.3 and f["m60"] == 2.0 and f["entry"] == 2.0
    assert abs(f["max_jump_1h"] - 2.0 / 1.3) < 1e-9
