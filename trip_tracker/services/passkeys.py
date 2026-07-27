"""WebAuthn passkey registration and authentication helpers."""

import hashlib
from urllib.parse import urlsplit

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    AuthenticatorTransport,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from trip_tracker.config import Settings
from trip_tracker.models import PasskeyCredential, utc_now

PASSKEY_REGISTRATION_SESSION_KEY = "passkey_registration_context"
PASSKEY_AUTHENTICATION_SESSION_KEY = "passkey_authentication_context"


class PasskeyCeremonyError(ValueError):
    """Raised when a WebAuthn ceremony cannot be completed safely."""


def passkey_user_handle(settings: Settings) -> bytes:
    """Return a stable opaque WebAuthn user handle for the configured web-login account."""

    username = settings.web_login_username.strip()
    return hashlib.sha256(f"trip-tracker-passkey:{username}".encode()).digest()


def passkey_user_handle_text(settings: Settings) -> str:
    """Return the stored text representation of the configured user's WebAuthn handle."""

    return bytes_to_base64url(passkey_user_handle(settings))


def list_passkeys(db: Session) -> list[PasskeyCredential]:
    """Return configured passkeys newest-first for Diagnostics rendering."""

    return list(
        db.scalars(
            select(PasskeyCredential).order_by(
                PasskeyCredential.created_at.desc(),
                PasskeyCredential.id.desc(),
            )
        )
    )


def passkey_login_available(db: Session) -> bool:
    """Return whether at least one passkey exists for device sign-in."""

    return db.scalar(select(PasskeyCredential.id).limit(1)) is not None


def _valid_origin(value: str) -> str:
    """Normalize a browser origin string and reject paths or unsupported schemes."""

    origin = value.strip().rstrip("/")
    parsed = urlsplit(origin)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise PasskeyCeremonyError("Passkey origin is not a valid http or https origin.")
    if parsed.path or parsed.query or parsed.fragment:
        raise PasskeyCeremonyError("Passkey origin must not include a path, query, or fragment.")
    return origin


def passkey_origin_for_request(request: Request, settings: Settings) -> str:
    """Return the expected WebAuthn browser origin for this request."""

    if settings.passkey_origin.strip():
        return settings.passkey_origin.strip().rstrip("/")

    origin_header = request.headers.get("origin", "")
    if origin_header.strip():
        return _valid_origin(origin_header)

    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",", maxsplit=1)[
        0
    ].strip()
    forwarded_host = request.headers.get("x-forwarded-host", "").split(",", maxsplit=1)[
        0
    ].strip()
    host = forwarded_host or request.headers.get("host", "").strip()
    if forwarded_proto in {"http", "https"} and host:
        return _valid_origin(f"{forwarded_proto}://{host}")

    return _valid_origin(str(request.url.replace(path="", query="", fragment="")).rstrip("/"))


def passkey_rp_id_for_origin(origin: str, settings: Settings) -> str:
    """Return the WebAuthn relying-party ID for a browser origin."""

    if settings.passkey_rp_id.strip():
        return settings.passkey_rp_id.strip().lower()
    hostname = urlsplit(origin).hostname
    if not hostname:
        raise PasskeyCeremonyError("Passkey relying-party ID could not be determined.")
    return hostname.lower()


def _public_key_descriptor(passkey: PasskeyCredential) -> PublicKeyCredentialDescriptor:
    """Build a WebAuthn credential descriptor from a stored passkey."""

    transports = []
    for transport in passkey.transports or []:
        try:
            transports.append(AuthenticatorTransport(transport))
        except ValueError:
            continue
    return PublicKeyCredentialDescriptor(
        id=base64url_to_bytes(passkey.credential_id),
        transports=transports or None,
    )


def _store_context(
    request: Request,
    *,
    session_key: str,
    challenge: bytes,
    rp_id: str,
    origin: str,
    next_url: str = "",
) -> None:
    """Persist a one-ceremony WebAuthn challenge in the signed session cookie."""

    request.session[session_key] = {
        "challenge": bytes_to_base64url(challenge),
        "rp_id": rp_id,
        "origin": origin,
        "next_url": next_url,
    }


def _pop_context(request: Request, session_key: str) -> dict[str, str]:
    """Read and remove a WebAuthn ceremony context to prevent challenge replay."""

    context = request.session.pop(session_key, None)
    if not isinstance(context, dict):
        raise PasskeyCeremonyError("Passkey challenge was missing or expired.")
    required_keys = {"challenge", "rp_id", "origin"}
    if not required_keys.issubset(context):
        raise PasskeyCeremonyError("Passkey challenge context was incomplete.")
    return {key: str(value) for key, value in context.items()}


def begin_passkey_registration(
    db: Session,
    request: Request,
    settings: Settings,
) -> str:
    """Create WebAuthn registration options for the configured web-login user."""

    origin = passkey_origin_for_request(request, settings)
    rp_id = passkey_rp_id_for_origin(origin, settings)
    existing_passkeys = list_passkeys(db)
    options = generate_registration_options(
        rp_id=rp_id,
        rp_name=settings.passkey_rp_name.strip() or settings.app_name,
        user_id=passkey_user_handle(settings),
        user_name=settings.web_login_username.strip(),
        user_display_name=settings.web_login_username.strip(),
        exclude_credentials=[
            _public_key_descriptor(passkey) for passkey in existing_passkeys
        ],
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
    )
    _store_context(
        request,
        session_key=PASSKEY_REGISTRATION_SESSION_KEY,
        challenge=options.challenge,
        rp_id=rp_id,
        origin=origin,
    )
    return options_to_json(options)


