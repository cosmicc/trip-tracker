import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from trip_tracker import database
from trip_tracker.config import Settings, get_settings
from trip_tracker.models import GasPriceSnapshot, MonthlyGasPrice
from trip_tracker.services.timezone import local_today

logger = logging.getLogger(__name__)


class GasPriceUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class GasPriceReading:
    state: str
    grade: str
    price_per_gallon: Decimal
    source: str
    source_detail: str
    observed_on: date


class GasPriceProvider:
    source_name = "base"

    def current_regular_price(self, state: str) -> GasPriceReading:
        raise NotImplementedError


class AaaMichiganGasPriceProvider(GasPriceProvider):
    source_name = "aaa_current"
    url = "https://gasprices.aaa.com/?state=MI"
    headers = {"User-Agent": "TripTracker/0.1"}

    def current_regular_price(self, state: str) -> GasPriceReading:
        if state.upper() != "MI":
            raise GasPriceUnavailable("AAA provider is configured only for Michigan")

        logger.info("Fetching AAA Michigan gas price from %s", self.url)
        response = httpx.get(self.url, headers=self.headers, timeout=20)
        response.raise_for_status()
        match = re.search(r"Current Avg\.</td>\s*<td>\$([0-9]+\.[0-9]+)</td>", response.text)
        if match is None:
            match = re.search(r"Current Avg\.\s*\$([0-9]+\.[0-9]+)", response.text)
        if not match:
            raise GasPriceUnavailable("Could not locate the Michigan current regular gas price")

        return GasPriceReading(
            state="MI",
            grade="regular",
            price_per_gallon=Decimal(match.group(1)).quantize(Decimal("0.001")),
            source=self.source_name,
            source_detail=self.url,
            observed_on=local_today(),
        )


class EiaSeriesProvider(GasPriceProvider):
    source_name = "eia_series"

    def current_regular_price(self, state: str) -> GasPriceReading:
        settings = get_settings()
        if not settings.eia_api_key or not settings.eia_series_id:
            raise GasPriceUnavailable("EIA_API_KEY and EIA_SERIES_ID are required")

        url = f"https://api.eia.gov/v2/seriesid/{settings.eia_series_id}"
        logger.info("Fetching EIA gas price series=%s", settings.eia_series_id)
        response = httpx.get(url, params={"api_key": settings.eia_api_key}, timeout=20)
        response.raise_for_status()
        data = response.json()
        rows = data.get("response", {}).get("data", [])
        if not rows:
            raise GasPriceUnavailable("EIA returned no rows for the configured series")
        row = rows[0]
        value = row.get("value")
        if value is None:
            raise GasPriceUnavailable("EIA latest row has no value")

        return GasPriceReading(
            state=state.upper(),
            grade="regular",
            price_per_gallon=Decimal(str(value)).quantize(Decimal("0.001")),
            source=self.source_name,
            source_detail=f"EIA series {settings.eia_series_id}",
            observed_on=local_today(),
        )


def configured_provider() -> GasPriceProvider:
    source = get_settings().gas_price_source
    logger.debug("Selecting gas price provider source=%s", source)
    if source == "eia_series":
        return EiaSeriesProvider()
    return AaaMichiganGasPriceProvider()


def save_daily_snapshot(db: Session, reading: GasPriceReading) -> GasPriceSnapshot:
    stmt = (
        select(GasPriceSnapshot)
        .where(GasPriceSnapshot.observed_on == reading.observed_on)
        .where(GasPriceSnapshot.state == reading.state)
        .where(GasPriceSnapshot.grade == reading.grade)
        .where(GasPriceSnapshot.source == reading.source)
    )
    snapshot = db.scalar(stmt)
    if snapshot is None:
        logger.debug(
            "Creating gas price snapshot state=%s date=%s source=%s",
            reading.state,
            reading.observed_on,
            reading.source,
        )
        snapshot = GasPriceSnapshot(
            observed_on=reading.observed_on,
            state=reading.state,
            grade=reading.grade,
            price_per_gallon=reading.price_per_gallon,
            source=reading.source,
            source_detail=reading.source_detail,
        )
        db.add(snapshot)
    else:
        logger.debug(
            "Updating gas price snapshot id=%s state=%s date=%s source=%s",
            snapshot.id,
            snapshot.state,
            snapshot.observed_on,
            snapshot.source,
        )
        snapshot.price_per_gallon = reading.price_per_gallon
        snapshot.source_detail = reading.source_detail
    db.commit()
    db.refresh(snapshot)
    logger.info(
        "Saved gas price snapshot state=%s date=%s price=%s source=%s",
        snapshot.state,
        snapshot.observed_on,
        snapshot.price_per_gallon,
        snapshot.source,
    )
    return snapshot


def fetch_and_save_current_snapshot(db: Session) -> GasPriceSnapshot:
    settings = get_settings()
    reading = configured_provider().current_regular_price(settings.gas_price_state)
    return save_daily_snapshot(db, reading)


