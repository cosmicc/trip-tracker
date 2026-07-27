import ipaddress
import secrets
import time
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

from fastapi import Request
from fastapi.responses import RedirectResponse

from trip_tracker.config import Settings, get_settings

WEB_AUTH_SESSION_KEY = "web_authenticated"
WEB_AUTH_PUBLIC_DEVICE_KEY = "web_public_device"
WEB_AUTH_LAST_ACTIVITY_KEY = "web_last_activity"
PUBLIC_DEVICE_IDLE_TIMEOUT_SECONDS = 15 * 60
PUBLIC_DEVICE_CLEAR_SITE_DATA = '"cache", "cookies", "storage"'
WEB_AUTH_OPEN_PATHS = {
    "/apple-touch-icon.png",
    "/login",
    "/logout",
    "/favicon.ico",
    "/manifest.webmanifest",
    "/passkeys/login/options",
    "/passkeys/login/verify",
    "/service-worker.js",
    "/site.webmanifest",
}


@dataclass
class LoginAttemptState:
    """Failed login state for one client address."""

    failed_count: int = 0
    locked_until: float = 0.0


FAILED_LOGIN_ATTEMPTS: dict[str, LoginAttemptState] = {}


def web_login_enabled(settings: Settings | None = None) -> bool:
    """Return whether web UI login is enabled by configured username and password."""

    active_settings = settings or get_settings()
    return bool(
        active_settings.web_login_username.strip()
        and active_settings.web_login_password.strip()
    )


def _client_host(request: Request) -> str:
    """Return the direct ASGI client host for audit and throttling decisions."""

    if request.client is None:
        return ""
    return request.client.host.strip()


