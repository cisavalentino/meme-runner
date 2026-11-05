"""FR7 — base rates, feature buckets, the first question, an out-of-time model.

Every run returns a `meta` block with rule version, config and data cut-off
so results are reproducible.
"""
from __future__ import annotations

import json
import math
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Sequence

from . import config
from .labels import LABELS, Candle, grid_t0
from .pipeline import graduation_price, load_candles

Z95 = 1.959963984540054


def wilson(k: int, n: int, z: float = Z95) -> tuple[float, float]:
    if n == 0:
        return (math.nan, math.nan)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def newcombe_diff(k1: int, n1: int, k2: int, n2: int, z: float = Z95) -> tuple[float, float, float]:
    """Difference p1 - p2 with Newcombe's hybrid score interval (method 10)."""
    p1, p2 = k1 / n1, k2 / n2
    l1, u1 = wilson(k1, n1, z)
    l2, u2 = wilson(k2, n2, z)
    d = p1 - p2
    return (d, d - math.sqrt((p1 - l1) ** 2 + (u2 - p2) ** 2), d + math.sqrt((u1 - p1) ** 2 + (p2 - l2) ** 2))


@dataclass
class Row:
    mint: str
    graduated_at: datetime
    label: str
    features: dict


def load_rows(conn: sqlite3.Connection, rule_version: str = config.RULE_VERSION) -> list[Row]:
    q = """SELECT l.mint, t.graduated_at, l.label, l.features FROM labels l
           JOIN tokens t USING (mint) WHERE l.rule_version = ? ORDER BY t.graduated_at"""
    return [Row(r["mint"], datetime.fromisoformat(r["graduated_at"]), r["label"], json.loads(r["features"] or "{}"))
            for r in conn.execute(q, (rule_version,))]


def iso_week(d: datetime) -> str:
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def base_rates_by_week(rows: Sequence[Row]) -> dict:
    by = defaultdict(Counter)
    for r in rows:
        by[iso_week(r.graduated_at)][r.label] += 1
    out = {}
    for wk in sorted(by):
        n = sum(by[wk].values())
        out[wk] = {"n": n, **{l: by[wk][l] / n for l in LABELS}}
    return out


def is_positive(label: str) -> bool:
    return label in config.POSITIVE_LABELS


def bucket_rates(rows: Sequence[Row], feature: str, n_buckets: int = 5) -> dict:
    """Positive-label rate per quantile bucket of `feature`, vs. the base rate.
    Rows missing the feature form their own bucket (missingness can be a signal)."""
    base_k = sum(is_positive(r.label) for r in rows)
    base = base_k / len(rows) if rows else math.nan
    have = sorted((r.features[feature], is_positive(r.label)) for r in rows
                  if r.features.get(feature) is not None)
    missing = [is_positive(r.label) for r in rows if r.features.get(feature) is None]
    buckets = []
    if have:
        size = len(have) / n_buckets
        for i in range(n_buckets):
            part = have[round(i * size):round((i + 1) * size)]
            if not part:
                continue
            k, n = sum(p for _, p in part), len(part)
            lo, hi = wilson(k, n)
            buckets.append({"lo": part[0][0], "hi": part[-1][0], "n": n, "rate": k / n,
                            "ci95": [lo, hi], "lift": (k / n) / base if base else None})
    if missing:
        k, n = sum(missing), len(missing)
        buckets.append({"lo": None, "hi": None, "n": n, "rate": k / n, "ci95": list(wilson(k, n)),
                        "lift": (k / n) / base if base else None})
    return {"feature": feature, "base_rate": base, "base_n": len(rows), "buckets": buckets}


def q1_outcome(t0: datetime, p0: float, candles: Sequence[Candle]) -> tuple[float | None, bool | None]:
    """(multiple at 1h, still >= 50% of the 0–24h peak at 24h). Peak at 24h is
    what was known at 24h, not the 72h peak."""
    from .features import close_at
    m1 = close_at(t0, candles, 60)
    c24 = close_at(t0, candles, 24 * 60)
    end = t0 + timedelta(hours=24)
    window = [c.close for c in candles if t0 <= c.ts and c.ts + timedelta(minutes=5) <= end]
    if m1 is None or c24 is None or not window:
        return (m1 / p0 if m1 else None, None)
    return (m1 / p0, c24 >= config.Q1_MIN_FRAC_OF_PEAK_24H * max(window))


