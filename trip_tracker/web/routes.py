import logging
import re
import shutil
from calendar import month_name
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from math import ceil
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError
from sqlalchemy.orm import Session, joinedload

from trip_tracker import __version__ as APP_VERSION
from trip_tracker.config import get_settings
from trip_tracker.database import get_db
from trip_tracker.models import (
    AUTOMATIC_TRIP_PROCESSING_CHECKPOINT,
    Base,
    CloudflareIPBlock,
    DeletedTrip,
    GasPriceSnapshot,
    HiddenLoginFailure,
    MonthlyGasPrice,
    MonthlyReportExpense,
    OwnTracksLocation,
    PasskeyCredential,
    Site,
    Trip,
    TripProcessingCheckpoint,
)
from trip_tracker.services.app_health import (
    build_app_health_snapshot,
    measure_database_latency_ms,
)
from trip_tracker.services.backups import (
    BACKUP_MEDIA_TYPE,
    AutomaticBackupFile,
    BackupValidationError,
    create_automatic_backup,
    create_full_backup,
    list_automatic_backup_files,
    read_automatic_backup_content,
    restore_full_backup,
)
from trip_tracker.services.cloudflare_blocks import (
    CloudflareBlockError,
    cloudflare_ip_blocking_configured,
    create_cloudflare_ip_block,
    delete_cloudflare_ip_block,
    ip_is_allowlisted,
    normalize_ip_address,
)
from trip_tracker.services.diagnostics import (
    owntracks_entry_event_label,
    owntracks_entry_received_delay_display,
    owntracks_movement_diagnostics,
    paginated_owntracks_entries,
)
from trip_tracker.services.gas_prices import (
    EiaSeriesProvider,
    GasPriceUnavailable,
    get_or_create_monthly_price,
    refresh_current_monthly_price,
)
from trip_tracker.services.login_failures import (
    login_audit_json_lines,
    record_web_login_failure,
    record_web_login_success,
    tail_login_failure_entries,
    tail_login_success_entries,
)
from trip_tracker.services.mileage import (
    HOME_WAYPOINT_NAME,
    MANUAL_TRIP_NOTE,
    MILEAGE_SOURCE_MANUAL,
    TripOdometerAdjustmentError,
    create_manual_trip,
    delete_trip,
    emergency_rebuild_trip_odometers_to_manual_reading,
    mark_trip_user_edited,
    monthly_miles,
    owntracks_segment_miles,
    resequence_month_trip_odometers_from,
    site_indexes,
)
from trip_tracker.services.owntracks_rollups import (
    owntracks_monthly_event_count,
    owntracks_monthly_total_miles,
)
from trip_tracker.services.passkeys import (
    PasskeyCeremonyError,
    begin_passkey_authentication,
    begin_passkey_registration,
    finish_passkey_authentication,
    finish_passkey_registration,
    list_passkeys,
    passkey_login_available,
)
from trip_tracker.services.pdf import (
    calculate_reimbursement,
    calculate_reimbursement_gallons,
    extra_expense_total,
    generate_monthly_pdf,
)
from trip_tracker.services.runtime_status import build_runtime_status
from trip_tracker.services.timezone import (
    datetime_to_local,
    datetime_to_utc,
    local_day_bounds,
    local_now,
    local_today,
)
from trip_tracker.services.trip_processor import update_odometer_anchor_from_manual_reading
from trip_tracker.services.waypoints import owntracks_waypoints_json
from trip_tracker.web.auth import (
    PUBLIC_DEVICE_IDLE_TIMEOUT_SECONDS,
    add_public_device_clear_site_data,
    authenticate_web_credentials,
    clear_login_failures,
    clear_request_authentication,
    login_client_key,
    login_failure_state,
    login_is_locked,
    login_lockout_remaining_seconds,
    mark_request_authenticated,
    record_login_failure,
    record_public_device_activity,
    request_is_authenticated,
    request_is_public_device,
    valid_next_path,
    web_login_enabled,
)

router = APIRouter()
logger = logging.getLogger(__name__)
WEB_DIR = Path(__file__).resolve().parent
STATIC_DIR = WEB_DIR / "static"
ICON_DIR = STATIC_DIR / "icons"
templates = Jinja2Templates(directory=[WEB_DIR / "templates", WEB_DIR / "static"])
DIAGNOSTICS_TABLE_PAGE_SIZE = 10
DIAGNOSTICS_STATE_CHANGE_LIMIT = 500
DIAGNOSTICS_LOGIN_FAILURE_MAX_ENTRIES = 200
DIAGNOSTICS_LOGIN_SUCCESS_MAX_ENTRIES = 200
MONTHLY_REPORT_EXPENSE_LIMIT = 5


def _format_local_datetime(value, fmt: str = "%Y-%m-%d %I:%M:%S %p %Z") -> str:
    if value is None:
        return ""
    return datetime_to_local(value).strftime(fmt)


def _format_odometer(value) -> str:
    if value is None:
        return "-"
    return f"{Decimal(value):,.1f}"


def _format_comma_number(value, decimal_places: int | None = None) -> str:
    """Return a display-only number with thousands separators."""

    if value is None:
        return "-"
    numeric_value = Decimal(str(value))
    if decimal_places is None:
        if numeric_value == numeric_value.to_integral_value():
            return f"{int(numeric_value):,}"
        return f"{numeric_value:,}"
    places = max(int(decimal_places), 0)
    return f"{numeric_value:,.{places}f}"


def _format_truncated_one_decimal(value: Decimal) -> str:
    """Return a decimal truncated to one displayed decimal place."""

    return f"{Decimal(value).quantize(Decimal('0.1'), rounding=ROUND_DOWN):.1f}"


def _format_odometer_source(value) -> str:
    labels = {
        "estimated": "Estimated",
        "previous_trip": "Previous trip",
        "manual": "Manual",
        "manual_odometer": "Manual odometer",
        "owntracks_path": "OwnTracks path",
        "owntracks_rolling": "OwnTracks rolling",
        "owntracks_estimate": "OwnTracks estimate",
        "estimated_odometer": "Estimated",
        "waypoint_distance": "Waypoint distance",
    }
    if value is None:
        return "-"
    return labels.get(str(value), str(value).replace("_", " ").title())


templates.env.filters["local_datetime"] = _format_local_datetime
templates.env.filters["odometer"] = _format_odometer
templates.env.filters["comma_number"] = _format_comma_number
templates.env.filters["odometer_source"] = _format_odometer_source
templates.env.filters["owntracks_entry_event_label"] = owntracks_entry_event_label
templates.env.filters["owntracks_entry_received_delay"] = owntracks_entry_received_delay_display
templates.env.globals["request_is_authenticated"] = request_is_authenticated
templates.env.globals["request_is_public_device"] = request_is_public_device
templates.env.globals["web_login_enabled"] = web_login_enabled
templates.env.globals["app_version"] = APP_VERSION
templates.env.globals["public_device_idle_timeout_seconds"] = (
    PUBLIC_DEVICE_IDLE_TIMEOUT_SECONDS
)
WAYPOINT_PAGE_SIZE = 20
DISTANCE_PRECISION = Decimal("0.1")


