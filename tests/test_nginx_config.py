import re
from pathlib import Path

ERROR_PAGE_STATUSES = (400, 401, 403, 404, 405, 408, 413, 429, 500, 502, 503, 504)
NGINX_CONF = Path("deploy/nginx/default.conf")
NGINX_DOCKERFILE = Path("deploy/nginx/Dockerfile")
NGINX_ERROR_PAGES = Path("deploy/nginx/error-pages")


def _location_block(config: str, location: str) -> str:
    match = re.search(
        rf"location\s+{re.escape(location)}\s+\{{(?P<body>.*?)\n    \}}",
        config,
        flags=re.DOTALL,
    )
    assert match is not None, f"missing nginx location {location}"
    return match.group("body")


def test_public_nginx_only_proxies_owntracks_api_endpoints() -> None:
    config = NGINX_CONF.read_text(encoding="utf-8")

    for location in (
        "= /api/owntracks",
        "= /api/owntracks/",
        "= /api/pub",
        "= /api/pub/",
    ):
        block = _location_block(config, location)
        assert "proxy_pass http://trip_tracker_ttapp;" in block
        assert "limit_except POST" in block

    assert "location /api/ {\n        return 404;\n    }" in config
    assert "location = /api {\n        return 404;\n    }" in config
    assert "location = /openapi.json {\n        return 404;\n    }" in config
    assert "location ^~ /docs {\n        return 404;\n    }" in config
    assert "location ^~ /redoc {\n        return 404;\n    }" in config
    assert "upstream trip_tracker_ttapp" in config
    assert "server ttapp:8000;" in config
    assert "server app:8000;" not in config


def test_nginx_serves_custom_error_pages() -> None:
    config = NGINX_CONF.read_text(encoding="utf-8")
    dockerfile = NGINX_DOCKERFILE.read_text(encoding="utf-8")

    assert "COPY deploy/nginx/error-pages/ /usr/share/nginx/html/errors/" in dockerfile
    assert 'add_header X-Robots-Tag "noindex, nofollow" always;' in config

    for status in ERROR_PAGE_STATUSES:
        page = NGINX_ERROR_PAGES / f"{status}.html"
        html = page.read_text(encoding="utf-8")

        assert f"error_page {status} /errors/{status}.html;" in config
        assert 'href="/login"' in html
        assert "Trip Tracker" not in html
        assert ">ML<" not in html
        assert "Nginx" not in html
        assert "nginx" not in html
        assert 'id="primary-action"' in html
        assert "Back to login" in html
        assert "Back to home" in html
        assert 'document.cookie.includes("trip_tracker_session=")' in html
        assert "font-size:clamp(30px, 5.6vw, 42px)" in html
        assert "font-size:clamp(24px, 5vw, 36px)" in html
        assert "background:linear-gradient" in html
        assert "This " in html or "The " in html


def test_nginx_intercepts_proxied_browser_errors_only() -> None:
    config = NGINX_CONF.read_text(encoding="utf-8")

    static_block = _location_block(config, "/static/")
    web_block = _location_block(config, "/")
    assert "proxy_intercept_errors on;" in static_block
    assert "proxy_intercept_errors on;" in web_block

    for location in (
        "= /api/owntracks",
        "= /api/owntracks/",
        "= /api/pub",
        "= /api/pub/",
    ):
        assert "proxy_intercept_errors on;" not in _location_block(config, location)


def test_nginx_keeps_web_routes_available_behind_web_access_rules() -> None:
    config = NGINX_CONF.read_text(encoding="utf-8")

    for location in ("= /api/health", "= /api/locations"):
        assert location not in config

    static_block = _location_block(config, "/static/")
    web_block = _location_block(config, "/")
    assert "include /etc/nginx/includes/web-access.conf;" in static_block
    assert "include /etc/nginx/includes/web-access.conf;" in web_block
    assert "proxy_pass http://trip_tracker_ttapp;" in static_block
    assert "proxy_pass http://trip_tracker_ttapp;" in web_block


def test_nginx_passes_loopback_tunnel_headers_without_trusted_proxy_maps() -> None:
    """Nginx should bind to a loopback tunnel origin without trusted-proxy CIDR maps."""

    config = NGINX_CONF.read_text(encoding="utf-8")

    assert "trip_tracker_client_ip" not in config
    assert "trusted_forwarded_proto" not in config
    assert "map $remote_addr" not in config
    assert "map $http_x_forwarded_proto $trip_tracker_forwarded_proto" in config

    for location in (
        "= /api/owntracks",
        "= /api/owntracks/",
        "= /api/pub",
        "= /api/pub/",
        "/static/",
        "/",
    ):
        block = _location_block(config, location)
        assert "proxy_set_header X-Real-IP $remote_addr;" in block
        assert "proxy_set_header X-Forwarded-For $remote_addr;" in block
        assert "proxy_set_header X-Forwarded-Proto $trip_tracker_forwarded_proto;" in block
        assert "proxy_set_header CF-Connecting-IP $http_cf_connecting_ip;" in block

    assert "$proxy_add_x_forwarded_for" not in config
