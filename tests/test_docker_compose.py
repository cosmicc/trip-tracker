import re
from pathlib import Path

COMPOSE_FILE = Path("docker-compose.yml")
STACK_FILE = Path("docker-stack.yml")
STACK_LOCAL_POSTGRES_FILE = Path("docker-stack.local-postgres.yml")
DOCKER_ENV_FILE = Path(".env.docker.example")
SWARM_IMAGE_WORKFLOW_FILE = Path(".github/workflows/publish-swarm-images.yml")


def _service_block(compose_text: str, service_name: str) -> str:
    match = re.search(
        rf"^  {re.escape(service_name)}:\n(?P<body>.*?)"
        r"(?=^  [A-Za-z0-9_-]+:|^volumes:|^networks:|\Z)",
        compose_text,
        flags=re.DOTALL | re.MULTILINE,
    )
    assert match is not None, f"missing compose service {service_name}"
    return match.group("body")


def test_nginx_host_port_is_loopback_only_for_cloudflared_host_network() -> None:
    compose_text = COMPOSE_FILE.read_text(encoding="utf-8")
    nginx_block = _service_block(compose_text, "ttnginx")
    cloudflared_block = _service_block(compose_text, "cloudflared")

    assert '"127.0.0.1:${HTTP_PORT:-80}:80"' in nginx_block
    assert "BIND_ADDRESS" not in compose_text
    assert "network_mode: host" in cloudflared_block


def test_gas_snapshot_runs_inside_app_container_without_sidecar() -> None:
    compose_text = COMPOSE_FILE.read_text(encoding="utf-8")
    app_block = _service_block(compose_text, "ttapp")

    assert "\n  gas-snapshot:" not in compose_text
    assert 'GAS_SNAPSHOT_ENABLED: "${GAS_SNAPSHOT_ENABLED:-true}"' in app_block
    assert (
        'GAS_SNAPSHOT_INTERVAL_SECONDS: "${GAS_SNAPSHOT_INTERVAL_SECONDS:-86400}"'
        in app_block
    )
    assert 'GAS_SNAPSHOT_RUN_ON_STARTUP: "${GAS_SNAPSHOT_RUN_ON_STARTUP:-true}"' in app_block


def test_app_container_requires_production_web_login_secrets() -> None:
    compose_text = COMPOSE_FILE.read_text(encoding="utf-8")
    app_block = _service_block(compose_text, "ttapp")

    assert "SECRET_KEY: \"${SECRET_KEY:?" in app_block
    assert "WEB_LOGIN_USERNAME: \"${WEB_LOGIN_USERNAME:?" in app_block
    assert "WEB_LOGIN_PASSWORD: \"${WEB_LOGIN_PASSWORD:?" in app_block
    assert "WEB_API_KEY: \"${WEB_API_KEY:?" in app_block
    assert "OWNTRACKS_USERNAME: \"${OWNTRACKS_USERNAME:?" in app_block
    assert "OWNTRACKS_PASSWORD: \"${OWNTRACKS_PASSWORD:?" in app_block
    assert "OWNTRACKS_ENCRYPTION_KEY: \"${OWNTRACKS_ENCRYPTION_KEY:?" in app_block
    assert 'PASSKEY_RP_NAME: "${PASSKEY_RP_NAME:-Trip Tracker}"' in app_block
    assert 'PASSKEY_RP_ID: "${PASSKEY_RP_ID:-}"' in app_block
    assert 'PASSKEY_ORIGIN: "${PASSKEY_ORIGIN:-}"' in app_block