def _ip_from_text(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Parse an IP address string, returning None for malformed or host-name values."""

    try:
        return ipaddress.ip_address(value.strip())
    except ValueError:
        return None


def _header_ip(value: str) -> str:
    """Return a normalized header IP address or an empty string for invalid values."""

    parsed = _ip_from_text(value)
    if parsed is None:
        return ""
    return str(parsed)


def login_client_key_from_values(
    *,
    direct_client_ip: str,
    cf_connecting_ip: str = "",
    x_real_ip: str = "",
    x_forwarded_for: str = "",
    settings: Settings | None = None,
) -> str:
    """Return the login client key from Cloudflare's client IP or the direct client."""

    cloudflare_ip = _header_ip(cf_connecting_ip)
    if cloudflare_ip:
        return cloudflare_ip

    direct_client = direct_client_ip.strip()
    if direct_client:
        return direct_client
    return "unknown"


def login_client_key(request: Request, settings: Settings | None = None) -> str:
    """Return the best available client key for throttling web login attempts."""

    active_settings = settings or get_settings()
    return login_client_key_from_values(
        direct_client_ip=_client_host(request),
        cf_connecting_ip=request.headers.get("cf-connecting-ip", ""),
        x_real_ip=request.headers.get("x-real-ip", ""),
        x_forwarded_for=request.headers.get("x-forwarded-for", ""),
        settings=active_settings,
    )


def login_is_locked(request: Request, settings: Settings | None = None) -> bool:
    """Return whether the current client is temporarily locked out after failed logins."""

    attempt_state = login_failure_state(request, settings)
    return bool(attempt_state and attempt_state.locked_until > time.monotonic())


def login_failure_state(
    request: Request,
    settings: Settings | None = None,
) -> LoginAttemptState | None:
    """Return the tracked failed-login state for the current client, if present."""

    return FAILED_LOGIN_ATTEMPTS.get(login_client_key(request, settings))


def login_lockout_remaining_seconds(attempt_state: LoginAttemptState | None) -> int:
    """Return whole seconds remaining in the current lockout window."""

    if attempt_state is None:
        return 0
    return max(0, int(attempt_state.locked_until - time.monotonic()))


def record_login_failure(
    request: Request,
    settings: Settings | None = None,
) -> LoginAttemptState:
    """Record a failed login and lock the client after the configured number of failures."""

    active_settings = settings or get_settings()
    client_key = login_client_key(request, active_settings)
    attempt_state = FAILED_LOGIN_ATTEMPTS.setdefault(client_key, LoginAttemptState())
    if attempt_state.locked_until <= time.monotonic():
        attempt_state.failed_count += 1
    if attempt_state.failed_count >= active_settings.web_login_max_attempts:
        attempt_state.locked_until = time.monotonic() + active_settings.web_login_lockout_seconds
    return attempt_state


def clear_login_failures(request: Request, settings: Settings | None = None) -> None:
    """Clear failed login state after a successful login."""

    FAILED_LOGIN_ATTEMPTS.pop(login_client_key(request, settings), None)


def valid_next_path(value: str | None) -> str:
    """Return a safe local redirect path after login or logout."""

    if not value:
        return "/"
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or not parsed.path.startswith("/"):
        return "/"
    if parsed.path.startswith("/api") or parsed.path in WEB_AUTH_OPEN_PATHS:
        return "/"
    query = f"?{parsed.query}" if parsed.query else ""
    return f"{parsed.path}{query}"


def login_redirect_for_request(request: Request) -> RedirectResponse:
    """Build the login redirect for an unauthenticated web UI request."""

    next_path = request.url.path
    if request.url.query:
        next_path = f"{next_path}?{request.url.query}"
    return RedirectResponse(
        url=f"/login?next={quote(valid_next_path(next_path), safe='')}",
        status_code=303,
    )


def request_is_authenticated(request: Request) -> bool:
    """Check whether the current signed session is logged into the web UI."""

    return request.session.get(WEB_AUTH_SESSION_KEY) is True


def request_is_public_device(request: Request) -> bool:
    """Return whether the authenticated session requested public-device protections."""

    return bool(
        request_is_authenticated(request)
        and request.session.get(WEB_AUTH_PUBLIC_DEVICE_KEY) is True
    )


def authenticate_web_credentials(
    username: str,
    password: str,
    settings: Settings | None = None,
) -> bool:
    """Validate submitted web UI credentials with constant-time comparisons."""

    active_settings = settings or get_settings()
    username_matches = secrets.compare_digest(username, active_settings.web_login_username)
    password_matches = secrets.compare_digest(password, active_settings.web_login_password)
    return username_matches and password_matches


def mark_request_authenticated(request: Request, *, public_device: bool = False) -> None:
    """Mark the signed session as authenticated and apply its device privacy mode."""

    request.session[WEB_AUTH_SESSION_KEY] = True
    request.session[WEB_AUTH_PUBLIC_DEVICE_KEY] = public_device
    if public_device:
        request.session[WEB_AUTH_LAST_ACTIVITY_KEY] = time.time()
    else:
        request.session.pop(WEB_AUTH_LAST_ACTIVITY_KEY, None)


def record_public_device_activity(request: Request) -> None:
    """Refresh the public-device idle timer after verified browser activity."""

    if request_is_public_device(request):
        request.session[WEB_AUTH_LAST_ACTIVITY_KEY] = time.time()


def public_device_session_expired(request: Request) -> bool:
    """Return whether a public-device session exceeded its inactivity limit."""

    if not request_is_public_device(request):
        return False
    try:
        last_activity = float(request.session.get(WEB_AUTH_LAST_ACTIVITY_KEY, 0))
    except (TypeError, ValueError):
        return True
    return time.time() - last_activity >= PUBLIC_DEVICE_IDLE_TIMEOUT_SECONDS


def clear_request_authentication(request: Request) -> None:
    """Clear all web UI session state so the signed session cookie is deleted."""

    request.session.clear()


def add_public_device_clear_site_data(response: RedirectResponse) -> RedirectResponse:
    """Ask supported browsers to remove cached and stored site data."""

    response.headers["Clear-Site-Data"] = PUBLIC_DEVICE_CLEAR_SITE_DATA
    response.headers["Cache-Control"] = "no-store"
    return response


async def enforce_web_login(request: Request, call_next):
    """Require login for rendered web pages while leaving API and static paths open."""

    settings = get_settings()
    if not web_login_enabled(settings):
        return await call_next(request)

    path = request.url.path
    if path == "/api" or path.startswith("/api/"):
        return await call_next(request)
    if path.startswith("/static/") or path in WEB_AUTH_OPEN_PATHS:
        return await call_next(request)
    if request_is_authenticated(request):
        if public_device_session_expired(request):
            clear_request_authentication(request)
            return add_public_device_clear_site_data(
                RedirectResponse(url="/login?public_timeout=1", status_code=303)
            )
        return await call_next(request)
    return login_redirect_for_request(request)