@router.get("/manifest.webmanifest", include_in_schema=False)
def web_manifest() -> FileResponse:
    """Serve the installable web-app manifest from a root URL for phone browsers."""

    return FileResponse(
        STATIC_DIR / "manifest.webmanifest",
        media_type="application/manifest+json",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/site.webmanifest", include_in_schema=False)
def site_manifest() -> FileResponse:
    """Serve the same manifest at the common fallback path used by some browsers."""

    return web_manifest()


@router.get("/service-worker.js", include_in_schema=False)
def service_worker() -> FileResponse:
    """Serve the install service worker without caching sensitive app responses."""

    return FileResponse(
        STATIC_DIR / "service-worker.js",
        media_type="text/javascript; charset=utf-8",
        headers={
            "Cache-Control": "no-store",
            "Service-Worker-Allowed": "/",
        },
    )


@router.get("/favicon.ico", include_in_schema=False)
def favicon() -> FileResponse:
    """Serve the launcher icon as the browser favicon at the standard root path."""

    return FileResponse(
        ICON_DIR / "favicon.ico",
        media_type="image/x-icon",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/apple-touch-icon.png", include_in_schema=False)
def apple_touch_icon() -> FileResponse:
    """Serve the iOS home-screen icon at Apple's default discovery path."""

    return FileResponse(
        ICON_DIR / "trip-tracker-apple-touch-icon.png",
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )


@dataclass(frozen=True)
class ApiTestResult:
    status: str
    message: str
    value: str = ""

    @property
    def passed(self) -> bool:
        return self.status == "pass"


@dataclass(frozen=True)
class DiagnosticAutomaticBackup:
    """Automatic backup metadata rendered on the Diagnostics page."""

    filename: str
    created_at_display: str
    source_label: str
    source_css_class: str
    size_display: str
    download_url: str


@dataclass(frozen=True)
class DiagnosticDiskUsage:
    """One logical disk usage row for Diagnostics, possibly covering several paths."""

    paths: tuple[str, ...]
    inspected_path: str
    total_bytes: int
    used_bytes: int
    free_bytes: int
    total_display: str
    used_display: str
    free_display: str
    used_percent_display: str
    used_percent_style: str

    @property
    def primary_path(self) -> str:
        return self.paths[0] if self.paths else self.inspected_path


@dataclass(frozen=True)
class DiagnosticDatabaseSummary:
    """Database storage and row totals rendered in the Diagnostics drive-space card."""

    size_bytes: int | None
    size_display: str
    total_records: int
    total_records_display: str


@dataclass(frozen=True)
class DiagnosticDatabaseStats:
    """Safe database health details rendered in the Diagnostics System Status card."""

    latency_ms: float | None
    latency_display: str
    latency_indicator_class: str
    size_display: str
    total_records_display: str
    pool_display: str
    timeout_display: str


@dataclass(frozen=True)
class DiagnosticGasPriceExtremes:
    """Gas price readings rendered in the Diagnostics Data card."""

    lowest_price_per_gallon: Decimal | None
    current_price_per_gallon: Decimal | None
    monthly_average_price_per_gallon: Decimal | None
    highest_price_per_gallon: Decimal | None

    @property
    def lowest_display(self) -> str:
        return _format_gas_price(self.lowest_price_per_gallon)

    @property
    def current_display(self) -> str:
        return _format_gas_price(self.current_price_per_gallon)

    @property
    def monthly_average_display(self) -> str:
        return _format_gas_price(self.monthly_average_price_per_gallon)

    @property
    def highest_display(self) -> str:
        return _format_gas_price(self.highest_price_per_gallon)


def _current_year_month() -> tuple[int, int]:
    today = local_today()
    return _year_month_for_local_date(today)


def _year_month_for_local_date(today: date) -> tuple[int, int]:
    """Return dashboard calendar selectors for an already-resolved local date."""

    return today.year, today.month


def _validate_month(month: int) -> None:
    if month < 1 or month > 12:
        raise HTTPException(status_code=400, detail="month must be 1 through 12")


def _parse_month_input(value: str) -> tuple[int, int]:
    """Parse a browser month input value in YYYY-MM format."""

    try:
        parsed_date = datetime.strptime(value.strip(), "%Y-%m").date()
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="selected_month must use YYYY-MM format",
        ) from exc
    return parsed_date.year, parsed_date.month


def _quantize_distance(value: Decimal) -> Decimal:
    """Round dashboard distance totals to the displayed one-decimal precision."""

    return Decimal(value).quantize(DISTANCE_PRECISION, rounding=ROUND_HALF_UP)


def _distance_components(total_distance: Decimal, trip_distance: Decimal) -> dict[str, Decimal]:
    """Return dashboard distance components with a non-negative non-trip remainder."""

    trip_total = _quantize_distance(trip_distance)
    combined_total = max(_quantize_distance(total_distance), trip_total)
    non_trip_total = _quantize_distance(max(combined_total - trip_total, Decimal("0.0")))
    return {
        "total": _quantize_distance(trip_total + non_trip_total),
        "trips": trip_total,
        "non_trips": non_trip_total,
    }


def _month_date_bounds(year: int, month: int) -> tuple[date, date]:
    """Return inclusive and exclusive local dates for a dashboard month."""

    start_date = date(year, month, 1)
    end_date = date(year + int(month == 12), 1 if month == 12 else month + 1, 1)
    return start_date, end_date


def _month_datetime_bounds(year: int, month: int) -> tuple[datetime, datetime]:
    """Return UTC datetime bounds for one complete local dashboard month."""

    start_date, end_date = _month_date_bounds(year, month)
    start_dt, _ = local_day_bounds(start_date)
    end_dt, _ = local_day_bounds(end_date)
    return start_dt, end_dt


def _trip_miles_for_date_range(db: Session, start_date: date, end_date: date) -> Decimal:
    """Sum stored trip miles for a half-open local date range."""

    total = db.scalar(
        select(func.coalesce(func.sum(Trip.miles), 0))
        .where(Trip.trip_date >= start_date)
        .where(Trip.trip_date < end_date)
    )
    return _quantize_distance(Decimal(str(total or "0.0")))


def _trip_count_for_date_range(db: Session, start_date: date, end_date: date) -> int:
    """Count trips in a half-open local date range."""

    return int(
        db.scalar(
            select(func.count(Trip.id))
            .where(Trip.trip_date >= start_date)
            .where(Trip.trip_date < end_date)
        )
        or 0
    )


def _monday_week_date_bounds(today: date) -> tuple[date, date]:
    """Return the Monday-Sunday app-local week containing today."""

    week_start = today - timedelta(days=today.weekday())
    return week_start, week_start + timedelta(days=7)


def _dashboard_work_trip_counts(
    db: Session,
    *,
    today: date,
    year: int,
    month: int,
) -> dict[str, int]:
    """Return Work Trips counts for the Dashboard count card."""

    tomorrow = today + timedelta(days=1)
    week_start, week_end = _monday_week_date_bounds(today)
    month_start, month_end = _month_date_bounds(year, month)
    return {
        "today": _trip_count_for_date_range(db, today, tomorrow),
        "week": _trip_count_for_date_range(db, week_start, week_end),
        "month": _trip_count_for_date_range(db, month_start, month_end),
    }


def _owntracks_location_before(
    db: Session,
    before_dt: datetime,
) -> OwnTracksLocation | None:
    """Return the latest OwnTracks row before a UTC boundary."""

    return db.scalar(
        select(OwnTracksLocation)
        .where(OwnTracksLocation.captured_at < before_dt)
        .order_by(OwnTracksLocation.captured_at.desc(), OwnTracksLocation.id.desc())
        .limit(1)
    )


def _owntracks_locations_in_range(
    db: Session,
    start_dt: datetime,
    end_dt: datetime,
) -> list[OwnTracksLocation]:
    """Return OwnTracks rows inside a UTC range in chronological order."""

    return list(
        db.scalars(
            select(OwnTracksLocation)
            .where(OwnTracksLocation.captured_at >= start_dt)
            .where(OwnTracksLocation.captured_at < end_dt)
            .order_by(OwnTracksLocation.captured_at.asc(), OwnTracksLocation.id.asc())
        )
    )


def _owntracks_path_locations_for_datetime_range(
    db: Session,
    start_dt: datetime,
    end_dt: datetime,
) -> list[OwnTracksLocation]:
    """Return path rows used to count segments ending inside the UTC range."""

    previous_location = _owntracks_location_before(db, start_dt)
    locations = _owntracks_locations_in_range(db, start_dt, end_dt)
    if previous_location is None:
        return locations
    return [previous_location, *locations]


def _owntracks_total_miles_for_datetime_range(
    db: Session,
    start_dt: datetime,
    end_dt: datetime,
) -> Decimal:
    """Calculate total driven distance directly from OwnTracks coordinate points."""

    path_locations = _owntracks_path_locations_for_datetime_range(db, start_dt, end_dt)
    if len(path_locations) < 2:
        return Decimal("0.0")

    sites = list(db.scalars(select(Site).where(Site.active.is_(True)).order_by(Site.name.asc())))
    sites_by_name, sites_by_region_id = site_indexes(sites)
    total_miles = Decimal("0.0")
    previous_location = path_locations[0]
    for location in path_locations[1:]:
        total_miles += owntracks_segment_miles(
            previous_location,
            location,
            sites,
            sites_by_name,
            sites_by_region_id,
        )
        previous_location = location

    return _quantize_distance(total_miles)


def _dashboard_distance_summary(db: Session, *, today: date, year: int, month: int) -> dict:
    """Return dashboard distance totals for today and the current month."""

    tomorrow = today + timedelta(days=1)
    today_start_dt, today_end_dt = local_day_bounds(today)
    today_components = _distance_components(
        _owntracks_total_miles_for_datetime_range(
            db,
            today_start_dt,
            today_end_dt,
        ),
        _trip_miles_for_date_range(db, today, tomorrow),
    )
    month_components = _monthly_distance_components(db, year=year, month=month)
    return {
        "today_total": today_components["total"],
        "today_trips": today_components["trips"],
        "today_non_trips": today_components["non_trips"],
        "month_total": month_components["total"],
        "month_trips": month_components["trips"],
        "month_non_trips": month_components["non_trips"],
    }


def _monthly_distance_components(db: Session, *, year: int, month: int) -> dict[str, Decimal]:
    """Return selected-month distance components for Trips and Dashboard cards."""

    month_start_date, month_end_date = _month_date_bounds(year, month)
    return _distance_components(
        owntracks_monthly_total_miles(db, year=year, month=month),
        _trip_miles_for_date_range(db, month_start_date, month_end_date),
    )


def _owntracks_event_count_for_datetime_range(
    db: Session,
    start_dt: datetime,
    end_dt: datetime,
) -> int:
    """Count OwnTracks rows captured inside a half-open UTC datetime range."""

    return int(
        db.scalar(
            select(func.count(OwnTracksLocation.id))
            .where(OwnTracksLocation.captured_at >= start_dt)
            .where(OwnTracksLocation.captured_at < end_dt)
        )
        or 0
    )


def _trips_month_summary(
    db: Session,
    *,
    year: int,
    month: int,
    trip_count: int,
) -> dict:
    """Return compact selected-month metrics shown above the Trips add form."""

    settings = get_settings()
    monthly_gas = db.scalar(
        select(MonthlyGasPrice)
        .where(MonthlyGasPrice.year == year)
        .where(MonthlyGasPrice.month == month)
        .where(MonthlyGasPrice.state == settings.gas_price_state.upper())
        .order_by(MonthlyGasPrice.updated_at.desc(), MonthlyGasPrice.id.desc())
        .limit(1)
    )
    distance_components = _monthly_distance_components(db, year=year, month=month)
    reimbursement_summary = _dashboard_reimbursement_summary(
        db,
        year=year,
        month=month,
        monthly_gas=monthly_gas,
        vehicle_mpg=settings.vehicle_mpg,
    )
    return {
        "month_total": distance_components["total"],
        "month_trips": distance_components["trips"],
        "month_non_trips": distance_components["non_trips"],
        "owntracks_event_count": owntracks_monthly_event_count(db, year=year, month=month),
        "trip_count": trip_count,
        "reimbursement": reimbursement_summary,
        "monthly_gas": monthly_gas,
        "monthly_gas_message": "" if monthly_gas is not None else "No monthly price",
    }


def _dashboard_reimbursement_summary(
    db: Session,
    *,
    year: int,
    month: int,
    monthly_gas: MonthlyGasPrice | None,
    vehicle_mpg: Decimal,
) -> dict[str, Decimal | str | None]:
    """Return the current-month reimbursement total using the same math as the PDF report."""

    total_miles = monthly_miles(db, year, month)
    reimbursement_gallons = calculate_reimbursement_gallons(total_miles, vehicle_mpg)
    expense_total = extra_expense_total(_monthly_report_expenses(db, year=year, month=month))
    if monthly_gas is None:
        return {
            "total": None,
            "mileage_total": None,
            "expense_total": expense_total,
            "total_miles": total_miles,
            "reimbursement_gallons": reimbursement_gallons,
            "reimbursement_gallons_display": _format_truncated_one_decimal(
                reimbursement_gallons
            ),
        }
    mileage_total = calculate_reimbursement(
        total_miles,
        monthly_gas.average_price_per_gallon,
        vehicle_mpg,
    )
    return {
        "total": mileage_total + expense_total,
        "mileage_total": mileage_total,
        "expense_total": expense_total,
        "total_miles": total_miles,
        "reimbursement_gallons": reimbursement_gallons,
        "reimbursement_gallons_display": _format_truncated_one_decimal(reimbursement_gallons),
    }


def _resolve_selected_trips_month(
    *,
    year: int | None,
    month: int | None,
    selected_month: str | None,
) -> tuple[int, int]:
    """Return the selected Trips page year and month from query parameters."""

    if selected_month:
        resolved_year, resolved_month = _parse_month_input(selected_month)
    elif year is None or month is None:
        resolved_year, resolved_month = _current_year_month()
    else:
        resolved_year, resolved_month = year, month
    _validate_month(resolved_month)
    return resolved_year, resolved_month


def _trips_template_context(db: Session, *, year: int, month: int) -> dict[str, object]:
    """Build the selected-month Trips template context."""

    start = date(year, month, 1)
    end = date(year + int(month == 12), 1 if month == 12 else month + 1, 1)
    stmt = (
        select(Trip)
        .options(joinedload(Trip.origin_site), joinedload(Trip.destination_site))
        .where(Trip.trip_date >= start)
        .where(Trip.trip_date < end)
        .order_by(Trip.trip_date.desc(), Trip.started_at.desc(), Trip.id.desc())
    )
    all_trips = list(db.scalars(stmt))
    monthly_report_expenses = _monthly_report_expenses(db, year=year, month=month)
    trips_summary = _trips_month_summary(
        db,
        year=year,
        month=month,
        trip_count=len(all_trips),
    )
    waypoints = _waypoints_for_trip_forms(db)
    suppressed_trips = list(
        db.scalars(
            select(DeletedTrip)
            .where(DeletedTrip.trip_date >= start)
            .where(DeletedTrip.trip_date < end)
            .order_by(DeletedTrip.trip_date.asc(), DeletedTrip.started_at.asc())
        )
    )
    return {
        "trips": all_trips,
        "year": year,
        "month": month,
        "selected_month_value": f"{year}-{month:02d}",
        "selected_month_display": f"{month_name[month]} {year} ({month:02d}/{year})",
        "today": local_today(),
        "expense_default_date": _expense_default_date(year, month),
        "monthly_report_expenses": monthly_report_expenses,
        "monthly_report_expense_total": extra_expense_total(monthly_report_expenses),
        "monthly_report_expense_limit": MONTHLY_REPORT_EXPENSE_LIMIT,
        "monthly_report_expense_slots_remaining": max(
            MONTHLY_REPORT_EXPENSE_LIMIT - len(monthly_report_expenses),
            0,
        ),
        "trips_summary": trips_summary,
        "waypoints": waypoints,
        "waypoint_names": [waypoint.name for waypoint in waypoints],
        "suppressed_trips": suppressed_trips,
        "manual_mileage_source": MILEAGE_SOURCE_MANUAL,
        "manual_trip_note": MANUAL_TRIP_NOTE,
    }


def _dashboard_location_state(movement_state) -> dict[str, str]:
    """Return compact current-location state text for the Dashboard card."""

    if movement_state.state == "waypoint":
        return {
            "state": "waypoint",
            "label": "Inside waypoint",
            "detail": movement_state.site_name or "Saved waypoint",
        }
    if movement_state.state == "travel":
        detail = "Moving away from saved waypoints"
        if movement_state.distance_miles is not None:
            detail = f"Last movement {movement_state.distance_miles:.1f} miles"
        return {"state": "travel", "label": "Driving", "detail": detail}
    if movement_state.state == "away":
        return {
            "state": "stationary",
            "label": "Stationary",
            "detail": "Away from saved waypoints",
        }
    return {
        "state": "unknown",
        "label": "No OwnTracks data",
        "detail": "Waiting for phone location",
    }


def _pagination_context(total: int, page: int, page_size: int) -> dict[str, int | bool]:
    total_pages = max(1, ceil(total / page_size))
    current_page = min(max(page, 1), total_pages)
    return {
        "page": current_page,
        "page_size": page_size,
        "total": total,
        "total_pages": total_pages,
        "first_item": ((current_page - 1) * page_size) + 1 if total else 0,
        "last_item": min(current_page * page_size, total),
        "has_previous": current_page > 1,
        "has_next": current_page < total_pages,
    }


def _paginate_items(
    items: list,
    *,
    page: int,
    page_size: int,
) -> tuple[list, dict[str, int | bool]]:
    pagination = _pagination_context(len(items), page, page_size)
    start = (int(pagination["page"]) - 1) * int(pagination["page_size"])
    end = start + int(pagination["page_size"])
    return items[start:end], pagination


def _monthly_gas_context(db: Session, year: int, month: int) -> tuple[MonthlyGasPrice | None, str]:
    try:
        return get_or_create_monthly_price(db, year, month), ""
    except GasPriceUnavailable as exc:
        return None, str(exc)
    except Exception as exc:
        return None, f"Could not load gas price: {exc}"


def _monthly_report_expenses(
    db: Session,
    *,
    year: int,
    month: int,
) -> list[MonthlyReportExpense]:
    """Return manual extra expenses for a selected report month."""

    return list(
        db.scalars(
            select(MonthlyReportExpense)
            .where(MonthlyReportExpense.year == year)
            .where(MonthlyReportExpense.month == month)
            .order_by(
                MonthlyReportExpense.expense_date.asc(),
                MonthlyReportExpense.created_at.asc(),
                MonthlyReportExpense.id.asc(),
            )
        )
    )


def _monthly_report_expense_count(db: Session, *, year: int, month: int) -> int:
    """Return the number of manual extra expense rows in one report month."""

    return int(
        db.scalar(
            select(func.count(MonthlyReportExpense.id))
            .where(MonthlyReportExpense.year == year)
            .where(MonthlyReportExpense.month == month)
        )
        or 0
    )


def _expense_report_month(expense_date: date) -> tuple[int, int]:
    """Return the report month selected by one manual expense date."""

    return expense_date.year, expense_date.month


def _expense_default_date(year: int, month: int) -> date:
    """Return a sensible default date for the selected report-month expense form."""

    selected_start, selected_end = _month_date_bounds(year, month)
    today = local_today()
    if selected_start <= today < selected_end:
        return today
    return selected_start


def _clean_expense_reason(reason: str) -> str:
    """Normalize manual expense text before persisting or rendering it."""

    cleaned = reason.strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="Expense reason is required")
    if len(cleaned) > 160:
        raise HTTPException(status_code=400, detail="Expense reason must be 160 characters or less")
    return cleaned