def test_app_container_exposes_pushover_health_settings() -> None:
    compose_text = COMPOSE_FILE.read_text(encoding="utf-8")
    stack_text = STACK_FILE.read_text(encoding="utf-8")
    env_text = DOCKER_ENV_FILE.read_text(encoding="utf-8")
    compose_app_block = _service_block(compose_text, "ttapp")
    stack_app_block = _service_block(stack_text, "ttapp")

    for app_block in (compose_app_block, stack_app_block):
        assert 'PUSHOVER_ENABLED: "${PUSHOVER_ENABLED:-false}"' in app_block
        assert 'PUSHOVER_TOKEN: "${PUSHOVER_TOKEN:-}"' in app_block
        assert 'PUSHOVER_USER: "${PUSHOVER_USER:-}"' in app_block
        assert 'PUSHOVER_APP_KEY: "${PUSHOVER_APP_KEY:-}"' in app_block
        assert 'PUSHOVER_USER_KEY: "${PUSHOVER_USER_KEY:-}"' in app_block
        assert (
            'APP_HEALTH_MONITOR_INTERVAL_SECONDS: '
            '"${APP_HEALTH_MONITOR_INTERVAL_SECONDS:-60}"'
            in app_block
        )
        assert (
            'APP_HEALTH_REMINDER_INTERVAL_SECONDS: '
            '"${APP_HEALTH_REMINDER_INTERVAL_SECONDS:-3600}"'
            in app_block
        )
        assert (
            'APP_HEALTH_DB_LATENCY_SUSTAINED_SECONDS: '
            '"${APP_HEALTH_DB_LATENCY_SUSTAINED_SECONDS:-15}"'
            in app_block
        )
        assert (
            'APP_HEALTH_DISK_WARNING_FREE_MB: '
            '"${APP_HEALTH_DISK_WARNING_FREE_MB:-1000}"'
            in app_block
        )
        assert (
            'APP_HEALTH_DISK_CRITICAL_FREE_MB: '
            '"${APP_HEALTH_DISK_CRITICAL_FREE_MB:-250}"'
            in app_block
        )
        assert (
            'APP_HEALTH_STATE_PATH: "${APP_HEALTH_STATE_PATH:-'
            '/data/app-health-state.json}"'
            in app_block
        )

    assert "PUSHOVER_ENABLED=false" in env_text
    assert "PUSHOVER_TOKEN=" in env_text
    assert "PUSHOVER_USER=" in env_text
    assert "APP_HEALTH_REMINDER_INTERVAL_SECONDS=3600" in env_text
    assert "APP_HEALTH_DB_LATENCY_WARNING_MS=500" in env_text
    assert "APP_HEALTH_DB_LATENCY_SUSTAINED_SECONDS=15" in env_text
    assert "APP_HEALTH_DISK_WARNING_FREE_MB=1000" in env_text
    assert "APP_HEALTH_DISK_CRITICAL_FREE_MB=250" in env_text
    assert "APP_HEALTH_DISK_WARNING_PERCENT" not in env_text
    assert "APP_HEALTH_DISK_CRITICAL_PERCENT" not in env_text


def test_app_container_uses_persistent_data_mount_without_file_log_settings() -> None:
    compose_text = COMPOSE_FILE.read_text(encoding="utf-8")
    stack_text = STACK_FILE.read_text(encoding="utf-8")
    env_text = DOCKER_ENV_FILE.read_text(encoding="utf-8")

    for deployment_text in (compose_text, stack_text):
        app_block = _service_block(deployment_text, "ttapp")
        assert "APP_DATA_DIR: /data" in app_block
        assert 'AUTOMATIC_BACKUP_DIR: "${AUTOMATIC_BACKUP_DIR:-/data/backups}"' in app_block
        assert (
            'AUTOMATIC_BACKUP_RETRY_SECONDS: '
            '"${AUTOMATIC_BACKUP_RETRY_SECONDS:-60}"'
            in app_block
        )
        assert "${HOST_DATA_DIR:-/var/lib/trip-tracker}:/data" in app_block
        assert (
            "${HOST_BACKUP_DIR:-/var/lib/trip-tracker/backups}:/data/backups"
            in app_block
        )
        assert "LOG_DIR" not in app_block
        assert "LOGIN_FAILURE_LOG_PATH" not in app_block

    assert "APP_DATA_DIR=/data" in env_text
    assert "HOST_DATA_DIR=/var/lib/trip-tracker" in env_text
    assert "HOST_BACKUP_DIR=/var/lib/trip-tracker/backups" in env_text
    assert "AUTOMATIC_BACKUP_RETRY_SECONDS=60" in env_text
    assert "LOG_DIR=" not in env_text
    assert "LOGIN_FAILURE_LOG_PATH=" not in env_text


def test_bundled_postgres_is_default_optional_compose_profile() -> None:
    compose_text = COMPOSE_FILE.read_text(encoding="utf-8")
    env_text = DOCKER_ENV_FILE.read_text(encoding="utf-8")
    postgres_block = _service_block(compose_text, "postgres")
    app_block = _service_block(compose_text, "ttapp")

    assert 'profiles: ["local-postgres"]' in postgres_block
    assert "COMPOSE_PROFILES=local-postgres" in env_text
    assert (
        'DATABASE_URL: "${DATABASE_URL:-postgresql+psycopg://'
        'triptracker:triptracker@postgres:5432/trip_tracker}"'
        in app_block
    )
    assert "depends_on" not in app_block


