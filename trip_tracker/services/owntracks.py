import base64
import binascii
import json
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from nacl.exceptions import CryptoError
from nacl.secret import SecretBox
from sqlalchemy import select
from sqlalchemy.orm import Session

from trip_tracker.config import Settings, get_settings
from trip_tracker.models import OwnTracksLocation, Site
from trip_tracker.services.timezone import datetime_to_local_date
from trip_tracker.services.trip_processor import run_automatic_trip_processing

logger = logging.getLogger(__name__)


class OwnTracksError(ValueError):
    pass


class EmptyOwnTracksPayload(OwnTracksError):
    pass


class UnsupportedOwnTracksType(OwnTracksError):
    pass


class EncryptedOwnTracksPayloadError(OwnTracksError):
    pass


class OwnTracksEncryptionNotConfigured(EncryptedOwnTracksPayloadError):
    pass


@dataclass(frozen=True)
class TopicIdentity:
    topic: str | None
    user: str | None
    device: str | None


@dataclass(frozen=True)
class OwnTracksLocationMessage:
    payload: dict
    identity: TopicIdentity
    captured_at: datetime
    latitude: Decimal
    longitude: Decimal
    tracker_id: str | None
    accuracy_m: int | None
    battery_percent: int | None


@dataclass(frozen=True)
class OwnTracksProcessResult:
    location: OwnTracksLocation | None
    site: Site | None


def identity_from_topic(topic: str | None) -> TopicIdentity:
    if not topic:
        return TopicIdentity(topic=None, user=None, device=None)
    parts = topic.split("/")
    if len(parts) >= 3 and parts[0] == "owntracks":
        return TopicIdentity(topic=topic, user=parts[1] or None, device=parts[2] or None)
    return TopicIdentity(topic=topic, user=None, device=None)


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise OwnTracksError(f"Expected integer-compatible value, got {value!r}") from exc


def _decode_payload(body: bytes) -> dict:
    if not body:
        raise EmptyOwnTracksPayload("OwnTracks sent an empty payload")

    try:
        payload = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise OwnTracksError("OwnTracks payload is not valid JSON") from exc

    if not isinstance(payload, dict):
        raise OwnTracksError("OwnTracks payload must be a JSON object")
    return payload


def _owntracks_secretbox_key(settings: Settings | None = None) -> bytes:
    """Return the OwnTracks secret padded to libsodium SecretBox's 32-byte key size."""

    active_settings = settings or get_settings()
    key = active_settings.owntracks_encryption_key.encode("utf-8")
    if not key:
        raise OwnTracksEncryptionNotConfigured("OWNTRACKS_ENCRYPTION_KEY is not configured")
    if len(key) > SecretBox.KEY_SIZE:
        raise EncryptedOwnTracksPayloadError("OWNTRACKS_ENCRYPTION_KEY is longer than 32 bytes")
    return key.ljust(SecretBox.KEY_SIZE, b"\0")