def upsert_monthly_price(
    db: Session,
    *,
    year: int,
    month: int,
    state: str,
    average_price_per_gallon: Decimal,
    buffer_per_gallon: Decimal,
    source: str,
    source_detail: str,
) -> MonthlyGasPrice:
    state = state.upper()
    effective_rate = (average_price_per_gallon + buffer_per_gallon).quantize(Decimal("0.001"))
    stmt = (
        select(MonthlyGasPrice)
        .where(MonthlyGasPrice.year == year)
        .where(MonthlyGasPrice.month == month)
        .where(MonthlyGasPrice.state == state)
    )
    monthly = db.scalar(stmt)
    if monthly is None:
        logger.debug(
            "Creating monthly gas price state=%s year=%s month=%s source=%s",
            state,
            year,
            month,
            source,
        )
        monthly = MonthlyGasPrice(
            year=year,
            month=month,
            state=state,
            average_price_per_gallon=average_price_per_gallon,
            buffer_per_gallon=buffer_per_gallon,
            effective_rate=effective_rate,
            source=source,
            source_detail=source_detail,
        )
        db.add(monthly)
    else:
        logger.debug(
            "Updating monthly gas price id=%s state=%s year=%s month=%s source=%s",
            monthly.id,
            monthly.state,
            monthly.year,
            monthly.month,
            source,
        )
        monthly.average_price_per_gallon = average_price_per_gallon
        monthly.buffer_per_gallon = buffer_per_gallon
        monthly.effective_rate = effective_rate
        monthly.source = source
        monthly.source_detail = source_detail
    db.commit()
    db.refresh(monthly)
    logger.info(
        "Saved monthly gas price state=%s year=%s month=%s average=%s source=%s",
        monthly.state,
        monthly.year,
        monthly.month,
        monthly.average_price_per_gallon,
        monthly.source,
    )
    return monthly


def upsert_manual_monthly_price(
    db: Session,
    *,
    year: int,
    month: int,
    state: str,
    average_price_per_gallon: Decimal,
    buffer_per_gallon: Decimal,
    source_detail: str = "manual",
) -> MonthlyGasPrice:
    return upsert_monthly_price(
        db,
        year=year,
        month=month,
        state=state,
        average_price_per_gallon=average_price_per_gallon,
        buffer_per_gallon=buffer_per_gallon,
        source="manual",
        source_detail=source_detail,
    )


def monthly_price_from_snapshots(db: Session, year: int, month: int, state: str) -> Decimal | None:
    start = date(year, month, 1)
    end = date(year + int(month == 12), 1 if month == 12 else month + 1, 1)
    stmt = (
        select(func.avg(GasPriceSnapshot.price_per_gallon))
        .where(GasPriceSnapshot.observed_on >= start)
        .where(GasPriceSnapshot.observed_on < end)
        .where(GasPriceSnapshot.state == state.upper())
        .where(GasPriceSnapshot.grade == "regular")
    )
    value = db.scalar(stmt)
    if value is None:
        return None
    return Decimal(str(value)).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


def get_or_create_monthly_price(db: Session, year: int, month: int) -> MonthlyGasPrice:
    settings = get_settings()
    state = settings.gas_price_state.upper()
    stmt = (
        select(MonthlyGasPrice)
        .where(MonthlyGasPrice.year == year)
        .where(MonthlyGasPrice.month == month)
        .where(MonthlyGasPrice.state == state)
    )
    monthly = db.scalar(stmt)
    if monthly is not None:
        logger.debug(
            "Using saved monthly gas price id=%s state=%s year=%s month=%s",
            monthly.id,
            monthly.state,
            monthly.year,
            monthly.month,
        )
        return monthly

    today = local_today()
    if today.year == year and today.month == month:
        fetch_and_save_current_snapshot(db)

    average = monthly_price_from_snapshots(db, year, month, state)
    if average is None:
        logger.warning(
            "No monthly gas price available state=%s year=%s month=%s",
            state,
            year,
            month,
        )
        raise GasPriceUnavailable(
            "No monthly gas price is available. Add one manually or collect daily snapshots."
        )

    return upsert_monthly_price(
        db,
        year=year,
        month=month,
        state=state,
        average_price_per_gallon=average,
        buffer_per_gallon=settings.gas_price_buffer,
        source="snapshot_average",
        source_detail="average of stored daily snapshots",
    )


def refresh_current_monthly_price(db: Session) -> MonthlyGasPrice:
    settings = get_settings()
    snapshot = fetch_and_save_current_snapshot(db)
    average = monthly_price_from_snapshots(
        db,
        snapshot.observed_on.year,
        snapshot.observed_on.month,
        snapshot.state,
    )
    if average is None:
        raise GasPriceUnavailable("No gas price snapshots are available for the current month")

    return upsert_monthly_price(
        db,
        year=snapshot.observed_on.year,
        month=snapshot.observed_on.month,
        state=snapshot.state,
        average_price_per_gallon=average,
        buffer_per_gallon=settings.gas_price_buffer,
        source="online_snapshot_average",
        source_detail=f"average of stored online snapshots; latest {snapshot.source_detail}",
    )


def run_gas_snapshot_once() -> MonthlyGasPrice:
    """Fetch the current gas price and refresh the current monthly average."""

    with database.SessionLocal() as db:
        monthly = refresh_current_monthly_price(db)
    logger.info(
        "Refreshed scheduled gas price state=%s year=%s month=%s average=%s",
        monthly.state,
        monthly.year,
        monthly.month,
        monthly.average_price_per_gallon,
    )
    return monthly


async def gas_snapshot_scheduler(application_settings: Settings) -> None:
    """Run gas price snapshots in the app container until shutdown."""

    if application_settings.gas_snapshot_run_on_startup:
        await _run_scheduled_gas_snapshot()

    while True:
        await asyncio.sleep(application_settings.gas_snapshot_interval_seconds)
        await _run_scheduled_gas_snapshot()


async def _run_scheduled_gas_snapshot() -> None:
    """Run one scheduled gas snapshot without stopping the web app on failure."""

    try:
        if not await asyncio.to_thread(database.database_is_reachable):
            logger.info("Scheduled gas snapshot paused because database is unavailable")
            return
        await asyncio.to_thread(run_gas_snapshot_once)
    except asyncio.CancelledError:
        raise
    except GasPriceUnavailable as exc:
        logger.warning("Scheduled gas snapshot unavailable: %s", exc)
    except Exception:
        logger.exception("Scheduled gas snapshot failed")
