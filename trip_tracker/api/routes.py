import logging
from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from trip_tracker.api.deps import OWNTRACKS_RETRY_HEADERS, verify_owntracks_auth
from trip_tracker.database import SessionLocal, get_db, is_database_unavailable_error
from trip_tracker.database_migrations import run_migrations_once_on_reconnect
from trip_tracker.models import OwnTracksLocation, Site, Trip
from trip_tracker.schemas import MonthlyGasPriceCreate, TripUpdate, WaypointRead
from trip_tracker.services.gas_prices import (
    GasPriceUnavailable,
    fetch_and_save_current_snapshot,
    upsert_manual_monthly_price,
)
from trip_tracker.services.mileage import (
    resequence_month_trip_odometers_from,
    update_trip_details,
)
from trip_tracker.services.owntracks import (
    EmptyOwnTracksPayload,
    OwnTracksEncryptionNotConfigured,
    OwnTracksError,
    UnsupportedOwnTracksType,
    decrypt_owntracks_payload,
    process_owntracks_payload,
    validate_owntracks_payload_for_ingest,
)
from trip_tracker.services.pdf import generate_monthly_pdf
from trip_tracker.services.timezone import datetime_to_local
from trip_tracker.services.waypoints import owntracks_waypoints_json

router = APIRouter()
logger = logging.getLogger(__name__)