def begin_passkey_authentication(
    db: Session,
    request: Request,
    settings: Settings,
    *,
    next_url: str,
) -> str:
    """Create WebAuthn authentication options for existing passkeys."""

    passkeys = list_passkeys(db)
    if not passkeys:
        raise PasskeyCeremonyError("No passkeys are configured.")

    origin = passkey_origin_for_request(request, settings)
    rp_id = passkey_rp_id_for_origin(origin, settings)
    options = generate_authentication_options(
        rp_id=rp_id,
        allow_credentials=[_public_key_descriptor(passkey) for passkey in passkeys],
        user_verification=UserVerificationRequirement.PREFERRED,
    )
    _store_context(
        request,
        session_key=PASSKEY_AUTHENTICATION_SESSION_KEY,
        challenge=options.challenge,
        rp_id=rp_id,
        origin=origin,
        next_url=next_url,
    )
    return options_to_json(options)


def _credential_id_from_response(credential: dict) -> str:
    """Return the normalized credential ID from a browser WebAuthn response."""

    raw_id = credential.get("rawId") or credential.get("id")
    if not isinstance(raw_id, str) or not raw_id:
        raise PasskeyCeremonyError("Passkey credential ID was missing.")
    return bytes_to_base64url(base64url_to_bytes(raw_id))


def _transports_from_registration_response(credential: dict) -> list[str]:
    """Extract optional authenticator transports from the browser registration response."""

    response = credential.get("response")
    if not isinstance(response, dict):
        return []
    transports = response.get("transports")
    if not isinstance(transports, list):
        return []
    cleaned: list[str] = []
    for transport in transports:
        if isinstance(transport, str) and transport and transport not in cleaned:
            cleaned.append(transport[:40])
    return cleaned


def finish_passkey_registration(
    db: Session,
    request: Request,
    credential: dict,
    settings: Settings,
) -> PasskeyCredential:
    """Verify and persist a WebAuthn registration response."""

    context = _pop_context(request, PASSKEY_REGISTRATION_SESSION_KEY)
    try:
        verification = verify_registration_response(
            credential=credential,
            expected_challenge=base64url_to_bytes(context["challenge"]),
            expected_rp_id=context["rp_id"],
            expected_origin=context["origin"],
            require_user_verification=False,
        )
    except WebAuthnException as exc:
        raise PasskeyCeremonyError("Passkey registration response could not be verified.") from exc

    credential_id = bytes_to_base64url(verification.credential_id)
    existing = db.scalar(
        select(PasskeyCredential).where(PasskeyCredential.credential_id == credential_id).limit(1)
    )
    if existing is not None:
        raise PasskeyCeremonyError("That passkey is already configured.")

    passkey = PasskeyCredential(
        credential_id=credential_id,
        user_handle=passkey_user_handle_text(settings),
        username=settings.web_login_username.strip(),
        public_key=bytes_to_base64url(verification.credential_public_key),
        sign_count=verification.sign_count,
        transports=_transports_from_registration_response(credential),
        aaguid=verification.aaguid,
        credential_type=verification.credential_type.value,
        device_type=verification.credential_device_type.value,
        backed_up=verification.credential_backed_up,
    )
    db.add(passkey)
    db.flush()
    return passkey


def finish_passkey_authentication(
    db: Session,
    request: Request,
    credential: dict,
) -> PasskeyCredential:
    """Verify a WebAuthn assertion and update the stored passkey sign count."""

    context = _pop_context(request, PASSKEY_AUTHENTICATION_SESSION_KEY)
    credential_id = _credential_id_from_response(credential)
    passkey = db.scalar(
        select(PasskeyCredential).where(PasskeyCredential.credential_id == credential_id).limit(1)
    )
    if passkey is None:
        raise PasskeyCeremonyError("Passkey credential is not configured.")

    response = credential.get("response")
    if isinstance(response, dict):
        user_handle = response.get("userHandle")
        if user_handle and user_handle != passkey.user_handle:
            raise PasskeyCeremonyError("Passkey user handle did not match.")

    try:
        verification = verify_authentication_response(
            credential=credential,
            expected_challenge=base64url_to_bytes(context["challenge"]),
            expected_rp_id=context["rp_id"],
            expected_origin=context["origin"],
            credential_public_key=base64url_to_bytes(passkey.public_key),
            credential_current_sign_count=passkey.sign_count,
            require_user_verification=False,
        )
    except WebAuthnException as exc:
        raise PasskeyCeremonyError(
            "Passkey authentication response could not be verified."
        ) from exc

    passkey.sign_count = verification.new_sign_count
    passkey.device_type = verification.credential_device_type.value
    passkey.backed_up = verification.credential_backed_up
    passkey.last_used_at = utc_now()
    return passkey