def first_question(conn: sqlite3.Connection, start: datetime | None = None, end: datetime | None = None,
                   now: datetime | None = None) -> dict:
    """Only tokens whose first 24h had fully elapsed by `now` (default: wall clock)
    count; younger tokens would otherwise use their latest close as close@24h."""
    now = now or datetime.now(timezone.utc)
    q = "SELECT mint, canonical_pool, graduated_at FROM tokens WHERE canonical_pool IS NOT NULL"
    toks = conn.execute(q).fetchall()
    allk = alln = gk = gn = 0
    for t in toks:
        t0 = datetime.fromisoformat(t["graduated_at"])
        if (start and t0 < start) or (end and t0 >= end) or t0 + timedelta(hours=24) > now:
            continue
        t0 = grid_t0(t0)
        p0 = graduation_price(conn, t["canonical_pool"])
        if not p0:
            continue
        m1, held = q1_outcome(t0, p0, load_candles(conn, t["canonical_pool"]))
        if held is None:
            continue
        alln += 1; allk += held
        if m1 >= config.Q1_MIN_MULT_1H:
            gn += 1; gk += held
    res = {"definition": f">= {config.Q1_MIN_MULT_1H}x at 1h; held = close@24h >= "
                         f"{config.Q1_MIN_FRAC_OF_PEAK_24H:.0%} of 0–24h peak",
           "all": {"n": alln, "held": allk, "rate": allk / alln if alln else None, "ci95": wilson(allk, alln)},
           "ge4x_1h": {"n": gn, "held": gk, "rate": gk / gn if gn else None, "ci95": wilson(gk, gn)}}
    if gn and alln:
        d, lo, hi = newcombe_diff(gk, gn, allk, alln)
        res["difference"] = {"value": d, "ci95": [lo, hi]}
    return res


def _matrix(rows: Sequence[Row], names: list[str]):
    import numpy as np
    X = np.array([[r.features.get(f) if r.features.get(f) is not None else np.nan for f in names]
                  for r in rows], dtype=float)
    y = np.array([is_positive(r.label) for r in rows], dtype=int)
    return X, y


def out_of_time_model(rows: Sequence[Row], split: datetime, feature_names: list[str] | None = None) -> dict:
    """Train on graduations before `split`, test on those after. Logistic
    regression with median imputation and missing-value indicators."""
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import average_precision_score, roc_auc_score
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    train = [r for r in rows if r.graduated_at < split]
    test = [r for r in rows if r.graduated_at >= split]
    names = feature_names or sorted({k for r in train for k, v in r.features.items()
                                     if isinstance(v, (int, float)) and not isinstance(v, bool)})
    Xtr, ytr = _matrix(train, names)
    Xte, yte = _matrix(test, names)
    model = make_pipeline(SimpleImputer(strategy="median", add_indicator=True, keep_empty_features=True),
                          StandardScaler(), LogisticRegression(max_iter=2000, class_weight="balanced"))
    out = {"model": "logreg", "split": split.isoformat(), "features": names,
           "train_n": len(train), "train_pos": int(ytr.sum()), "test_n": len(test), "test_pos": int(yte.sum())}
    if len(set(ytr)) < 2:
        out["skipped"] = "training set has a single class"
        return out
    model.fit(Xtr, ytr)
    if len(set(yte)) == 2:
        p = model.predict_proba(Xte)[:, 1]
        out["test_auc"] = float(roc_auc_score(yte, p))
        out["test_avg_precision"] = float(average_precision_score(yte, p))
        out["test_base_rate"] = float(yte.mean())
    return out


def meta(conn: sqlite3.Connection) -> dict:
    r = conn.execute("SELECT MAX(graduated_at) m, MIN(graduated_at) n FROM tokens").fetchone()
    return {"rule_version": config.RULE_VERSION, "label_rules": config.DEFAULT_LABEL_RULES.as_dict(),
            "data_from": r["n"], "data_cutoff": r["m"], "positive_labels": list(config.POSITIVE_LABELS)}