def get_owntracks_session_factory() -> Callable[[], Session]:
    """Return the DB session factory without opening a database connection."""

    return SessionLocal


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/owntracks")
@router.post("/owntracks/")
@router.post("/pub")
@router.post("/pub/")
async def owntracks_http(
    request: Request,
    session_factory=Depends(get_owntracks_session_factory),
) -> JSONResponse:
    verify_owntracks_auth(request)
    body = await request.body()
    try:
        body = decrypt_owntracks_payload(body)
        topic = request.query_params.get("topic")
        user = request.headers.get("x-limit-u") or request.query_params.get("u")
        device = request.headers.get("x-limit-d") or request.query_params.get("d")
        validate_owntracks_payload_for_ingest(body, topic=topic, user=user, device=device)
        try:
            run_migrations_once_on_reconnect()
        except Exception as exc:
            if is_database_unavailable_error(exc):
                logger.warning("OwnTracks ingestion paused while PostgreSQL is unavailable")
            else:
                logger.exception("OwnTracks ingestion paused because migrations are not ready")
            raise HTTPException(
                status_code=503,
                detail="Database is not ready; OwnTracks should retry later",
                headers=OWNTRACKS_RETRY_HEADERS,
            ) from exc
        with session_factory() as db:
            process_owntracks_payload(db, body, topic=topic, user=user, device=device)
    except EmptyOwnTracksPayload:
        logger.debug("Ignored empty OwnTracks payload")
        return JSONResponse(content=[])
    except UnsupportedOwnTracksType as exc:
        logger.debug("Ignored unsupported OwnTracks payload: %s", exc)
        return JSONResponse(content=[])
    except OwnTracksEncryptionNotConfigured as exc:
        logger.error("Rejected OwnTracks payload because encryption is not configured")
        raise HTTPException(
            status_code=503,
            detail=str(exc),
            headers=OWNTRACKS_RETRY_HEADERS,
        ) from exc
    except OwnTracksError as exc:
        logger.warning("Rejected OwnTracks payload: %s", exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        if is_database_unavailable_error(exc):
            logger.warning("OwnTracks payload could not be stored because database is unavailable")
            raise HTTPException(
                status_code=503,
                detail="Database is unavailable; OwnTracks should retry later",
                headers=OWNTRACKS_RETRY_HEADERS,
            ) from exc
        raise
    return JSONResponse(content=[], headers={"Cache-Control": "no-store"})


@router.get("/locations")
def locations(
    limit: int = Query(default=100, ge=1, le=1000),
    db: Session = Depends(get_db),
) -> list[dict]:
    stmt = select(OwnTracksLocation).order_by(OwnTracksLocation.captured_at.desc()).limit(limit)
    return [
        {
            "id": location.id,
            "user": location.user,
            "device": location.device,
            "captured_at": datetime_to_local(location.captured_at).isoformat(),
            "latitude": str(location.latitude),
            "longitude": str(location.longitude),
            "accuracy_m": location.accuracy_m,
        }
        for location in db.scalars(stmt)
    ]


@router.get("/sites", response_model=list[WaypointRead])
@router.get("/waypoints", response_model=list[WaypointRead])
def list_waypoints(db: Session = Depends(get_db)) -> list[Site]:
    return list(
        db.scalars(
            select(Site).order_by(
                Site.last_visited_at.desc().nulls_last(),
                Site.created_at.desc(),
                Site.name.asc(),
            )
        )
    )


@router.get("/waypoints/export")
def export_waypoints(db: Session = Depends(get_db)) -> Response:
    all_waypoints = list(
        db.scalars(
            select(Site).order_by(
                Site.last_visited_at.desc().nulls_last(),
                Site.created_at.desc(),
                Site.name.asc(),
            )
        )
    )
    return Response(
        content=owntracks_waypoints_json(all_waypoints),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="owntracks-waypoints.json"'},
    )


@router.patch("/trips/{trip_id}")
def update_trip(trip_id: int, update: TripUpdate, db: Session = Depends(get_db)) -> dict[str, str]:
    trip = db.get(Trip, trip_id)
    if trip is None:
        raise HTTPException(status_code=404, detail="Trip not found")
    original_miles = trip.miles
    update_trip_details(
        trip,
        update.origin_name,
        update.destination_name,
        update.miles,
        update.trip_date,
    )
    if update.miles is not None and trip.miles != original_miles:
        resequence_month_trip_odometers_from(db, trip)
    db.commit()
    logger.info(
        "Updated trip via API trip_id=%s origin=%s destination=%s miles=%s",
        trip.id,
        trip.origin_display_name,
        trip.destination_display_name,
        trip.miles,
    )
    return {"status": "updated"}


@router.post("/gas-prices/current")
def snapshot_current_gas_price(db: Session = Depends(get_db)) -> dict[str, str]:
    try:
        snapshot = fetch_and_save_current_snapshot(db)
    except GasPriceUnavailable as exc:
        logger.warning("Current gas price snapshot unavailable: %s", exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.info(
        "Saved current gas price snapshot via API date=%s price=%s",
        snapshot.observed_on,
        snapshot.price_per_gallon,
    )
    return {
        "status": "saved",
        "observed_on": snapshot.observed_on.isoformat(),
        "price_per_gallon": str(snapshot.price_per_gallon),
    }


@router.post("/gas-prices/monthly")
def manual_monthly_gas_price(
    payload: MonthlyGasPriceCreate,
    db: Session = Depends(get_db),
) -> dict[str, str]:
    monthly = upsert_manual_monthly_price(db, **payload.model_dump())
    logger.info(
        "Saved manual monthly gas price via API year=%s month=%s state=%s effective_rate=%s",
        monthly.year,
        monthly.month,
        monthly.state,
        monthly.effective_rate,
    )
    return {"status": "saved", "effective_rate": str(monthly.effective_rate)}


@router.post("/reports/{year}/{month}")
def generate_report(year: int, month: int, db: Session = Depends(get_db)) -> Response:
    if month < 1 or month > 12:
        raise HTTPException(status_code=400, detail="month must be 1 through 12")
    try:
        report = generate_monthly_pdf(db, year, month)
    except GasPriceUnavailable as exc:
        logger.warning("Report generation unavailable year=%s month=%s error=%s", year, month, exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.info(
        "Generated report via API year=%s month=%s filename=%s",
        year,
        month,
        report.filename,
    )
    return Response(
        content=report.content,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{report.filename}"'},
    )
