from types import SimpleNamespace

from trip_tracker.services import gas_prices


def test_run_gas_snapshot_once_uses_isolated_database_session(monkeypatch) -> None:
    seen: list[object] = []
    monthly = SimpleNamespace(
        state="MI",
        year=2026,
        month=6,
        average_price_per_gallon="3.100",
    )

    class FakeSession:
        def __enter__(self) -> str:
            return "db-session"

        def __exit__(self, exc_type, exc, traceback) -> None:
            return None

    monkeypatch.setattr(gas_prices.database, "SessionLocal", FakeSession)

    def fake_refresh_current_monthly_price(db):
        seen.append(db)
        return monthly

    monkeypatch.setattr(
        gas_prices,
        "refresh_current_monthly_price",
        fake_refresh_current_monthly_price,
    )

    assert gas_prices.run_gas_snapshot_once() is monthly
    assert seen == ["db-session"]
