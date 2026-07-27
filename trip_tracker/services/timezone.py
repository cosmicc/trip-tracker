from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from trip_tracker.config import get_settings


def local_timezone() -> ZoneInfo:
    return ZoneInfo(get_settings().local_timezone)


def datetime_to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def datetime_to_local_date(value: datetime) -> date:
    return datetime_to_utc(value).astimezone(local_timezone()).date()


def datetime_to_local(value: datetime) -> datetime:
    return datetime_to_utc(value).astimezone(local_timezone())


def local_now() -> datetime:
    return datetime.now(UTC).astimezone(local_timezone())


def local_today() -> date:
    return local_now().date()


def local_day_bounds(day: date) -> tuple[datetime, datetime]:
    """Return UTC instants for one midnight-to-midnight local calendar day."""

    timezone = local_timezone()
    local_start = datetime.combine(day, time.min, tzinfo=timezone)
    next_day = day + timedelta(days=1)
    local_end = datetime.combine(next_day, time.min, tzinfo=timezone)
    return local_start.astimezone(UTC), local_end.astimezone(UTC)


def local_day_end_for_datetime(value: datetime) -> datetime:
    _, end = local_day_bounds(datetime_to_local_date(value))
    return end
