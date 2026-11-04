from runner_research import db
from runner_research.fakecheck import check_address, fake_pool_reasons


def test_wash_volume_rule():
    r = fake_pool_reasons(is_canonical=True, has_launchpad_record=True,
                          volume_24h_usd=6_000_000, liquidity_usd=100_000, distinct_traders_24h=30)
    assert r == ["wash_volume"]
    assert fake_pool_reasons(is_canonical=True, has_launchpad_record=True,
                             volume_24h_usd=6_000_000, liquidity_usd=100_000,
                             distinct_traders_24h=500) == []


def _seed(conn):
    db.upsert(conn, "tokens", [
        {"mint": "MINT_A", "name": "Ram Altman", "symbol": "RAM", "creator": "C1",
         "created_at": "2026-11-01T10:00:00+00:00", "graduated_at": "2026-11-01T11:00:00+00:00",
         "canonical_pool": "POOL_A", "source": "test"},
        {"mint": "MINT_COPY", "name": "ram altman", "symbol": "RAMX", "creator": "C2",
         "created_at": "2026-10-30T10:00:00+00:00", "graduated_at": None,
         "canonical_pool": None, "source": "test"},
    ], key=("mint",))
    db.upsert(conn, "pools", [
        {"pool": "POOL_A", "mint": "MINT_A", "dex": "pumpswap", "is_canonical": 1, "source": "test"},
        {"pool": "POOL_B", "mint": "MINT_A", "dex": "pumpswap", "is_canonical": 0, "source": "test"},
    ], key=("pool",))


def test_upsert_is_idempotent():
    conn = db.connect(":memory:")
    _seed(conn); _seed(conn)
    assert conn.execute("SELECT COUNT(*) FROM tokens").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM pools").fetchone()[0] == 2


def test_check_canonical_and_copy():
    conn = db.connect(":memory:")
    _seed(conn)
    r = check_address(conn, "POOL_A")
    assert r.canonical and not r.fake_flag and r.same_name_copies_7d == 1


def test_check_secondary_pool_is_fake():
    conn = db.connect(":memory:")
    _seed(conn)
    r = check_address(conn, "POOL_B")
    assert r.canonical is False and r.fake_reasons == ["not_canonical_pool"]


def test_check_unknown_mint():
    conn = db.connect(":memory:")
    r = check_address(conn, "UNKNOWN")
    assert not r.launchpad_record and r.fake_reasons == ["no_launchpad_record"]