def _clean_expense_amount(amount: Decimal) -> Decimal:
    """Validate and round a submitted manual expense amount."""

    rounded = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if rounded <= 0:
        raise HTTPException(status_code=400, detail="Expense price must be greater than zero")
    return rounded


def _human_duration_since(value: datetime | None, *, now: datetime | None = None) -> str:
    if value is None:
        return "Never"
    current_dt = now or datetime.now(UTC)
    elapsed_seconds = int((datetime_to_utc(current_dt) - datetime_to_utc(value)).total_seconds())
    if elapsed_seconds <= 5:
        return "just now"

    units = (
        ("day", 86_400),
        ("hour", 3_600),
        ("minute", 60),
    )
    for label, unit_seconds in units:
        count = elapsed_seconds // unit_seconds
        if count >= 1:
            suffix = "" if count == 1 else "s"
            return f"{count} {label}{suffix} ago"
    return f"{elapsed_seconds} seconds ago"


def _api_test_result(
    status: str | None,
    message: str | None,
    value: str | None,
) -> ApiTestResult | None:
    if status not in {"pass", "fail"}:
        return None
    cleaned_message = (message or "").strip()[:300]
    cleaned_value = (value or "").strip()[:120]
    return ApiTestResult(status=status, message=cleaned_message, value=cleaned_value)


def _format_file_size(size_bytes: int) -> str:
    """Return a compact human-readable file size for backup listings."""

    if size_bytes >= 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    if size_bytes >= 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes} B"


def _format_storage_size(size_bytes: int) -> str:
    """Return a compact human-readable size for filesystem capacity values."""

    units = (
        ("TB", 1024**4),
        ("GB", 1024**3),
        ("MB", 1024**2),
        ("KB", 1024),
    )
    for suffix, unit_size in units:
        if size_bytes >= unit_size:
            return f"{size_bytes / unit_size:.1f} {suffix}"
    return f"{size_bytes} B"


def _format_record_count(record_count: int) -> str:
    """Return a human-readable record count with thousands separators."""

    suffix = "record" if record_count == 1 else "records"
    return f"{record_count:,} {suffix}"


def _serialize_automatic_backup(
    backup_file: AutomaticBackupFile,
) -> DiagnosticAutomaticBackup:
    """Return display-safe metadata for one automatic backup file."""

    source_labels = {
        "emergency": "Emergency",
        "startup": "Startup",
    }
    source_label = source_labels.get(backup_file.reason, "6-Hour")
    return DiagnosticAutomaticBackup(
        filename=backup_file.filename,
        created_at_display=_format_local_datetime(backup_file.created_at_utc),
        source_label=source_label,
        source_css_class=(
            "bad"
            if backup_file.reason == "emergency"
            else "warning"
            if backup_file.reason == "startup"
            else "muted"
        ),
        size_display=_format_file_size(backup_file.size_bytes),
        download_url=(
            "/diagnostics/automatic-backups/download?"
            f"filename={quote(backup_file.filename, safe='')}"
        ),
    )


def _sqlite_database_path(database_url: str) -> Path | None:
    """Return a local SQLite database path when the configured URL points to one."""

    try:
        parsed_url = make_url(database_url)
    except ArgumentError:
        return None
    if parsed_url.drivername not in {"sqlite", "sqlite+pysqlite"}:
        return None
    database_path = parsed_url.database
    if not database_path or database_path == ":memory:":
        return None
    return Path(database_path)


def _diagnostic_storage_paths(settings) -> tuple[str, ...]:
    """Return configured paths worth checking for Diagnostics disk usage."""

    path_candidates = [
        str(Path.cwd()),
        settings.app_data_dir,
        settings.automatic_backup_dir,
    ]
    sqlite_path = _sqlite_database_path(settings.database_url)
    if sqlite_path is not None:
        path_candidates.append(str(sqlite_path))

    unique_paths: list[str] = []
    seen: set[str] = set()
    for path_text in path_candidates:
        cleaned_path = str(path_text).strip()
        if not cleaned_path or cleaned_path in seen:
            continue
        seen.add(cleaned_path)
        unique_paths.append(cleaned_path)
    return tuple(unique_paths)


def _database_size_bytes(db: Session, database_url: str) -> int | None:
    """Return the current database storage size in bytes when the dialect supports it."""

    dialect_name = db.get_bind().dialect.name
    try:
        if dialect_name == "postgresql":
            database_size = db.scalar(text("select pg_database_size(current_database())"))
            return int(database_size) if database_size is not None else None
        if dialect_name == "sqlite":
            page_count = db.scalar(text("PRAGMA page_count"))
            page_size = db.scalar(text("PRAGMA page_size"))
            if page_count is not None and page_size is not None:
                return int(page_count) * int(page_size)
    except (SQLAlchemyError, ValueError, TypeError):
        logger.exception("Could not read database storage size")

    sqlite_path = _sqlite_database_path(database_url)
    if sqlite_path is None:
        return None
    try:
        return sqlite_path.stat().st_size if sqlite_path.exists() else None
    except OSError:
        logger.exception("Could not read SQLite database file size for path=%s", sqlite_path)
        return None


def _diagnostic_database_summary(
    db: Session,
    database_url: str,
) -> DiagnosticDatabaseSummary:
    """Return total app-table records and the current database storage footprint."""

    total_records = 0
    for table in Base.metadata.sorted_tables:
        total_records += int(db.scalar(select(func.count()).select_from(table)) or 0)

    size_bytes = _database_size_bytes(db, database_url)
    return DiagnosticDatabaseSummary(
        size_bytes=size_bytes,
        size_display=_format_storage_size(size_bytes) if size_bytes is not None else "Unavailable",
        total_records=total_records,
        total_records_display=_format_record_count(total_records),
    )


def _database_latency_display(db: Session) -> str:
    """Measure a lightweight database round trip for the Diagnostics status card."""

    elapsed_ms = measure_database_latency_ms(db)
    if elapsed_ms is None:
        return "Unavailable"
    return f"{elapsed_ms:.1f} ms"


def _call_pool_method(pool, method_name: str) -> int | None:
    """Return a numeric SQLAlchemy pool metric when the active pool exposes it."""

    method = getattr(pool, method_name, None)
    if not callable(method):
        return None
    try:
        return int(method())
    except (TypeError, ValueError, NotImplementedError):
        return None


def _database_pool_display(db: Session, settings) -> str:
    """Return safe connection-pool details without exposing connection strings."""

    pool = db.get_bind().pool
    configured = (
        f"{settings.database_pool_size} pool, "
        f"{settings.database_max_overflow} overflow"
    )
    size = _call_pool_method(pool, "size")
    checked_out = _call_pool_method(pool, "checkedout")
    overflow = _call_pool_method(pool, "overflow")
    if size is None or checked_out is None:
        return configured
    if overflow is None:
        return f"{checked_out} in use / {size} pool"
    return f"{checked_out} in use / {size} pool, {overflow} overflow"


def _diagnostic_database_stats(
    db: Session,
    settings,
    summary: DiagnosticDatabaseSummary,
) -> DiagnosticDatabaseStats:
    """Return database metrics for the Diagnostics System Status card."""

    latency_ms = measure_database_latency_ms(db)
    return DiagnosticDatabaseStats(
        latency_ms=latency_ms,
        latency_display="Unavailable" if latency_ms is None else f"{latency_ms:.1f} ms",
        latency_indicator_class=_database_latency_indicator_class(settings, latency_ms),
        size_display=summary.size_display,
        total_records_display=summary.total_records_display,
        pool_display=_database_pool_display(db, settings),
        timeout_display=(
            f"{settings.database_connect_timeout_seconds}s connect, "
            f"{settings.database_pool_timeout_seconds}s pool"
        ),
    )


def _database_latency_indicator_class(settings, latency_ms: float | None) -> str:
    """Return the status-dot class for the current database latency reading."""

    if latency_ms is None:
        return "warning"
    if latency_ms >= settings.app_health_db_latency_critical_ms:
        return "bad"
    if latency_ms >= settings.app_health_db_latency_warning_ms:
        return "warning"
    return "good"


def _format_gas_price(price_per_gallon: Decimal | None) -> str:
    """Format an optional per-gallon gas price for Diagnostics display."""

    if price_per_gallon is None:
        return "None"
    return f"${_format_comma_number(price_per_gallon, 3)}"


