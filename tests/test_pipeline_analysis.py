import json
import math
import random
from datetime import timedelta

import pytest

from conftest import T0, seed_token
from runner_research import analysis, collector, db
from runner_research.pipeline import label_tokens


def test_wilson_known_value():
    lo, hi = analysis.wilson(10, 100)
    assert lo == pytest.approx(0.0552, abs=1e-3) and hi == pytest.approx(0.1744, abs=1e-3)
    assert all(math.isnan(x) for x in analysis.wilson(0, 0))


def test_newcombe_contains_point():
    d, lo, hi = analysis.newcombe_diff(30, 50, 100, 1000)
    assert lo < d < hi and d == pytest.approx(0.5)


def test_label_job_idempotent_and_first_question(conn):
    seed_token(conn, "HOLD", T0, [(0, 1), (0.5, 5), (10, 8), (23, 6)])     # 5x@1h, holds
    seed_token(conn, "FADE", T0, [(0, 1), (0.5, 6), (2, 12), (5, 2)])      # 6x@1h, fades
    seed_token(conn, "DUD", T0, [(0, 1), (3, 1.2)])                         # never moves, holds flat
    now = T0 + timedelta(days=5)
    assert label_tokens(conn, now=now) == 3
    label_tokens(conn, now=now)
    assert conn.execute("SELECT COUNT(*) FROM labels").fetchone()[0] == 3
    labels = dict(conn.execute("SELECT mint, label FROM labels").fetchall())
    assert labels == {"HOLD": "slow_climb", "FADE": "spike_and_fade", "DUD": "dead"}
    feats = json.loads(conn.execute("SELECT features FROM labels WHERE mint='HOLD'").fetchone()[0])
    assert feats["mult_1h"] == 5.0 and feats["day_grad_count"] == 3

    q = analysis.first_question(conn)
    assert q["ge4x_1h"]["n"] == 2 and q["ge4x_1h"]["held"] == 1
    assert q["all"]["n"] == 3 and q["all"]["held"] == 2


def test_tokens_inside_72h_window_not_labeled(conn):
    seed_token(conn, "YOUNG", T0, [(0, 1)])
    assert label_tokens(conn, now=T0 + timedelta(hours=50)) == 0


def test_bucket_rates_and_missing_bucket():
    rows = [analysis.Row(f"m{i}", T0, "slow_climb" if i % 4 == 0 else "dead",
                         {"x": i if i < 80 else None}) for i in range(100)]
    b = analysis.bucket_rates(rows, "x", n_buckets=4)
    assert b["base_rate"] == 0.25
    assert sum(x["n"] for x in b["buckets"]) == 100
    assert b["buckets"][-1]["lo"] is None and b["buckets"][-1]["n"] == 20


def test_out_of_time_model_learns_signal():
    rng = random.Random(0)
    rows = []
    for i in range(600):
        x = rng.random()
        pos = rng.random() < (0.05 + 0.5 * x)
        rows.append(analysis.Row(f"m{i}", T0 + timedelta(hours=i), "slow_climb" if pos else "dead",
                                 {"signal": x, "noise": rng.random(), "sometimes": None if i % 3 else 1.0}))
    res = analysis.out_of_time_model(rows, split=T0 + timedelta(hours=400))
    assert res["train_n"] == 400 and res["test_n"] == 200
    assert res["test_auc"] > 0.7


def test_collector_due_and_lateness(conn):
    seed_token(conn, "A", T0, [(0, 1)])
    now = T0 + timedelta(hours=1, minutes=1)
    due = {(d.mint, d.offset) for d in collector.due_snapshots(conn, now)}
    assert due == {("A", "1h")}  # 15m is past the 30-min give-up window
    db.upsert(conn, "snapshots", [{"mint": "A", "offset": "1h", "target_at": (T0 + timedelta(hours=1)).isoformat(),
              "taken_at": now.isoformat(), "source": "test"}], key=("mint", "offset"))
    assert collector.due_snapshots(conn, now) == []
    assert collector.next_wakeup(conn, now) == T0 + timedelta(hours=6)
    assert collector.is_on_time(T0, T0 + timedelta(minutes=2))
    assert not collector.is_on_time(T0, T0 + timedelta(minutes=3))
