from datetime import datetime, timedelta, timezone

import pytest

from runner_research.labels import Candle, label_path

T0 = datetime(2026, 11, 4, 0, 0, tzinfo=timezone.utc)
P0 = 1.0


def path(points, trades=20, liq=None):
    """points: list of (hours, close). Fills 5-minute candles by step interpolation."""
    out, pts = [], sorted(points)
    for i in range(72 * 12):
        h = i / 12
        close = [c for hh, c in pts if hh <= h][-1]
        l = liq(h) if liq else None
        out.append(Candle(T0 + timedelta(minutes=5 * i), close, trades, l))
    return out


def test_dead_low_peak():
    assert label_path(T0, P0, path([(0, 1.0), (1, 1.8), (10, 0.5)])).label == "dead"


def test_dead_no_trades_hour_24():
    c = [Candle(x.ts, x.close, 0 if 24 <= (x.ts - T0).total_seconds() / 3600 < 25 else 20)
         for x in path([(0, 1), (2, 6), (30, 6)])]
    assert label_path(T0, P0, c).label == "dead"


def test_rug_beats_everything():
    r = label_path(T0, P0, path([(0, 1), (1, 20), (3, 5)], liq=lambda h: 100_000 if h < 3 else 2_000))
    assert r.label == "rug"


def test_spike_and_fade():
    r = label_path(T0, P0, path([(0, 1), (2, 15), (4, 5), (12, 3)]))
    assert r.label == "spike_and_fade"
    assert r.peak_multiple == pytest.approx(15)
    assert r.hours_to_peak == pytest.approx(2)


def test_slow_climb():
    r = label_path(T0, P0, path([(0, 1), (4, 3), (15, 8), (20, 7)]))
    assert r.label == "slow_climb"
    assert r.close_24h_vs_peak == pytest.approx(7 / 8)


def test_second_wave():
    r = label_path(T0, P0, path([(0, 1), (1, 1.5), (5, 2.5), (10, 1.2), (30, 1.0), (60, 6.0)]))
    assert r.label == "second_wave"


def test_runner_other():
    # 6x within 6h, holds well (not a fade, peak too early for slow climb)
    assert label_path(T0, P0, path([(0, 1), (3, 6), (24, 5)])).label == "runner_other"


def test_flat():
    assert label_path(T0, P0, path([(0, 1), (3, 3), (24, 2.5)])).label == "flat"


def test_ignores_data_outside_window():
    c = path([(0, 1), (3, 1.5)]) + [Candle(T0 + timedelta(hours=80), 100, 50)]
    assert label_path(T0, P0, c).label == "dead"