def _diagnostic_gas_price_extremes(db: Session) -> DiagnosticGasPriceExtremes:
    """Return gas price summary values for the Diagnostics Data card."""

    lowest_price, highest_price = db.execute(
        select(
            func.min(GasPriceSnapshot.price_per_gallon),
            func.max(GasPriceSnapshot.price_per_gallon),
        )
    ).one()
    current_price = db.scalar(
        select(GasPriceSnapshot.price_per_gallon)
        .order_by(
            GasPriceSnapshot.observed_on.desc(),
            GasPriceSnapshot.created_at.desc(),
            GasPriceSnapshot.id.desc(),
        )
        .limit(1)
    )
    current_year, current_month = _current_year_month()
    monthly_average = db.scalar(
        select(MonthlyGasPrice.average_price_per_gallon)
        .where(MonthlyGasPrice.year == current_year)
        .where(MonthlyGasPrice.month == current_month)
        .order_by(MonthlyGasPrice.updated_at.desc(), MonthlyGasPrice.id.desc())
        .limit(1)
    )
    return DiagnosticGasPriceExtremes(
        lowest_price_per_gallon=lowest_price,
        current_price_per_gallon=current_price,
        monthly_average_price_per_gallon=monthly_average,
        highest_price_per_gallon=highest_price,
    )


def _existing_disk_usage_target(path: Path) -> Path | None:
    """Return the nearest existing path that can be passed to disk usage checks."""

    expanded_path = path.expanduser()
    candidate = expanded_path if expanded_path.is_absolute() else Path.cwd() / expanded_path
    if candidate.exists():
        return candidate
    for parent in candidate.parents:
        if parent.exists():
            return parent
    return None


def _diagnostic_disk_usages(
    paths: tuple[str, ...],
    *,
    disk_usage_func=shutil.disk_usage,
) -> list[DiagnosticDiskUsage]:
    """Group configured paths by exact used and total bytes for the Diagnostics page."""

    grouped_paths: dict[tuple[int, int], list[tuple[str, str, int]]] = {}
    for path_text in paths:
        target_path = _existing_disk_usage_target(Path(path_text))
        if target_path is None:
            continue
        try:
            usage = disk_usage_func(target_path)
        except OSError:
            logger.exception("Could not read disk usage for path=%s", path_text)
            continue
        key = (int(usage.used), int(usage.total))
        grouped_paths.setdefault(key, []).append(
            (path_text, str(target_path), int(usage.free))
        )

    disk_usages: list[DiagnosticDiskUsage] = []
    for (used_bytes, total_bytes), path_rows in grouped_paths.items():
        free_bytes = path_rows[0][2]
        used_percent = (used_bytes / total_bytes * 100) if total_bytes else 0
        disk_usages.append(
            DiagnosticDiskUsage(
                paths=tuple(row[0] for row in path_rows),
                inspected_path=path_rows[0][1],
                total_bytes=total_bytes,
                used_bytes=used_bytes,
                free_bytes=free_bytes,
                total_display=_format_storage_size(total_bytes),
                used_display=_format_storage_size(used_bytes),
                free_display=_format_storage_size(free_bytes),
                used_percent_display=f"{used_percent:.1f}%",
                used_percent_style=f"{min(max(used_percent, 0), 100):.1f}%",
            )
        )

    return sorted(disk_usages, key=lambda item: item.primary_path)


def _update_trip_row_values(
    trip: Trip,
    *,
    origin_site: Site,
    destination_site: Site,
    miles: Decimal,
) -> bool:
    """Apply only the editable Trips-page fields to one trip row."""

    manual_review_needed = _apply_trip_waypoints(trip, origin_site, destination_site)
    distance_changed = False
    rounded_miles = Decimal(str(miles)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    if trip.miles != rounded_miles:
        trip.miles = rounded_miles
        trip.mileage_source = MILEAGE_SOURCE_MANUAL
        distance_changed = True
        manual_review_needed = True

    if manual_review_needed:
        mark_trip_user_edited(trip)
    return distance_changed


def _diagnostics_redirect(fragment: str, params: dict[str, str]) -> RedirectResponse:
    query = urlencode(params)
    return RedirectResponse(url=f"/diagnostics?{query}#{fragment}", status_code=303)


def _require_backup_restore_auth(request: Request) -> None:
    """Require a logged-in web session before exporting or restoring sensitive app data."""

    settings = get_settings()
    if web_login_enabled(settings) and request_is_authenticated(request):
        return
    raise HTTPException(
        status_code=403,
        detail="Full backup and restore require WEB_LOGIN_USERNAME and WEB_LOGIN_PASSWORD login.",
    )


def _require_diagnostics_security_auth(request: Request) -> None:
    """Require an authenticated web session before mutating Diagnostics security state."""

    settings = get_settings()
    if web_login_enabled(settings) and request_is_authenticated(request):
        return
    raise HTTPException(
        status_code=403,
        detail=(
            "Diagnostics security actions require WEB_LOGIN_USERNAME and WEB_LOGIN_PASSWORD "
            "login."
        ),
    )


async def _json_object_payload(request: Request) -> dict:
    """Return a JSON object payload or reject malformed ceremony requests."""

    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Request body must be JSON.") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object.")
    return payload


def _passkey_error_response(message: str, status_code: int) -> JSONResponse:
    """Return a compact JSON error for browser passkey JavaScript."""

    return JSONResponse({"error": message}, status_code=status_code)


def _record_failed_passkey_login(
    db: Session,
    request: Request,
    *,
    reason: str,
    safe_next: str,
    settings,
    locked_out: bool = False,
) -> None:
    """Record a failed passkey login through the standard audit and lockout path."""

    if locked_out:
        attempt_state = login_failure_state(request, settings)
    else:
        attempt_state = record_login_failure(request, settings)
    lockout_remaining_seconds = login_lockout_remaining_seconds(attempt_state)
    record_web_login_failure(
        db=db,
        request=request,
        username=settings.web_login_username,
        password="",
        reason=reason,
        failed_count=attempt_state.failed_count if attempt_state else 0,
        max_attempts=settings.web_login_max_attempts,
        lockout_applied=locked_out or lockout_remaining_seconds > 0,
        lockout_remaining_seconds=lockout_remaining_seconds,
        next_url=safe_next,
        settings=settings,
    )
    _maybe_auto_block_failed_login_ip(
        db,
        request,
        attempt_state.failed_count if attempt_state else 0,
        settings,
    )
    db.commit()


def _cloudflare_block_for_ip(db: Session, ip_address: str) -> CloudflareIPBlock | None:
    """Return the app-managed Cloudflare block row for an IP, if present."""

    return db.scalar(
        select(CloudflareIPBlock).where(CloudflareIPBlock.ip_address == ip_address).limit(1)
    )


def _create_app_cloudflare_block(
    db: Session,
    ip_address: str,
    *,
    source: str,
    reason: str,
    failure_count: int | None = None,
    settings=None,
) -> CloudflareIPBlock:
    """Create a Cloudflare block and persist the app-managed rule ID."""

    active_settings = settings or get_settings()
    normalized_ip = normalize_ip_address(ip_address)
    if normalized_ip is None:
        raise CloudflareBlockError("Cannot block an invalid IP address.")
    if ip_is_allowlisted(normalized_ip, active_settings):
        raise CloudflareBlockError("IP address is on the Cloudflare block allowlist.")

    existing_block = _cloudflare_block_for_ip(db, normalized_ip)
    if existing_block is not None:
        return existing_block

    note = f"Trip Tracker {source} block: {reason}"
    access_rule = create_cloudflare_ip_block(normalized_ip, note=note, settings=active_settings)
    block = CloudflareIPBlock(
        ip_address=normalized_ip,
        cloudflare_rule_id=access_rule.rule_id,
        source=source,
        reason=reason,
        failure_count=failure_count,
        notes=note,
    )
    db.add(block)
    db.commit()
    db.refresh(block)
    logger.warning(
        "Created app-managed Cloudflare IP block ip=%s source=%s failure_count=%s",
        normalized_ip,
        source,
        failure_count,
    )
    return block


def _remove_app_cloudflare_block(
    db: Session,
    block: CloudflareIPBlock,
    *,
    settings=None,
) -> None:
    """Delete the Cloudflare rule and remove its app-managed local row."""

    active_settings = settings or get_settings()
    delete_cloudflare_ip_block(block.cloudflare_rule_id, settings=active_settings)
    logger.warning("Removed app-managed Cloudflare IP block ip=%s", block.ip_address)
    db.delete(block)
    db.commit()


def _maybe_auto_block_failed_login_ip(
    db: Session,
    request: Request,
    failed_count: int,
    settings,
) -> None:
    """Automatically block a login IP after the configured consecutive-failure threshold."""

    if failed_count < settings.cloudflare_auto_block_failed_login_attempts:
        return
    if not cloudflare_ip_blocking_configured(settings):
        return
    normalized_ip = normalize_ip_address(login_client_key(request, settings))
    if normalized_ip is None:
        logger.warning("Skipped Cloudflare auto-block for invalid login client IP")
        return
    if ip_is_allowlisted(normalized_ip, settings):
        logger.warning("Skipped Cloudflare auto-block for allowlisted ip=%s", normalized_ip)
        return
    try:
        _create_app_cloudflare_block(
            db,
            normalized_ip,
            source="automatic",
            reason=(
                f"{settings.cloudflare_auto_block_failed_login_attempts} consecutive "
                "failed web login attempts"
            ),
            failure_count=failed_count,
            settings=settings,
        )
    except CloudflareBlockError as exc:
        logger.warning("Could not auto-block failed login IP at Cloudflare: %s", exc)


def _waypoint_ordering():
    return (
        Site.last_visited_at.desc().nulls_last(),
        Site.created_at.desc(),
        Site.name.asc(),
    )


def _waypoints_for_trip_forms(db: Session) -> list[Site]:
    """Return waypoints in the same stable order used by the Waypoints page."""

    return list(db.scalars(select(Site).order_by(*_waypoint_ordering())))


def _load_trip_form_waypoint(db: Session, waypoint_id: int) -> Site:
    """Load a submitted waypoint ID or reject the trip form as invalid."""

    waypoint = db.get(Site, waypoint_id)
    if waypoint is None:
        raise HTTPException(status_code=400, detail="Selected waypoint does not exist")
    return waypoint


def _apply_trip_waypoints(trip: Trip, origin_site: Site, destination_site: Site) -> bool:
    """Apply selected waypoint metadata to a trip and report whether it changed."""

    changed = (
        trip.origin_site_id != origin_site.id
        or trip.destination_site_id != destination_site.id
        or trip.origin_name != origin_site.name
        or trip.destination_name != destination_site.name
        or trip.start_latitude != origin_site.latitude
        or trip.start_longitude != origin_site.longitude
        or trip.end_latitude != destination_site.latitude
        or trip.end_longitude != destination_site.longitude
    )
    if not changed:
        return False

    trip.origin_site = origin_site
    trip.destination_site = destination_site
    trip.origin_site_id = origin_site.id
    trip.destination_site_id = destination_site.id
    trip.origin_name = origin_site.name
    trip.destination_name = destination_site.name
    trip.start_latitude = origin_site.latitude
    trip.start_longitude = origin_site.longitude
    trip.end_latitude = destination_site.latitude
    trip.end_longitude = destination_site.longitude
    trip.mileage_source = MILEAGE_SOURCE_MANUAL
    mark_trip_user_edited(trip)
    return True


