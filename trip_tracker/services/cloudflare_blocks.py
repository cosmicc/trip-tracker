"""Cloudflare zone IP Access Rule helpers for app-managed login blocks."""

import ipaddress
import logging
import re
from dataclasses import dataclass

import httpx

from trip_tracker.config import Settings, get_settings

logger = logging.getLogger(__name__)

CLOUDFLARE_API_BASE_URL = "https://api.cloudflare.com/client/v4"
CLOUDFLARE_TIMEOUT_SECONDS = 10.0


class CloudflareBlockError(RuntimeError):
    """Raised when a Cloudflare block or unblock operation cannot be completed."""


@dataclass(frozen=True)
class CloudflareAccessRule:
    """Small app-owned representation of a Cloudflare IP Access Rule."""

    rule_id: str
    ip_address: str


def cloudflare_ip_blocking_configured(settings: Settings | None = None) -> bool:
    """Return whether Cloudflare blocking has all required runtime configuration."""

    active_settings = settings or get_settings()
    return bool(
        active_settings.cloudflare_ip_blocking_enabled
        and active_settings.cloudflare_api_token.strip()
        and active_settings.cloudflare_zone_id.strip()
    )


def normalize_ip_address(value: str) -> str | None:
    """Return a canonical IPv4/IPv6 address string, or None for invalid input."""

    try:
        return str(ipaddress.ip_address(str(value).strip()))
    except ValueError:
        return None


def _allowlist_entries(settings: Settings) -> tuple[str, ...]:
    """Split the configured allowlist on common env-friendly separators."""

    return tuple(
        item.strip()
        for item in re.split(r"[\s,]+", settings.cloudflare_ip_block_allowlist)
        if item.strip()
    )


def ip_is_allowlisted(ip_address: str, settings: Settings | None = None) -> bool:
    """Return whether an IP is protected from app-managed Cloudflare blocking."""

    active_settings = settings or get_settings()
    normalized_ip = normalize_ip_address(ip_address)
    if normalized_ip is None:
        return False

    ip_value = ipaddress.ip_address(normalized_ip)
    for entry in _allowlist_entries(active_settings):
        try:
            if "/" in entry:
                if ip_value in ipaddress.ip_network(entry, strict=False):
                    return True
            elif ip_value == ipaddress.ip_address(entry):
                return True
        except ValueError:
            logger.warning("Ignoring invalid Cloudflare IP block allowlist entry=%s", entry)
    return False


def _cloudflare_headers(settings: Settings) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.cloudflare_api_token.strip()}",
        "Content-Type": "application/json",
    }


def _cloudflare_target_for_ip(ip_address: str) -> str:
    ip_value = ipaddress.ip_address(ip_address)
    return "ip6" if ip_value.version == 6 else "ip"


def _api_error_message(payload: object) -> str:
    if not isinstance(payload, dict):
        return "Cloudflare API returned an unexpected response."
    errors = payload.get("errors")
    if not isinstance(errors, list) or not errors:
        return "Cloudflare API request was not successful."
    messages: list[str] = []
    for error in errors:
        if isinstance(error, dict):
            message = str(error.get("message") or "").strip()
            code = str(error.get("code") or "").strip()
            if code == "10000":
                messages.append(
                    "Cloudflare rejected the API credentials (10000: Authentication error). "
                    "Set CLOUDFLARE_API_TOKEN to a Cloudflare API token with Account Firewall "
                    "Access Rules Write access for CLOUDFLARE_ZONE_ID; do not use "
                    "CLOUDFLARED_TUNNEL_TOKEN or a Global API Key in this field."
                )
            elif code and message:
                messages.append(f"{code}: {message}")
            elif message:
                messages.append(message)
    return "; ".join(messages) or "Cloudflare API request was not successful."


def create_cloudflare_ip_block(
    ip_address: str,
    *,
    note: str,
    settings: Settings | None = None,
) -> CloudflareAccessRule:
    """Create a zone-scoped Cloudflare IP Access Rule with block mode."""

    active_settings = settings or get_settings()
    if not cloudflare_ip_blocking_configured(active_settings):
        raise CloudflareBlockError("Cloudflare IP blocking is not fully configured.")

    normalized_ip = normalize_ip_address(ip_address)
    if normalized_ip is None:
        raise CloudflareBlockError("Cannot block an invalid IP address.")
    if ip_is_allowlisted(normalized_ip, active_settings):
        raise CloudflareBlockError("IP address is on the Cloudflare block allowlist.")

    url = (
        f"{CLOUDFLARE_API_BASE_URL}/zones/"
        f"{active_settings.cloudflare_zone_id.strip()}/firewall/access_rules/rules"
    )
    body = {
        "mode": "block",
        "configuration": {
            "target": _cloudflare_target_for_ip(normalized_ip),
            "value": normalized_ip,
        },
        "notes": note[:255],
    }
    try:
        response = httpx.post(
            url,
            headers=_cloudflare_headers(active_settings),
            json=body,
            timeout=CLOUDFLARE_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        raise CloudflareBlockError("Could not contact Cloudflare to create IP block.") from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise CloudflareBlockError("Cloudflare API returned a non-JSON response.") from exc

    if response.status_code >= 400 or not payload.get("success"):
        raise CloudflareBlockError(_api_error_message(payload))

    result = payload.get("result")
    if not isinstance(result, dict) or not result.get("id"):
        raise CloudflareBlockError("Cloudflare API did not return a rule ID.")
    return CloudflareAccessRule(rule_id=str(result["id"]), ip_address=normalized_ip)


def delete_cloudflare_ip_block(
    rule_id: str,
    *,
    settings: Settings | None = None,
) -> None:
    """Delete one app-managed Cloudflare IP Access Rule by Cloudflare rule ID."""

    active_settings = settings or get_settings()
    if not cloudflare_ip_blocking_configured(active_settings):
        raise CloudflareBlockError("Cloudflare IP blocking is not fully configured.")
    cleaned_rule_id = str(rule_id).strip()
    if not cleaned_rule_id:
        raise CloudflareBlockError("Cloudflare rule ID is missing.")

    url = (
        f"{CLOUDFLARE_API_BASE_URL}/zones/"
        f"{active_settings.cloudflare_zone_id.strip()}/firewall/access_rules/rules/"
        f"{cleaned_rule_id}"
    )
    try:
        response = httpx.delete(
            url,
            headers=_cloudflare_headers(active_settings),
            timeout=CLOUDFLARE_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        raise CloudflareBlockError("Could not contact Cloudflare to delete IP block.") from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise CloudflareBlockError("Cloudflare API returned a non-JSON response.") from exc

    if response.status_code >= 400 or not payload.get("success"):
        raise CloudflareBlockError(_api_error_message(payload))
