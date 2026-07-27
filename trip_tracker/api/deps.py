import base64
import secrets

from fastapi import HTTPException, Request, status

from trip_tracker.config import get_settings

OWNTRACKS_API_PATHS = {
    "/api/owntracks",
    "/api/owntracks/",
    "/api/pub",
    "/api/pub/",
}
WEB_API_AUTH_EXEMPT_PATHS = {"/api/health", *OWNTRACKS_API_PATHS}
OWNTRACKS_RETRY_HEADERS = {"Cache-Control": "no-store", "Retry-After": "30"}


def _verify_owntracks_basic_auth(request: Request) -> bool:
    settings = get_settings()
    basic_configured = bool(settings.owntracks_username and settings.owntracks_password)
    if not basic_configured:
        return False

    authorization = request.headers.get("authorization", "")
    if not authorization.startswith("Basic "):
        return False

    encoded = authorization.removeprefix("Basic ").strip()
    try:
        username, password = base64.b64decode(encoded).decode("utf-8").split(":", 1)
    except (ValueError, UnicodeDecodeError):
        username, password = "", ""
    username_matches = secrets.compare_digest(username, settings.owntracks_username)
    password_matches = secrets.compare_digest(
        password,
        settings.owntracks_password,
    )
    return username_matches and password_matches


def verify_owntracks_auth(request: Request) -> None:
    settings = get_settings()
    if not settings.owntracks_encryption_key.strip():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="OWNTRACKS_ENCRYPTION_KEY is not configured",
            headers=OWNTRACKS_RETRY_HEADERS,
        )
    if not (settings.owntracks_username.strip() and settings.owntracks_password.strip()):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="OwnTracks Basic Auth is not configured",
            headers=OWNTRACKS_RETRY_HEADERS,
        )
    if _verify_owntracks_basic_auth(request):
        return

    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")


def verify_web_api_auth(request: Request) -> None:
    settings = get_settings()
    expected_token = settings.web_api_key.strip()
    if not expected_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="WEB_API_KEY is not configured",
        )

    authorization = request.headers.get("authorization", "")
    scheme, _, supplied_token = authorization.partition(" ")
    if scheme.casefold() != "bearer" or not supplied_token.strip():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
    if not secrets.compare_digest(supplied_token.strip(), expected_token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