def decrypt_owntracks_payload(body: bytes, settings: Settings | None = None) -> bytes:
    """Decrypt an OwnTracks encrypted HTTP payload into the original JSON bytes."""

    wrapper = _decode_payload(body)
    if wrapper.get("_type") != "encrypted":
        raise EncryptedOwnTracksPayloadError(
            "OwnTracks payload encryption is required when OWNTRACKS_ENCRYPTION_KEY is set"
        )

    encoded_data = wrapper.get("data")
    if not isinstance(encoded_data, str) or not encoded_data.strip():
        raise EncryptedOwnTracksPayloadError("Encrypted OwnTracks payload is missing data")

    try:
        encrypted = base64.b64decode(encoded_data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise EncryptedOwnTracksPayloadError(
            "Encrypted OwnTracks payload data is not valid base64"
        ) from exc

    try:
        cleartext = SecretBox(_owntracks_secretbox_key(settings)).decrypt(encrypted)
    except CryptoError as exc:
        raise EncryptedOwnTracksPayloadError(
            "Encrypted OwnTracks payload could not be decrypted"
        ) from exc

    clear_payload = _decode_payload(cleartext)
    clear_payload["_decrypted"] = True
    return json.dumps(clear_payload, separators=(",", ":")).encode("utf-8")


def _location_message_from_payload(
    payload: dict,
    *,
    topic: str | None = None,
    user: str | None = None,
    device: str | None = None,
) -> OwnTracksLocationMessage:
    payload_type = payload.get("_type")
    if payload_type not in {"location", "transition"}:
        raise UnsupportedOwnTracksType(f"Ignoring OwnTracks payload type {payload_type!r}")

    if "lat" not in payload or "lon" not in payload or "tst" not in payload:
        raise OwnTracksError("OwnTracks location payload requires lat, lon, and tst")

    payload_topic = payload.get("topic") or topic
    identity = identity_from_topic(str(payload_topic) if payload_topic else None)
    identity = TopicIdentity(
        topic=identity.topic,
        user=user or identity.user,
        device=device or identity.device,
    )

    captured_at = datetime.fromtimestamp(int(payload["tst"]), tz=UTC)

    return OwnTracksLocationMessage(
        payload=payload,
        identity=identity,
        captured_at=captured_at,
        latitude=Decimal(str(payload["lat"])),
        longitude=Decimal(str(payload["lon"])),
        tracker_id=str(payload["tid"]) if payload.get("tid") else None,
        accuracy_m=_optional_int(payload.get("acc")),
        battery_percent=_optional_int(payload.get("batt")),
    )


def parse_owntracks_location(
    body: bytes,
    *,
    topic: str | None = None,
    user: str | None = None,
    device: str | None = None,
) -> OwnTracksLocationMessage:
    return _location_message_from_payload(
        _decode_payload(body),
        topic=topic,
        user=user,
        device=device,
    )


def validate_owntracks_payload_for_ingest(
    body: bytes,
    *,
    topic: str | None = None,
    user: str | None = None,
    device: str | None = None,
) -> None:
    """Validate that an OwnTracks HTTP payload is safe to store in PostgreSQL."""

    payload = _decode_payload(body)
    if payload.get("_type") == "waypoint":
        return
    _location_message_from_payload(payload, topic=topic, user=user, device=device)


def _first_region_name(payload: dict) -> str | None:
    description = payload.get("desc")
    if description:
        return str(description).strip() or None

    regions = payload.get("inregions")
    if isinstance(regions, list):
        for region in regions:
            name = str(region).strip()
            if name:
                return name
    return None


def _region_id(payload: dict) -> str | None:
    region_id = payload.get("rid")
    if region_id is None:
        return None
    return str(region_id).strip() or None


def _transition_event(payload: dict) -> str | None:
    if payload.get("_type") != "transition":
        return None
    event = str(payload.get("event") or "").strip().casefold()
    if event in {"enter", "arrive", "arrival"}:
        return "enter"
    if event in {"leave", "exit", "departure"}:
        return "leave"
    return None


def _payload_datetime(payload: dict) -> datetime:
    for key in ("tst", "wtst"):
        try:
            value = payload.get(key)
            if value is not None:
                return datetime.fromtimestamp(int(value), tz=UTC)
        except (TypeError, ValueError, OSError):
            continue
    return datetime.now(UTC)


def _datetime_for_compare(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _site_radius_from_payload(payload: dict) -> int:
    settings = get_settings()
    try:
        return int(payload.get("rad") or settings.owntracks_default_site_radius_m)
    except (TypeError, ValueError):
        return settings.owntracks_default_site_radius_m


def _payload_date(payload: dict) -> date:
    return datetime_to_local_date(_payload_datetime(payload))


def _run_trip_processing(db: Session, payload: dict) -> None:
    touched_date = _payload_date(payload)
    finalize_completed_days = touched_date >= datetime_to_local_date(datetime.now(UTC))
    try:
        run_automatic_trip_processing(
            db,
            touched_date=touched_date,
            finalize_completed_days=finalize_completed_days,
        )
    except Exception:
        logger.exception("Automatic trip processing failed after OwnTracks payload")


def sync_site_from_owntracks_payload(
    db: Session,
    payload: dict,
    *,
    latitude: Decimal | None = None,
    longitude: Decimal | None = None,
) -> Site | None:
    settings = get_settings()
    if not settings.owntracks_sync_waypoints:
        return None
    if payload.get("_type") != "waypoint":
        logger.debug("Skipping waypoint sync for payload_type=%s", payload.get("_type"))
        return None

    name = _first_region_name(payload)
    if name is None:
        logger.warning("OwnTracks waypoint payload missing waypoint name")
        return None
    region_id = _region_id(payload)

    payload_latitude = payload.get("lat")
    payload_longitude = payload.get("lon")
    site_latitude = Decimal(str(payload_latitude)) if payload_latitude is not None else latitude
    site_longitude = Decimal(str(payload_longitude)) if payload_longitude is not None else longitude
    if site_latitude is None or site_longitude is None:
        logger.warning("OwnTracks waypoint payload missing coordinates name=%s", name)
        return None

    site = None
    if region_id is not None:
        site = db.scalar(select(Site).where(Site.owntracks_region_id == region_id))
    if site is None:
        site = db.scalar(select(Site).where(Site.name == name))
    if site is None:
        site = Site(
            name=name,
            owntracks_region_id=region_id,
            latitude=site_latitude,
            longitude=site_longitude,
            radius_m=_site_radius_from_payload(payload),
            active=True,
            created_at=_payload_datetime(payload),
        )
        db.add(site)
        logger.info(
            "Created OwnTracks waypoint name=%s region_id=%s radius_m=%s event_time=%s",
            site.name,
            site.owntracks_region_id or "",
            site.radius_m,
            site.created_at.isoformat(),
        )
        return site

    site.name = name
    site.owntracks_region_id = region_id or site.owntracks_region_id
    site.latitude = site_latitude
    site.longitude = site_longitude
    site.radius_m = _site_radius_from_payload(payload)
    site.active = True
    logger.info(
        "Updated OwnTracks waypoint id=%s name=%s region_id=%s radius_m=%s",
        site.id,
        site.name,
        site.owntracks_region_id or "",
        site.radius_m,
    )
    return site


def update_site_last_visit_from_transition(
    db: Session,
    payload: dict,
    captured_at: datetime,
) -> Site | None:
    if _transition_event(payload) != "enter":
        return None

    site = None
    region_id = _region_id(payload)
    if region_id is not None:
        site = db.scalar(select(Site).where(Site.owntracks_region_id == region_id))

    if site is None:
        name = _first_region_name(payload)
        if name is not None:
            site = db.scalar(select(Site).where(Site.name == name))

    if site is None:
        logger.warning(
            "OwnTracks enter transition did not match a waypoint region_id=%s name=%s",
            region_id or "",
            _first_region_name(payload) or "",
        )
        return None

    if site.last_visited_at is None or _datetime_for_compare(captured_at) > _datetime_for_compare(
        site.last_visited_at
    ):
        site.last_visited_at = captured_at
        logger.info(
            "Updated waypoint last visit site_id=%s name=%s captured_at=%s",
            site.id,
            site.name,
            captured_at.isoformat(),
        )
    return site


def store_owntracks_location(db: Session, message: OwnTracksLocationMessage) -> OwnTracksLocation:
    """Store one OwnTracks event, returning an existing row for an exact HTTP retry."""

    sync_site_from_owntracks_payload(
        db,
        message.payload,
        latitude=message.latitude,
        longitude=message.longitude,
    )
    candidates = db.scalars(
        select(OwnTracksLocation).where(
            OwnTracksLocation.captured_at == message.captured_at,
            OwnTracksLocation.latitude == message.latitude,
            OwnTracksLocation.longitude == message.longitude,
        )
    )
    for existing in candidates:
        if (
            existing.user == message.identity.user
            and existing.device == message.identity.device
            and existing.topic == message.identity.topic
            and existing.raw_payload == message.payload
        ):
            logger.info(
                "Accepted duplicate OwnTracks retry without inserting a second row id=%s "
                "captured_at=%s user=%s device=%s",
                existing.id,
                existing.captured_at.isoformat(),
                existing.user or "",
                existing.device or "",
            )
            return existing

    location = OwnTracksLocation(
        user=message.identity.user,
        device=message.identity.device,
        topic=message.identity.topic,
        tracker_id=message.tracker_id,
        captured_at=message.captured_at,
        received_at=datetime.now(UTC),
        latitude=message.latitude,
        longitude=message.longitude,
        accuracy_m=message.accuracy_m,
        battery_percent=message.battery_percent,
        raw_payload=message.payload,
    )
    db.add(location)
    db.commit()
    db.refresh(location)
    logger.info(
        "Stored OwnTracks event id=%s type=%s event=%s captured_at=%s user=%s device=%s topic=%s",
        location.id,
        message.payload.get("_type"),
        message.payload.get("event") or "",
        location.captured_at.isoformat(),
        location.user or "",
        location.device or "",
        location.topic or "",
    )
    logger.debug(
        "OwnTracks event details id=%s latitude=%s longitude=%s accuracy_m=%s battery_percent=%s",
        location.id,
        location.latitude,
        location.longitude,
        location.accuracy_m,
        location.battery_percent,
    )
    return location


def process_owntracks_payload(
    db: Session,
    body: bytes,
    *,
    topic: str | None = None,
    user: str | None = None,
    device: str | None = None,
) -> OwnTracksProcessResult:
    """Persist one validated HTTP payload and run best-effort trip processing."""

    payload = _decode_payload(body)
    payload_type = payload.get("_type")
    logger.debug("Processing OwnTracks payload type=%s", payload_type)

    if payload_type == "waypoint":
        site = sync_site_from_owntracks_payload(db, payload)
        db.commit()
        if site is not None:
            db.refresh(site)
        _run_trip_processing(db, payload)
        return OwnTracksProcessResult(location=None, site=site)

    message = _location_message_from_payload(payload, topic=topic, user=user, device=device)
    location = store_owntracks_location(db, message)
    _run_trip_processing(db, payload)
    return OwnTracksProcessResult(location=location, site=None)