def _latest_odometer_reading(db: Session) -> dict | None:
    candidates = []
    options = (joinedload(Trip.origin_site), joinedload(Trip.destination_site))

    latest_end_trip = db.scalar(
        select(Trip)
        .options(*options)
        .where(Trip.end_odometer_miles.is_not(None))
        .order_by(Trip.ended_at.desc(), Trip.id.desc())
        .limit(1)
    )
    if latest_end_trip is not None:
        candidates.append(
            {
                "value": latest_end_trip.end_odometer_miles,
                "source": latest_end_trip.end_odometer_source or latest_end_trip.mileage_source,
                "recorded_at": latest_end_trip.ended_at,
                "trip": latest_end_trip,
                "database_id": latest_end_trip.id,
                "position": "End",
            }
        )

    latest_start_trip = db.scalar(
        select(Trip)
        .options(*options)
        .where(Trip.start_odometer_miles.is_not(None))
        .order_by(Trip.started_at.desc(), Trip.id.desc())
        .limit(1)
    )
    if latest_start_trip is not None:
        candidates.append(
            {
                "value": latest_start_trip.start_odometer_miles,
                "source": latest_start_trip.start_odometer_source
                or latest_start_trip.mileage_source,
                "recorded_at": latest_start_trip.started_at,
                "trip": latest_start_trip,
                "database_id": latest_start_trip.id,
                "position": "Start",
            }
        )

    checkpoint = db.scalar(
        select(TripProcessingCheckpoint)
        .where(TripProcessingCheckpoint.name == AUTOMATIC_TRIP_PROCESSING_CHECKPOINT)
        .where(TripProcessingCheckpoint.odometer_anchor_miles.is_not(None))
        .where(TripProcessingCheckpoint.odometer_anchor_recorded_at.is_not(None))
        .limit(1)
    )
    if checkpoint is not None:
        candidates.append(
            {
                "value": checkpoint.odometer_anchor_miles,
                "source": "owntracks_estimate",
                "recorded_at": checkpoint.odometer_anchor_recorded_at,
                "trip": None,
                "database_id": checkpoint.id,
                "position": "Rolling",
            }
        )

    if not candidates:
        return None
    return max(
        candidates,
        key=lambda item: (datetime_to_utc(item["recorded_at"]), item["database_id"]),
    )


def _masked_database_url(url: str) -> str:
    parts = urlsplit(url)
    if not parts.password:
        return url
    username = parts.username or ""
    hostname = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    netloc = f"{username}:***@{hostname}{port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


@router.get("/login", response_class=HTMLResponse)
def login_page(
    request: Request,
    next: str = Query(default="/"),
    public_timeout: bool = Query(default=False),
    db: Session = Depends(get_db),
) -> Response:
    settings = get_settings()
    safe_next = valid_next_path(next)
    if not web_login_enabled(settings):
        return RedirectResponse(url=safe_next, status_code=303)
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "next_url": safe_next,
            "login_error": (
                "Public-device session ended after 15 minutes of inactivity."
                if public_timeout
                else ""
            ),
            "public_device": False,
            "passkey_login_available": passkey_login_available(db),
        },
    )


