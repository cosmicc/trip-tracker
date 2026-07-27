from datetime import UTC, date, datetime

from trip_tracker.services.timezone import (
    datetime_to_local,
    datetime_to_local_date,
    local_day_bounds,
)


def test_datetime_to_local_uses_configured_detroit_timezone() -> None:
    utc_time = datetime(2026, 6, 12, 1, 30, tzinfo=UTC)

    local_time = datetime_to_local(utc_time)

    assert local_time.date() == date(2026, 6, 11)
    assert local_time.strftime("%H:%M %Z") == "21:30 EDT"
    assert datetime_to_local_date(utc_time) == date(2026, 6, 11)


def test_local_day_bounds_use_detroit_midnight_not_utc_midnight() -> None:
    summer_start, summer_end = local_day_bounds(date(2026, 6, 16))
    winter_start, winter_end = local_day_bounds(date(2026, 1, 16))

    assert summer_start == datetime(2026, 6, 16, 4, 0, tzinfo=UTC)
    assert summer_end == datetime(2026, 6, 17, 4, 0, tzinfo=UTC)
    assert winter_start == datetime(2026, 1, 16, 5, 0, tzinfo=UTC)
    assert winter_end == datetime(2026, 1, 17, 5, 0, tzinfo=UTC)
