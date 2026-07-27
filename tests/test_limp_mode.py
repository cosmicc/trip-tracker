from trip_tracker.services.trip_processor import AutomaticTripProcessor


def test_automatic_trip_processor_pauses_when_database_is_unavailable(monkeypatch) -> None:
    opened_session = False

    def fail_session_open():
        nonlocal opened_session
        opened_session = True
        raise AssertionError("trip processor should not open a session while DB is unavailable")

    monkeypatch.setattr(
        "trip_tracker.services.trip_processor.database_is_reachable",
        lambda: False,
    )
    monkeypatch.setattr("trip_tracker.services.trip_processor.SessionLocal", fail_session_open)

    AutomaticTripProcessor()._process_once()

    assert opened_session is False