def test_owntracks_ingestion_has_no_server_buffer_or_mqtt_configuration() -> None:
    compose_text = COMPOSE_FILE.read_text(encoding="utf-8")
    stack_text = STACK_FILE.read_text(encoding="utf-8")
    env_text = DOCKER_ENV_FILE.read_text(encoding="utf-8")

    for deployment_text in (compose_text, stack_text, env_text):
        assert "OWNTRACKS_BUFFER" not in deployment_text
        assert "HOST_OWNTRACKS_BUFFER_DIR" not in deployment_text
        assert "MQTT_" not in deployment_text

    assert 'start_period: "${APP_HEALTHCHECK_START_PERIOD:-90s}"' in compose_text
    assert 'start_period: "${APP_HEALTHCHECK_START_PERIOD:-90s}"' in stack_text


def test_swarm_stack_avoids_compose_only_features() -> None:
    stack_text = STACK_FILE.read_text(encoding="utf-8")
    app_block = _service_block(stack_text, "ttapp")
    nginx_block = _service_block(stack_text, "ttnginx")
    cloudflared_block = _service_block(stack_text, "cloudflared")

    assert "\n    build:" not in stack_text
    assert "\n    profiles:" not in stack_text
    assert "\n    depends_on:" not in stack_text
    assert "\n    network_mode:" not in stack_text
    assert "\n    restart:" not in stack_text
    assert "\n  app:" not in stack_text
    assert "\n  nginx:" not in stack_text
    assert "\n  ttapp:" in stack_text
    assert "\n  ttnginx:" in stack_text
    assert 'image: "${APP_IMAGE:-trip-tracker-app:latest}"' in app_block
    assert 'user: "${APP_UID:-1000}:${APP_GID:-100}"' in app_block
    assert 'image: "${NGINX_IMAGE:-trip-tracker-nginx:latest}"' in nginx_block
    assert "ports:" not in nginx_block
    assert "TUNNEL_TOKEN:" in cloudflared_block
    assert "replicas: 2" in cloudflared_block
    assert "max_replicas_per_node: 1" in cloudflared_block
    assert "delay: 5s" in cloudflared_block
    assert "delay: 10s" in cloudflared_block
    assert "order: start-first" in cloudflared_block
    assert 'start_period: "${APP_HEALTHCHECK_START_PERIOD:-90s}"' in app_block
    assert "restart_policy:" in stack_text
    assert "trip-tracker-internal" in app_block
    assert "trip-tracker-internal" in nginx_block
    assert "trip-tracker-internal" in cloudflared_block


def test_swarm_stack_has_optional_local_postgres_overlay() -> None:
    stack_text = STACK_FILE.read_text(encoding="utf-8")
    local_postgres_text = STACK_LOCAL_POSTGRES_FILE.read_text(encoding="utf-8")
    env_text = DOCKER_ENV_FILE.read_text(encoding="utf-8")

    assert "\n  postgres:" not in stack_text
    assert "\n  postgres:" in local_postgres_text
    assert "postgres_data:/var/lib/postgresql/data" in local_postgres_text
    assert 'name: "${POSTGRES_DATA_VOLUME:-trip-tracker-postgres-data}"' in (
        local_postgres_text
    )
    assert "POSTGRES_DATA_VOLUME=trip-tracker-postgres-data" in env_text
    assert "trip-tracker-internal" in local_postgres_text
    assert "APP_IMAGE=ghcr.io/cosmicc/trip-tracker-app:1.5.0" in env_text
    assert "NGINX_IMAGE=ghcr.io/cosmicc/trip-tracker-nginx:1.5.0" in env_text
    assert "APP_UID=1000" in env_text
    assert "APP_GID=100" in env_text


def test_swarm_image_workflow_publishes_versioned_and_immutable_tags() -> None:
    workflow_text = SWARM_IMAGE_WORKFLOW_FILE.read_text(encoding="utf-8")

    assert "packages: write" in workflow_text
    assert "component: ttapp" in workflow_text
    assert "component: ttnginx" in workflow_text
    assert "component: app" not in workflow_text
    assert "component: nginx" not in workflow_text
    assert "ghcr.io/cosmicc/trip-tracker-app" in workflow_text
    assert "ghcr.io/cosmicc/trip-tracker-nginx" in workflow_text
    assert 'open("pyproject.toml", "rb")' in workflow_text
    assert "type=raw,value=latest" in workflow_text
    assert "type=raw,value=${{ steps.version.outputs.version }}" in workflow_text
    assert "type=sha,prefix=sha-,format=long" in workflow_text