@router.post("/passkeys/login/options")
async def passkey_login_options(
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    """Return WebAuthn authentication options for the login page."""

    settings = get_settings()
    payload = await _json_object_payload(request)
    safe_next = valid_next_path(str(payload.get("next_url") or "/"))
    if payload.get("public_device") is True:
        return _passkey_error_response(
            "Device Sign-In is unavailable on a public device.",
            400,
        )
    if not web_login_enabled(settings):
        return _passkey_error_response("Web login is not configured.", 404)
    if login_is_locked(request, settings):
        _record_failed_passkey_login(
            db,
            request,
            reason="locked_out",
            safe_next=safe_next,
            settings=settings,
            locked_out=True,
        )
        return _passkey_error_response("Login is temporarily unavailable.", 429)
    try:
        options_json = begin_passkey_authentication(
            db,
            request,
            settings,
            next_url=safe_next,
        )
    except PasskeyCeremonyError:
        return _passkey_error_response("Passkey login is not configured.", 404)
    return Response(content=options_json, media_type="application/json")


@router.post("/passkeys/login/verify")
async def passkey_login_verify(
    request: Request,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Verify a browser passkey assertion and mark the web session authenticated."""

    settings = get_settings()
    payload = await _json_object_payload(request)
    safe_next = valid_next_path(str(payload.get("next_url") or "/"))
    if payload.get("public_device") is True:
        return _passkey_error_response(
            "Device Sign-In is unavailable on a public device.",
            400,
        )
    if not web_login_enabled(settings):
        return _passkey_error_response("Web login is not configured.", 404)
    if login_is_locked(request, settings):
        _record_failed_passkey_login(
            db,
            request,
            reason="locked_out",
            safe_next=safe_next,
            settings=settings,
            locked_out=True,
        )
        return _passkey_error_response("Login is temporarily unavailable.", 429)
    try:
        passkey = finish_passkey_authentication(db, request, payload)
    except PasskeyCeremonyError:
        _record_failed_passkey_login(
            db,
            request,
            reason="invalid_passkey",
            safe_next=safe_next,
            settings=settings,
        )
        db.commit()
        logger.warning("Web passkey login failed")
        return _passkey_error_response("Device sign-in failed.", 401)

    record_web_login_success(
        db=db,
        request=request,
        username=settings.web_login_username,
        account=settings.web_login_username,
        authentication_method="passkey",
        next_url=safe_next,
        settings=settings,
    )
    clear_login_failures(request, settings)
    mark_request_authenticated(request, public_device=False)
    db.commit()
    logger.info("Web passkey login succeeded passkey_id=%s", passkey.id)
    return JSONResponse({"redirect_url": safe_next})


@router.post("/login")
def login_form(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next_url: str = Form(default="/"),
    public_device: bool = Form(default=False),
    db: Session = Depends(get_db),
) -> Response:
    settings = get_settings()
    safe_next = valid_next_path(next_url)
    if not web_login_enabled(settings):
        return RedirectResponse(url=safe_next, status_code=303)
    if login_is_locked(request, settings):
        attempt_state = login_failure_state(request, settings)
        lockout_remaining_seconds = login_lockout_remaining_seconds(attempt_state)
        record_web_login_failure(
            db=db,
            request=request,
            username=username,
            password=password,
            reason="locked_out",
            failed_count=attempt_state.failed_count if attempt_state else 0,
            max_attempts=settings.web_login_max_attempts,
            lockout_applied=True,
            lockout_remaining_seconds=lockout_remaining_seconds,
            next_url=safe_next,
            settings=settings,
        )
        _maybe_auto_block_failed_login_ip(
            db,
            request,
            attempt_state.failed_count if attempt_state else 0,
            settings,
        )
        db.commit()
        logger.warning("Web login rejected reason=locked_out")
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "next_url": safe_next,
                "login_error": "Login is temporarily unavailable.",
                "public_device": public_device,
                "passkey_login_available": passkey_login_available(db),
            },
        )
    if authenticate_web_credentials(username, password, settings):
        record_web_login_success(
            db=db,
            request=request,
            username=username,
            account=settings.web_login_username,
            authentication_method="password",
            next_url=safe_next,
            settings=settings,
        )
        clear_login_failures(request, settings)
        mark_request_authenticated(request, public_device=public_device)
        db.commit()
        logger.info("Web login succeeded")
        return RedirectResponse(url=safe_next, status_code=303)

    attempt_state = record_login_failure(request, settings)
    lockout_remaining_seconds = login_lockout_remaining_seconds(attempt_state)
    record_web_login_failure(
        db=db,
        request=request,
        username=username,
        password=password,
        reason="invalid_credentials",
        failed_count=attempt_state.failed_count,
        max_attempts=settings.web_login_max_attempts,
        lockout_applied=lockout_remaining_seconds > 0,
        lockout_remaining_seconds=lockout_remaining_seconds,
        next_url=safe_next,
        settings=settings,
    )
    _maybe_auto_block_failed_login_ip(db, request, attempt_state.failed_count, settings)
    db.commit()
    logger.warning("Web login failed")
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "next_url": safe_next,
            "login_error": "Invalid username or password.",
            "public_device": public_device,
            "passkey_login_available": passkey_login_available(db),
        },
    )


@router.post("/logout")
def logout_form(request: Request) -> RedirectResponse:
    public_device = request_is_public_device(request)
    clear_request_authentication(request)
    response = RedirectResponse(url="/login", status_code=303)
    if public_device:
        return add_public_device_clear_site_data(response)
    return response


@router.post("/session/activity", status_code=204)
def public_device_activity(request: Request) -> Response:
    """Refresh the authenticated public-device session after browser activity."""

    record_public_device_activity(request)
    return Response(status_code=204, headers={"Cache-Control": "no-store"})


def _dashboard_template_context(db: Session) -> dict:
    """Return the expensive Dashboard context loaded after the shell renders."""

    settings = get_settings()
    app_now = local_now()
    app_today = app_now.date()
    year, month = _year_month_for_local_date(app_today)
    monthly_gas, _ = _monthly_gas_context(db, year, month)
    distance_summary = _dashboard_distance_summary(
        db,
        today=app_today,
        year=year,
        month=month,
    )
    location_count = owntracks_monthly_event_count(db, year=year, month=month)
    work_trip_counts = _dashboard_work_trip_counts(
        db,
        today=app_today,
        year=year,
        month=month,
    )
    reimbursement_summary = _dashboard_reimbursement_summary(
        db,
        year=year,
        month=month,
        monthly_gas=monthly_gas,
        vehicle_mpg=settings.vehicle_mpg,
    )
    latest_odometer = _latest_odometer_reading(db)
    movement_diagnostics = owntracks_movement_diagnostics(db)
    location_state = _dashboard_location_state(movement_diagnostics.current_state)
    recent_trips = list(
        db.scalars(
            select(Trip)
            .options(joinedload(Trip.origin_site), joinedload(Trip.destination_site))
            .order_by(Trip.trip_date.desc(), Trip.started_at.desc())
            .limit(8)
        )
    )
    return {
        "year": year,
        "month": month,
        "location_count": location_count,
        "trip_count": work_trip_counts["month"],
        "work_trip_counts": work_trip_counts,
        "distance_summary": distance_summary,
        "reimbursement_summary": reimbursement_summary,
        "location_state": location_state,
        "latest_odometer": latest_odometer,
        "recent_trips": recent_trips,
        "monthly_gas": monthly_gas,
        "app_local_datetime": app_now,
        "app_timezone": settings.local_timezone,
        "app_timezone_abbr": app_now.tzname(),
    }


def _assert_shell_database_available(db: Session) -> None:
    """Force lightweight shell routes to fail into limp mode when PostgreSQL is offline."""

    db.execute(text("SELECT 1"))


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    _assert_shell_database_available(db)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"dashboard_content_url": "/dashboard/content"},
    )


@router.get("/dashboard/content", response_class=HTMLResponse)
def dashboard_content(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "dashboard_content.html",
        _dashboard_template_context(db),
    )


@router.get("/trips", response_class=HTMLResponse)
def trips(
    request: Request,
    year: int | None = None,
    month: int | None = None,
    selected_month: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    _assert_shell_database_available(db)
    year, month = _resolve_selected_trips_month(
        year=year,
        month=month,
        selected_month=selected_month,
    )
    return templates.TemplateResponse(
        request,
        "trips.html",
        {"trips_content_url": f"/trips/content?year={year}&month={month}"},
    )


@router.get("/trips/content", response_class=HTMLResponse)
def trips_content(
    request: Request,
    year: int | None = None,
    month: int | None = None,
    selected_month: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    year, month = _resolve_selected_trips_month(
        year=year,
        month=month,
        selected_month=selected_month,
    )
    return templates.TemplateResponse(
        request,
        "trips_content.html",
        _trips_template_context(db, year=year, month=month),
    )


@router.post("/trips/{trip_id}")
def update_trip_form(
    trip_id: int,
    origin_site_id: int = Form(...),
    destination_site_id: int = Form(...),
    miles: Decimal = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    trip = db.get(Trip, trip_id)
    if trip is None:
        raise HTTPException(status_code=404, detail="Trip not found")
    if miles < 0:
        raise HTTPException(status_code=400, detail="Miles must be zero or greater")
    origin_site = _load_trip_form_waypoint(db, origin_site_id)
    destination_site = _load_trip_form_waypoint(db, destination_site_id)

    distance_changed = _update_trip_row_values(
        trip,
        origin_site=origin_site,
        destination_site=destination_site,
        miles=miles,
    )
    adjusted_trip_count = (
        resequence_month_trip_odometers_from(db, trip) if distance_changed else 0
    )
    db.commit()
    logger.info(
        "Updated trip via web form trip_id=%s date=%s origin=%s destination=%s miles=%s "
        "adjusted_same_month_trips=%s",
        trip.id,
        trip.trip_date.isoformat(),
        trip.origin_display_name,
        trip.destination_display_name,
        trip.miles,
        adjusted_trip_count,
    )
    return RedirectResponse(
        url=f"/trips?year={trip.trip_date.year}&month={trip.trip_date.month}",
        status_code=303,
    )


@router.post("/trips")
def create_trip_form(
    trip_date: date = Form(...),
    origin_site_id: int = Form(...),
    destination_site_id: int = Form(...),
    miles: Decimal = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    if miles < 0:
        raise HTTPException(status_code=400, detail="Miles must be zero or greater")
    origin_site = _load_trip_form_waypoint(db, origin_site_id)
    destination_site = _load_trip_form_waypoint(db, destination_site_id)
    trip = create_manual_trip(
        db,
        trip_date=trip_date,
        origin_name=origin_site.name,
        destination_name=destination_site.name,
        miles=miles,
    )
    _apply_trip_waypoints(trip, origin_site, destination_site)
    db.commit()
    logger.info(
        "Created manual trip via web form trip_id=%s date=%s origin=%s destination=%s miles=%s",
        trip.id,
        trip.trip_date.isoformat(),
        trip.origin_display_name,
        trip.destination_display_name,
        trip.miles,
    )
    return RedirectResponse(
        url=f"/trips?year={trip.trip_date.year}&month={trip.trip_date.month}",
        status_code=303,
    )


@router.post("/trips/report-expenses/add")
def create_monthly_report_expense_form(
    expense_date: date = Form(...),
    expense_reason: str = Form(...),
    expense_amount: Decimal = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    expense_year, expense_month = _expense_report_month(expense_date)
    existing_count = _monthly_report_expense_count(
        db,
        year=expense_year,
        month=expense_month,
    )
    if existing_count >= MONTHLY_REPORT_EXPENSE_LIMIT:
        raise HTTPException(
            status_code=400,
            detail=f"Monthly reports can include at most {MONTHLY_REPORT_EXPENSE_LIMIT} expenses.",
        )

    expense = MonthlyReportExpense(
        expense_date=expense_date,
        year=expense_year,
        month=expense_month,
        reason=_clean_expense_reason(expense_reason),
        amount=_clean_expense_amount(expense_amount),
    )
    db.add(expense)
    db.commit()
    logger.info(
        "Created monthly report expense expense_id=%s date=%s amount=%s",
        expense.id,
        expense.expense_date.isoformat(),
        expense.amount,
    )
    return RedirectResponse(
        url=f"/trips?year={expense_year}&month={expense_month}",
        status_code=303,
    )


@router.post("/trips/report-expenses/{expense_id}/delete")
def delete_monthly_report_expense_form(
    expense_id: int,
    redirect_year: int = Form(...),
    redirect_month: int = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    _validate_month(redirect_month)
    expense = db.get(MonthlyReportExpense, expense_id)
    if expense is None:
        raise HTTPException(status_code=404, detail="Monthly report expense not found")

    logger.info(
        "Deleted monthly report expense expense_id=%s date=%s amount=%s",
        expense.id,
        expense.expense_date.isoformat(),
        expense.amount,
    )
    db.delete(expense)
    db.commit()
    return RedirectResponse(
        url=f"/trips?year={redirect_year}&month={redirect_month}",
        status_code=303,
    )


@router.post("/trips/{trip_id}/delete")
def delete_trip_form(trip_id: int, db: Session = Depends(get_db)) -> RedirectResponse:
    trip = db.get(Trip, trip_id)
    if trip is None:
        raise HTTPException(status_code=404, detail="Trip not found")

    redirect_year = trip.trip_date.year
    redirect_month = trip.trip_date.month
    deleted_trip = delete_trip(db, trip)
    db.commit()
    logger.info(
        "Deleted trip via web form trip_id=%s suppressed=%s origin=%s destination=%s "
        "started_at=%s ended_at=%s",
        trip_id,
        deleted_trip is not None,
        deleted_trip.origin_name if deleted_trip is not None else "",
        deleted_trip.destination_name if deleted_trip is not None else "",
        deleted_trip.started_at.isoformat() if deleted_trip is not None else "",
        deleted_trip.ended_at.isoformat() if deleted_trip is not None else "",
    )
    return RedirectResponse(
        url=f"/trips?year={redirect_year}&month={redirect_month}",
        status_code=303,
    )


@router.post("/trips/suppression/{deleted_trip_id}/delete")
def delete_trip_suppression_form(
    deleted_trip_id: int,
    redirect_year: int = Form(...),
    redirect_month: int = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    _validate_month(redirect_month)
    deleted_trip = db.get(DeletedTrip, deleted_trip_id)
    if deleted_trip is None:
        raise HTTPException(status_code=404, detail="Deleted trip record not found")

    logger.info(
        "Removed deleted trip record deleted_trip_id=%s origin=%s destination=%s "
        "started_at=%s ended_at=%s",
        deleted_trip.id,
        deleted_trip.origin_name or "",
        deleted_trip.destination_name or "",
        deleted_trip.started_at.isoformat(),
        deleted_trip.ended_at.isoformat(),
    )
    db.delete(deleted_trip)
    db.commit()
    return RedirectResponse(
        url=f"/trips?year={redirect_year}&month={redirect_month}",
        status_code=303,
    )


@router.get("/sites")
def sites_redirect() -> RedirectResponse:
    return RedirectResponse(url="/waypoints", status_code=308)


def _detach_waypoint_references(db: Session, waypoint: Site) -> tuple[int, int]:
    """Remove foreign-key references before deleting a waypoint while keeping audit text."""

    trip_updates = 0
    trips = list(
        db.scalars(
            select(Trip).where(
                (Trip.origin_site_id == waypoint.id)
                | (Trip.destination_site_id == waypoint.id)
            )
        )
    )
    for trip in trips:
        if trip.origin_site_id == waypoint.id:
            trip.origin_name = trip.origin_name or waypoint.name
            trip.origin_site_id = None
            trip_updates += 1
        if trip.destination_site_id == waypoint.id:
            trip.destination_name = trip.destination_name or waypoint.name
            trip.destination_site_id = None
            trip_updates += 1

    deleted_trip_updates = 0
    deleted_trips = list(
        db.scalars(
            select(DeletedTrip).where(
                (DeletedTrip.origin_site_id == waypoint.id)
                | (DeletedTrip.destination_site_id == waypoint.id)
            )
        )
    )
    for deleted_trip in deleted_trips:
        if deleted_trip.origin_site_id == waypoint.id:
            deleted_trip.origin_name = deleted_trip.origin_name or waypoint.name
            deleted_trip.origin_site_id = None
            deleted_trip_updates += 1
        if deleted_trip.destination_site_id == waypoint.id:
            deleted_trip.destination_name = deleted_trip.destination_name or waypoint.name
            deleted_trip.destination_site_id = None
            deleted_trip_updates += 1

    return trip_updates, deleted_trip_updates


@router.post("/waypoints/{waypoint_id}/delete")
def delete_waypoint_form(
    waypoint_id: int,
    page: int = Form(default=1),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    waypoint = db.get(Site, waypoint_id)
    if waypoint is None:
        raise HTTPException(status_code=404, detail="Waypoint not found")

    redirect_page = max(page, 1)
    trip_updates, deleted_trip_updates = _detach_waypoint_references(db, waypoint)
    logger.info(
        "Deleted waypoint id=%s name=%s trip_references_detached=%s "
        "deleted_trip_references_detached=%s",
        waypoint.id,
        waypoint.name,
        trip_updates,
        deleted_trip_updates,
    )
    db.delete(waypoint)
    db.commit()
    return RedirectResponse(url=f"/waypoints?page={redirect_page}", status_code=303)


@router.get("/waypoints", response_class=HTMLResponse)
def waypoints(
    request: Request,
    page: int = Query(default=1, ge=1),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    waypoint_count = db.scalar(select(func.count(Site.id))) or 0
    pagination = _pagination_context(waypoint_count, page, WAYPOINT_PAGE_SIZE)
    all_waypoints = list(
        db.scalars(
            select(Site)
            .order_by(*_waypoint_ordering())
            .offset((pagination["page"] - 1) * pagination["page_size"])
            .limit(pagination["page_size"])
        )
    )
    return templates.TemplateResponse(
        request,
        "waypoints.html",
        {
            "waypoints": all_waypoints,
            "waypoint_pagination": pagination,
        },
    )


@router.get("/waypoints/export")
def export_waypoints(db: Session = Depends(get_db)) -> Response:
    all_waypoints = list(db.scalars(select(Site).order_by(*_waypoint_ordering())))
    return Response(
        content=owntracks_waypoints_json(all_waypoints),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="owntracks-waypoints.json"'},
    )


@router.post("/gas-prices/refresh")
def refresh_gas_price_form(
    next_url: str = Form(default="/"),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    try:
        refresh_current_monthly_price(db)
    except GasPriceUnavailable as exc:
        logger.warning("Gas price refresh unavailable from web form: %s", exc)
        pass
    else:
        logger.info("Refreshed current monthly gas price from web form")
    return RedirectResponse(url=next_url if next_url.startswith("/") else "/", status_code=303)


@router.get("/diagnostics", response_class=HTMLResponse)
def diagnostics(
    request: Request,
    owntracks_page: int = Query(default=1, ge=1),
    state_changes_page: int = Query(default=1, ge=1),
    login_successes_page: int = Query(default=1, ge=1),
    login_failures_page: int = Query(default=1, ge=1),
    cloudflare_blocks_page: int = Query(default=1, ge=1),
    odometer_test: str | None = Query(default=None),
    odometer_message: str | None = Query(default=None),
    odometer_value: str | None = Query(default=None),
    eia_test: str | None = Query(default=None),
    eia_message: str | None = Query(default=None),
    eia_value: str | None = Query(default=None),
    restore_test: str | None = Query(default=None),
    restore_message: str | None = Query(default=None),
    restore_value: str | None = Query(default=None),
    cloudflare_block_test: str | None = Query(default=None),
    cloudflare_block_message: str | None = Query(default=None),
    cloudflare_block_value: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> HTMLResponse:
    settings = get_settings()
    latest_location = db.scalar(
        select(OwnTracksLocation).order_by(OwnTracksLocation.captured_at.desc()).limit(1)
    )
    latest_received_location = db.scalar(
        select(OwnTracksLocation).order_by(OwnTracksLocation.received_at.desc()).limit(1)
    )
    latest_snapshot = db.scalar(
        select(GasPriceSnapshot).order_by(GasPriceSnapshot.observed_on.desc()).limit(1)
    )
    latest_monthly_gas = db.scalar(
        select(MonthlyGasPrice)
        .order_by(MonthlyGasPrice.year.desc(), MonthlyGasPrice.month.desc())
        .limit(1)
    )
    latest_odometer = _latest_odometer_reading(db)
    owntracks_entries_page = paginated_owntracks_entries(
        db,
        page=owntracks_page,
        page_size=DIAGNOSTICS_TABLE_PAGE_SIZE,
    )
    movement_diagnostics = owntracks_movement_diagnostics(
        db,
        state_change_limit=DIAGNOSTICS_STATE_CHANGE_LIMIT,
    )
    movement_state = movement_diagnostics.current_state
    inside_home = (
        movement_state.state == "waypoint"
        and (movement_state.site_name or "").casefold() == HOME_WAYPOINT_NAME.casefold()
    )
    movement_state_changes, movement_state_changes_page = _paginate_items(
        movement_diagnostics.state_changes,
        page=state_changes_page,
        page_size=DIAGNOSTICS_TABLE_PAGE_SIZE,
    )
    backup_restore_enabled = web_login_enabled(settings)
    disk_usages = _diagnostic_disk_usages(_diagnostic_storage_paths(settings))
    database_summary = _diagnostic_database_summary(db, settings.database_url)
    database_stats = _diagnostic_database_stats(db, settings, database_summary)
    runtime_status = build_runtime_status(settings, database_available=True)
    gas_price_extremes = _diagnostic_gas_price_extremes(db)
    hidden_login_failure_ids = set(db.scalars(select(HiddenLoginFailure.entry_id)))
    login_failure_entries = tail_login_failure_entries(
        db,
        max_entries=DIAGNOSTICS_LOGIN_FAILURE_MAX_ENTRIES,
        hidden_entry_ids=hidden_login_failure_ids,
        settings=settings,
    )
    login_success_entries = tail_login_success_entries(
        db,
        max_entries=DIAGNOSTICS_LOGIN_SUCCESS_MAX_ENTRIES,
        settings=settings,
    )
    login_success_entries, login_success_entries_page = _paginate_items(
        login_success_entries,
        page=login_successes_page,
        page_size=DIAGNOSTICS_TABLE_PAGE_SIZE,
    )
    login_failure_entries, login_failure_entries_page = _paginate_items(
        login_failure_entries,
        page=login_failures_page,
        page_size=DIAGNOSTICS_TABLE_PAGE_SIZE,
    )
    all_cloudflare_ip_blocks = list(
        db.scalars(select(CloudflareIPBlock).order_by(CloudflareIPBlock.created_at.desc()))
    )
    app_health_snapshot = build_app_health_snapshot(
        settings=settings,
        runtime_status=runtime_status,
        database_latency_ms=database_stats.latency_ms,
        disk_usages=disk_usages,
        cloudflare_block_count=len(all_cloudflare_ip_blocks),
    )
    cloudflare_ip_blocks, cloudflare_ip_blocks_page = _paginate_items(
        all_cloudflare_ip_blocks,
        page=cloudflare_blocks_page,
        page_size=DIAGNOSTICS_TABLE_PAGE_SIZE,
    )
    blocked_ip_addresses = {block.ip_address for block in all_cloudflare_ip_blocks}
    login_failure_ip_statuses = {}
    for entry in login_failure_entries:
        normalized_ip = normalize_ip_address(entry.client_ip)
        login_failure_ip_statuses[entry.entry_id] = {
            "ip_address": normalized_ip or entry.client_ip,
            "valid": normalized_ip is not None,
            "blocked": normalized_ip in blocked_ip_addresses if normalized_ip else False,
            "allowlisted": ip_is_allowlisted(normalized_ip, settings) if normalized_ip else False,
        }
    automatic_backups = (
        [
            _serialize_automatic_backup(backup_file)
            for backup_file in list_automatic_backup_files(settings.automatic_backup_dir)
        ]
        if backup_restore_enabled and request_is_authenticated(request)
        else []
    )
    return templates.TemplateResponse(
        request,
        "diagnostics.html",
        {
            "app_version": APP_VERSION,
            "settings": settings,
            "database_url": _masked_database_url(settings.database_url),
            "runtime_status": runtime_status,
            "database_stats": database_stats,
            "app_health_snapshot": app_health_snapshot,
            "location_count": owntracks_entries_page.total,
            "site_count": db.scalar(select(func.count(Site.id))) or 0,
            "trip_count": db.scalar(select(func.count(Trip.id))) or 0,
            "gas_snapshot_count": db.scalar(select(func.count(GasPriceSnapshot.id))) or 0,
            "gas_price_extremes": gas_price_extremes,
            "latest_location": latest_location,
            "last_owntracks_received_at": (
                latest_received_location.received_at if latest_received_location else None
            ),
            "last_owntracks_received_age": _human_duration_since(
                latest_received_location.received_at if latest_received_location else None
            ),
            "latest_snapshot": latest_snapshot,
            "latest_monthly_gas": latest_monthly_gas,
            "latest_odometer": latest_odometer,
            "disk_usages": disk_usages,
            "database_summary": database_summary,
            "recent_locations": owntracks_entries_page.entries,
            "owntracks_entries_page": owntracks_entries_page,
            "movement_state": movement_state,
            "inside_home": inside_home,
            "movement_state_changes": movement_state_changes,
            "movement_state_changes_page": movement_state_changes_page,
            "login_success_entries": login_success_entries,
            "login_success_entries_page": login_success_entries_page,
            "login_failure_entries": login_failure_entries,
            "login_failure_entries_page": login_failure_entries_page,
            "login_failure_ip_statuses": login_failure_ip_statuses,
            "cloudflare_ip_blocks": cloudflare_ip_blocks,
            "cloudflare_ip_blocks_page": cloudflare_ip_blocks_page,
            "cloudflare_ip_blocking_configured": cloudflare_ip_blocking_configured(settings),
            "passkeys": list_passkeys(db),
            "passkey_origin": settings.passkey_origin,
            "passkey_rp_id": settings.passkey_rp_id,
            "manual_odometer_result": _api_test_result(
                odometer_test,
                odometer_message,
                odometer_value,
            ),
            "eia_test_result": _api_test_result(eia_test, eia_message, eia_value),
            "restore_result": _api_test_result(
                restore_test,
                restore_message,
                restore_value,
            ),
            "cloudflare_block_result": _api_test_result(
                cloudflare_block_test,
                cloudflare_block_message,
                cloudflare_block_value,
            ),
            "backup_restore_enabled": backup_restore_enabled,
            "automatic_backups_enabled": settings.automatic_backups_enabled,
            "automatic_backup_dir": settings.automatic_backup_dir,
            "automatic_backups": automatic_backups,
            "backup_upload_max_mb": settings.max_backup_restore_bytes // (1024 * 1024),
        },
    )


@router.post("/diagnostics/passkeys/register/options")
def diagnostics_passkey_registration_options(
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    """Return WebAuthn registration options for the authenticated Diagnostics page."""

    _require_diagnostics_security_auth(request)
    settings = get_settings()
    try:
        options_json = begin_passkey_registration(db, request, settings)
    except PasskeyCeremonyError as exc:
        return _passkey_error_response(str(exc), 400)
    return Response(content=options_json, media_type="application/json")


@router.post("/diagnostics/passkeys/register/verify")
async def diagnostics_passkey_registration_verify(
    request: Request,
    db: Session = Depends(get_db),
) -> JSONResponse:
    """Verify a WebAuthn registration response and store the new passkey."""

    _require_diagnostics_security_auth(request)
    settings = get_settings()
    payload = await _json_object_payload(request)
    try:
        passkey = finish_passkey_registration(db, request, payload, settings)
    except PasskeyCeremonyError as exc:
        db.rollback()
        return _passkey_error_response(str(exc), 400)
    db.commit()
    logger.warning("Registered web passkey passkey_id=%s username=%s", passkey.id, passkey.username)
    return JSONResponse({"status": "created", "passkey_id": passkey.id})


@router.post("/diagnostics/passkeys/{passkey_id}/delete")
def diagnostics_delete_passkey(
    request: Request,
    passkey_id: int,
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Remove one configured passkey from Diagnostics."""

    _require_diagnostics_security_auth(request)
    passkey = db.get(PasskeyCredential, passkey_id)
    if passkey is None:
        raise HTTPException(status_code=404, detail="Passkey not found.")
    logger.warning(
        "Deleted web passkey passkey_id=%s username=%s",
        passkey.id,
        passkey.username,
    )
    db.delete(passkey)
    db.commit()
    return RedirectResponse(url="/diagnostics#passkeys", status_code=303)


@router.post("/diagnostics/login-failures/hide")
def hide_login_failure_entry(
    request: Request,
    entry_id: str = Form(...),
    client_ip: str = Form(default=""),
    occurred_at_utc: str = Form(default=""),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Hide one failed-login audit entry from Diagnostics without editing the raw log."""

    _require_diagnostics_security_auth(request)
    cleaned_entry_id = entry_id.strip().lower()
    if not re.fullmatch(r"[a-f0-9]{64}", cleaned_entry_id):
        raise HTTPException(status_code=400, detail="Invalid failed-login entry ID.")
    existing = db.scalar(
        select(HiddenLoginFailure)
        .where(HiddenLoginFailure.entry_id == cleaned_entry_id)
        .limit(1)
    )
    if existing is None:
        db.add(
            HiddenLoginFailure(
                entry_id=cleaned_entry_id,
                client_ip=(normalize_ip_address(client_ip) or client_ip.strip())[:45],
                occurred_at_utc=occurred_at_utc.strip()[:40],
            )
        )
        db.commit()
    return RedirectResponse(url="/diagnostics#login-failures", status_code=303)


@router.post("/diagnostics/cloudflare-blocks/block")
def block_login_ip_form(
    request: Request,
    ip_address: str = Form(...),
    reason: str = Form(default=""),
    result_anchor: str = Form(default="login-failures"),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Create an app-managed Cloudflare block from a Diagnostics action."""

    _require_diagnostics_security_auth(request)
    redirect_anchor = (
        "cloudflare-blocked-ips"
        if result_anchor == "cloudflare-blocked-ips"
        else "login-failures"
    )
    normalized_ip = normalize_ip_address(ip_address)
    if normalized_ip is None:
        return _diagnostics_redirect(
            redirect_anchor,
            {
                "cloudflare_block_test": "fail",
                "cloudflare_block_message": "Cannot block an invalid IP address.",
            },
        )
    cleaned_reason = reason.strip()
    if not cleaned_reason and redirect_anchor == "cloudflare-blocked-ips":
        return _diagnostics_redirect(
            redirect_anchor,
            {
                "cloudflare_block_test": "fail",
                "cloudflare_block_message": "A block reason is required.",
                "cloudflare_block_value": normalized_ip,
            },
        )
    block_reason = (
        cleaned_reason[:160] if cleaned_reason else "Diagnostics failed-login row block button"
    )
    try:
        block = _create_app_cloudflare_block(
            db,
            normalized_ip,
            source="manual",
            reason=block_reason,
        )
    except CloudflareBlockError as exc:
        return _diagnostics_redirect(
            redirect_anchor,
            {
                "cloudflare_block_test": "fail",
                "cloudflare_block_message": str(exc),
            },
        )
    return _diagnostics_redirect(
        redirect_anchor,
        {
            "cloudflare_block_test": "pass",
            "cloudflare_block_message": "Cloudflare IP block is active.",
            "cloudflare_block_value": block.ip_address,
        },
    )


@router.post("/diagnostics/cloudflare-blocks/unblock")
def unblock_login_ip_form(
    request: Request,
    ip_address: str = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Remove one app-managed Cloudflare block and its local block-list row."""

    _require_diagnostics_security_auth(request)
    normalized_ip = normalize_ip_address(ip_address)
    if normalized_ip is None:
        return _diagnostics_redirect(
            "cloudflare-blocked-ips",
            {
                "cloudflare_block_test": "fail",
                "cloudflare_block_message": "Cannot unblock an invalid IP address.",
            },
        )
    block = _cloudflare_block_for_ip(db, normalized_ip)
    if block is None:
        return _diagnostics_redirect(
            "cloudflare-blocked-ips",
            {
                "cloudflare_block_test": "pass",
                "cloudflare_block_message": "IP address is not in the app-managed block list.",
                "cloudflare_block_value": normalized_ip,
            },
        )
    try:
        _remove_app_cloudflare_block(db, block)
    except CloudflareBlockError as exc:
        return _diagnostics_redirect(
            "cloudflare-blocked-ips",
            {
                "cloudflare_block_test": "fail",
                "cloudflare_block_message": str(exc),
                "cloudflare_block_value": normalized_ip,
            },
        )
    return _diagnostics_redirect(
        "cloudflare-blocked-ips",
        {
            "cloudflare_block_test": "pass",
            "cloudflare_block_message": "Cloudflare IP block was removed.",
            "cloudflare_block_value": normalized_ip,
        },
    )


@router.get("/diagnostics/backup")
def download_full_backup(
    request: Request,
    db: Session = Depends(get_db),
) -> Response:
    _require_backup_restore_auth(request)
    backup = create_full_backup(db)
    logger.warning(
        "Created full database backup filename=%s total_rows=%s",
        backup.filename,
        backup.total_rows,
    )
    return Response(
        content=backup.content,
        media_type=BACKUP_MEDIA_TYPE,
        headers={
            "Content-Disposition": f'attachment; filename="{backup.filename}"',
            "Cache-Control": "no-store",
        },
    )


@router.post("/diagnostics/restore")
async def restore_full_backup_form(
    request: Request,
    backup_file: UploadFile = File(...),
    confirmation: str = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    _require_backup_restore_auth(request)
    settings = get_settings()
    if confirmation.strip() != "RESTORE":
        return _diagnostics_redirect(
            "data-backup",
            {
                "restore_test": "fail",
                "restore_message": "Type RESTORE to confirm full database restore.",
            },
        )

    content = await backup_file.read(settings.max_backup_restore_bytes + 1)
    await backup_file.close()
    if len(content) > settings.max_backup_restore_bytes:
        max_upload_mb = settings.max_backup_restore_bytes // (1024 * 1024)
        return _diagnostics_redirect(
            "data-backup",
            {
                "restore_test": "fail",
                "restore_message": f"Backup file is larger than {max_upload_mb} MB.",
            },
        )

    try:
        summary = restore_full_backup(db, content)
    except BackupValidationError as exc:
        logger.warning("Rejected full database restore upload: %s", exc)
        return _diagnostics_redirect(
            "data-backup",
            {
                "restore_test": "fail",
                "restore_message": str(exc),
            },
        )
    except Exception:
        logger.exception("Full database restore failed unexpectedly")
        return _diagnostics_redirect(
            "data-backup",
            {
                "restore_test": "fail",
                "restore_message": (
                    "Restore failed. Check the container console logs before trying again."
                ),
            },
        )

    return _diagnostics_redirect(
        "data-backup",
        {
            "restore_test": "pass",
            "restore_message": "Full database restore completed.",
            "restore_value": (
                f"{summary.total_rows} rows restored across "
                f"{len(summary.table_counts)} tables."
            ),
        },
    )


@router.get("/diagnostics/automatic-backups/download")
def download_automatic_backup(
    request: Request,
    filename: str = Query(...),
) -> Response:
    """Download one retained automatic backup after the same checks used for restore."""

    _require_backup_restore_auth(request)
    settings = get_settings()
    backup_filename = filename.strip()
    try:
        content = read_automatic_backup_content(
            settings.automatic_backup_dir,
            backup_filename,
            max_bytes=settings.max_backup_restore_bytes,
        )
    except BackupValidationError as exc:
        logger.warning(
            "Rejected automatic backup download filename=%s error=%s",
            backup_filename,
            exc,
        )
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    logger.warning(
        "Downloaded automatic Trip Tracker backup filename=%s size_bytes=%s",
        backup_filename,
        len(content),
    )
    return Response(
        content=content,
        media_type=BACKUP_MEDIA_TYPE,
        headers={
            "Content-Disposition": f'attachment; filename="{backup_filename}"',
            "Cache-Control": "no-store",
        },
    )


@router.post("/diagnostics/automatic-backups/restore")
def restore_automatic_backup_form(
    request: Request,
    filename: str = Form(...),
    confirmation: str = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Restore a retained automatic backup selected from the Diagnostics page."""

    _require_backup_restore_auth(request)
    settings = get_settings()
    backup_filename = filename.strip()
    if confirmation.strip() != "RESTORE":
        return _diagnostics_redirect(
            "automatic-backups",
            {
                "restore_test": "fail",
                "restore_message": "Type RESTORE to confirm automatic backup restore.",
            },
        )

    try:
        content = read_automatic_backup_content(
            settings.automatic_backup_dir,
            backup_filename,
            max_bytes=settings.max_backup_restore_bytes,
        )
        summary = restore_full_backup(db, content)
    except BackupValidationError as exc:
        logger.warning(
            "Rejected automatic backup restore filename=%s error=%s",
            backup_filename,
            exc,
        )
        return _diagnostics_redirect(
            "automatic-backups",
            {
                "restore_test": "fail",
                "restore_message": str(exc),
            },
        )
    except Exception:
        logger.exception(
            "Automatic Trip Tracker restore failed unexpectedly filename=%s",
            backup_filename,
        )
        return _diagnostics_redirect(
            "automatic-backups",
            {
                "restore_test": "fail",
                "restore_message": (
                    "Restore failed. Check the container console logs before trying again."
                ),
            },
        )

    return _diagnostics_redirect(
        "automatic-backups",
        {
            "restore_test": "pass",
            "restore_message": "Automatic backup restore completed.",
            "restore_value": (
                f"{summary.total_rows} rows restored across "
                f"{len(summary.table_counts)} tables."
            ),
        },
    )


@router.get("/diagnostics/logs/login-failures")
def download_login_failure_log(db: Session = Depends(get_db)) -> Response:
    """Export database-backed login audit records as JSON Lines."""

    return Response(
        content=login_audit_json_lines(db),
        media_type="application/jsonl; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="trip-tracker-login-audits.jsonl"',
            "Cache-Control": "no-store",
        },
    )


@router.post("/diagnostics/odometer")
def set_manual_odometer(
    odometer_miles: Decimal = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    if odometer_miles <= 0:
        return _diagnostics_redirect(
            "api-tests",
            {
                "odometer_test": "fail",
                "odometer_message": "Odometer reading must be greater than zero.",
            },
        )

    movement_state = owntracks_movement_diagnostics(db, state_change_limit=1).current_state
    inside_home = (
        movement_state.state == "waypoint"
        and (movement_state.site_name or "").casefold() == HOME_WAYPOINT_NAME.casefold()
    )
    if not inside_home:
        return _diagnostics_redirect(
            "api-tests",
            {
                "odometer_test": "fail",
                "odometer_message": (
                    "Manual odometer readings can only be saved while inside the Home waypoint."
                ),
            },
        )
    try:
        checkpoint = update_odometer_anchor_from_manual_reading(
            db,
            odometer_miles,
            recorded_at=datetime.now(UTC),
            align_trip_odometers=True,
        )
    except TripOdometerAdjustmentError as exc:
        db.rollback()
        return _diagnostics_redirect(
            "api-tests",
            {
                "odometer_test": "fail",
                "odometer_message": str(exc),
            },
        )

    return _diagnostics_redirect(
        "api-tests",
        {
            "odometer_test": "pass",
            "odometer_message": (
                "Manual odometer reading saved and trip odometers adjusted from Home."
            ),
            "odometer_value": f"{checkpoint.odometer_anchor_miles:.1f} miles",
        },
    )


@router.post("/diagnostics/odometer/emergency-rebuild")
def emergency_rebuild_odometer(
    odometer_miles: Decimal = Form(...),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Back up app data, then best-effort rebuild trip and master odometers."""

    if odometer_miles <= 0:
        return _diagnostics_redirect(
            "api-tests",
            {
                "odometer_test": "fail",
                "odometer_message": "Odometer reading must be greater than zero.",
            },
        )

    settings = get_settings()
    try:
        backup_result = create_automatic_backup(
            db,
            settings.automatic_backup_dir,
            reason="emergency",
        )
        rebuild_result = emergency_rebuild_trip_odometers_to_manual_reading(
            db,
            odometer_miles,
        )
        checkpoint = update_odometer_anchor_from_manual_reading(
            db,
            odometer_miles,
            recorded_at=datetime.now(UTC),
        )
    except TripOdometerAdjustmentError as exc:
        db.rollback()
        logger.warning("Emergency odometer rebuild rejected: %s", exc)
        return _diagnostics_redirect(
            "api-tests",
            {
                "odometer_test": "fail",
                "odometer_message": f"Emergency rebuild failed: {exc}",
            },
        )
    except (BackupValidationError, OSError, SQLAlchemyError):
        db.rollback()
        logger.exception("Emergency odometer rebuild failed")
        return _diagnostics_redirect(
            "api-tests",
            {
                "odometer_test": "fail",
                "odometer_message": (
                    "Emergency rebuild stopped because the safety backup or database update "
                    "failed. No odometer changes were saved."
                ),
            },
        )

    logger.warning(
        "Emergency odometer rebuild completed backup=%s trips=%s preserved_gaps=%s "
        "reconstructed_gaps=%s discarded_gaps=%s",
        backup_result.backup_file.filename,
        rebuild_result.adjusted_trip_count,
        rebuild_result.preserved_gap_count,
        rebuild_result.reconstructed_gap_count,
        rebuild_result.discarded_gap_count,
    )
    return _diagnostics_redirect(
        "api-tests",
        {
            "odometer_test": "pass",
            "odometer_message": (
                f"Emergency rebuild adjusted {rebuild_result.adjusted_trip_count} trips; "
                f"preserved {rebuild_result.preserved_gap_count}, reconstructed "
                f"{rebuild_result.reconstructed_gap_count}, and discarded "
                f"{rebuild_result.discarded_gap_count} odometer gaps."
            ),
            "odometer_value": (
                f"Current odometer {checkpoint.odometer_anchor_miles:.1f} miles; "
                f"backup {backup_result.backup_file.filename}"
            ),
        },
    )


@router.post("/diagnostics/test/eia")
def test_eia_api() -> RedirectResponse:
    settings = get_settings()
    try:
        reading = EiaSeriesProvider().current_regular_price(settings.gas_price_state)
    except GasPriceUnavailable as exc:
        return _diagnostics_redirect(
            "api-tests",
            {
                "eia_test": "fail",
                "eia_message": str(exc),
            },
        )
    except Exception:
        logger.warning("EIA diagnostic test failed with an unexpected provider error")
        return _diagnostics_redirect(
            "api-tests",
            {
                "eia_test": "fail",
                "eia_message": (
                    "EIA test failed. Check the API key, series ID, and container console logs."
                ),
            },
        )

    return _diagnostics_redirect(
        "api-tests",
        {
            "eia_test": "pass",
            "eia_message": "EIA returned a current regular gas price reading.",
            "eia_value": f"${reading.price_per_gallon:.3f} on {reading.observed_on}",
        },
    )


@router.post("/reports/{year}/{month}")
def report_form(year: int, month: int, db: Session = Depends(get_db)) -> Response:
    _validate_month(month)
    try:
        report = generate_monthly_pdf(db, year, month)
    except GasPriceUnavailable as exc:
        logger.warning("Report generation unavailable year=%s month=%s error=%s", year, month, exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.info(
        "Generated report from web form year=%s month=%s filename=%s",
        year,
        month,
        report.filename,
    )
    return Response(
        content=report.content,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{report.filename}"'},
    )
