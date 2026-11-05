from datetime import timedelta

from conftest import T0, seed_token
from runner_research import db
from runner_research.features import Trade, close_at, context_features, price_features, trade_features
from runner_research.labels import Candle
from runner_research.pipeline import label_tokens


def test_norm_ts_dune_format():
    assert db.norm_ts("2026-08-01 12:00:00.000 UTC") == "2026-08-01T12:00:00+00:00"
    assert db.norm_ts("2026-08-01T12:00:00Z") == "2026-08-01T12:00:00+00:00"


def test_close_at_uses_only_closed_candles():
    c = [Candle(T0 + timedelta(minutes=5 * i), float(i + 1)) for i in range(30)]
    # candle starting at 55m closes at 60m -> index 11 -> close 12
    assert close_at(T0, c, 60) == 12.0
    f = price_features(T0, 1.0, c)
    assert f["mult_1h"] == 12.0 and f["mult_15m"] == 3.0 and f["grad_mcap"] == 1e9


def test_trade_features_ignore_after_1h_and_sniper_share():
    t = lambda m, who, side, usd: Trade(T0 + timedelta(minutes=m), who, side, usd)
    trades = [t(0.5, "sniper", "buy", 300), t(1.5, "dev", "buy", 100), t(10, "a", "buy", 400),
              t(20, "sniper", "buy", 200), t(30, "sniper", "sell", 500), t(59, "dev", "sell", 50),
              t(61, "late", "buy", 1_000_000)]
    f = trade_features(T0, trades, creator="dev")
    assert f["vol_1h"] == 1550 and f["traders_1h"] == 3
    # snipers = {sniper, dev}; their buys 300+100+200 of total buys 1000
    assert f["sniper_share"] == 0.6
    assert f["buy_sell_ratio"] == 1000 / 550
    assert f["creator_buy_usd_1h"] == 100 and f["creator_sell_usd_1h"] == 50


def test_creator_history_has_no_lookahead(conn):
    # earlier token by same creator graduated 2 days before: its 72h window is
    # still open at the new token's T0+1h, so its label must not count.
    seed_token(conn, "OLD", T0 - timedelta(days=2), [(0, 1), (1, 20), (3, 5)],
               liq=lambda h: 1e5 if h < 3 else 10)
    seed_token(conn, "OLDER", T0 - timedelta(days=5), [(0, 1), (1, 20), (3, 5)],
               liq=lambda h: 1e5 if h < 3 else 10)
    seed_token(conn, "NEW", T0, [(0, 1), (2, 3)])
    label_tokens(conn, now=T0 + timedelta(days=10))
    f = context_features(conn, "NEW")
    assert f["creator_prior_graduated"] == 2
    assert f["creator_prior_rugs"] == 1


def test_snapshot_after_cutoff_is_ignored(conn):
    seed_token(conn, "A", T0, [(0, 1)])
    db.upsert(conn, "snapshots", [{"mint": "A", "offset": "1h", "top10_pct": 20.0, "holders": 300,
              "target_at": (T0 + timedelta(hours=1)).isoformat(),
              "taken_at": (T0 + timedelta(hours=1, minutes=10)).isoformat(), "source": "test"}],
              key=("mint", "offset"))
    assert context_features(conn, "A")["top10_pct_1h"] is None
