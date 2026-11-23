"""Runtime settings, read from environment variables (12-factor style).

Everything has a safe local default so ``python run_verishield.py`` works with no
configuration. Production deployments override these through the environment or
an ``.env`` file (see ``.env.example``).
"""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal ``.env`` loader (KEY=VALUE lines); real environment variables win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    env: str = "development"                  # development | production
    host: str = "127.0.0.1"
    port: int = 8000
    data_dir: Path = ROOT / "data" / "verishield"
    database_url: str = ""
    secret_key: str = ""
    tenant_code: str = "BOP"
    tenant_name: str = "Bank of Palestine"
    tenant_currency: str = "ILS"
    tenant_timezone: str = "Asia/Hebron"
    seed_demo: bool = True                    # create synthetic demo data on first start
    seed_demo_users: bool = True              # create the other four initial accounts (temporary passwords)
    demo_customers: int = 180
    demo_history_days: int = 45
    demo_live_channels: bool = True           # create & enable ready-to-use live channels in Demo Mode
    demo_stream_rate: float = 0.5             # synthetic payments per second on the built-in stream
    demo_tcp_port: int = 9009
    demo_http_port: int = 9010
    bootstrap_username: str = "1000"
    bootstrap_password: str = "Admin"
    require_mfa: bool = False                 # forced on in production
    cors_origins: list[str] = field(default_factory=list)
    ssl_certfile: str = ""                    # PEM certificate (enables HTTPS)
    ssl_keyfile: str = ""                     # PEM private key
    log_file: str = ""                        # rotating log file (Windows service / server installs)
    show_dev_accounts: bool = True            # list development accounts on the sign-in page
    background_jobs: bool = True
    engine_config: Path = ROOT / "config" / "banksealer.yaml"

    @property
    def production(self) -> bool:
        return self.env == "production"

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"

    @property
    def evidence_dir(self) -> Path:
        return self.data_dir / "evidence"

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"

    @property
    def assets_dir(self) -> Path:
        return self.data_dir / "assets"


def load_settings() -> Settings:
    _load_dotenv(ROOT / ".env")
    env = os.environ.get("VERISHIELD_ENV", "development").strip().lower()
    production = env == "production"
    data_dir = Path(os.environ.get("VERISHIELD_DATA_DIR", str(ROOT / "data" / "verishield"))).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    secret = os.environ.get("VERISHIELD_SECRET_KEY", "")
    if not secret:
        # Development convenience: persist a random key in the data directory.
        key_file = data_dir / ".secret_key"
        if key_file.exists():
            secret = key_file.read_text().strip()
        else:
            secret = secrets.token_urlsafe(48)
            key_file.write_text(secret)
            try:
                key_file.chmod(0o600)
            except OSError:
                pass

    db_url = os.environ.get("DATABASE_URL") or os.environ.get("VERISHIELD_DATABASE_URL") or \
        f"sqlite:///{(data_dir / 'verishield.db').as_posix()}"
    if db_url.startswith("postgres://"):            # Heroku/Render style URL
        db_url = "postgresql+psycopg://" + db_url[len("postgres://"):]
    elif db_url.startswith("postgresql://"):
        db_url = "postgresql+psycopg://" + db_url[len("postgresql://"):]

    cors = [o.strip() for o in os.environ.get("VERISHIELD_CORS_ORIGINS", "").split(",") if o.strip()]
    return Settings(
        env=env,
        host=os.environ.get("VERISHIELD_HOST", "0.0.0.0" if production else "127.0.0.1"),
        port=int(os.environ.get("PORT", os.environ.get("VERISHIELD_PORT", "8000"))),
        data_dir=data_dir,
        database_url=db_url,
        secret_key=secret,
        tenant_code=os.environ.get("VERISHIELD_TENANT_CODE", "BOP").upper(),
        tenant_name=os.environ.get("VERISHIELD_TENANT_NAME", "Bank of Palestine"),
        tenant_currency=os.environ.get("VERISHIELD_TENANT_CURRENCY", "ILS").upper(),
        tenant_timezone=os.environ.get("VERISHIELD_TENANT_TIMEZONE", "Asia/Hebron"),
        seed_demo=_bool("VERISHIELD_SEED_DEMO", True),
        seed_demo_users=_bool("VERISHIELD_SEED_DEMO_USERS", not production),
        demo_customers=int(os.environ.get("VERISHIELD_DEMO_CUSTOMERS", "180")),
        demo_history_days=int(os.environ.get("VERISHIELD_DEMO_HISTORY_DAYS", "45")),
        demo_live_channels=_bool("VERISHIELD_DEMO_LIVE_CHANNELS", True),
        demo_stream_rate=float(os.environ.get("VERISHIELD_DEMO_STREAM_RATE", "0.5")),
        demo_tcp_port=int(os.environ.get("VERISHIELD_DEMO_TCP_PORT", "9009")),
        demo_http_port=int(os.environ.get("VERISHIELD_DEMO_HTTP_PORT", "9010")),
        bootstrap_username=os.environ.get("VERISHIELD_BOOTSTRAP_USERNAME", "1000"),
        bootstrap_password=os.environ.get("VERISHIELD_BOOTSTRAP_PASSWORD", "Admin"),
        require_mfa=_bool("VERISHIELD_REQUIRE_MFA", production),
        cors_origins=cors,
        background_jobs=_bool("VERISHIELD_BACKGROUND_JOBS", True),
        ssl_certfile=os.environ.get("VERISHIELD_SSL_CERTFILE", ""),
        ssl_keyfile=os.environ.get("VERISHIELD_SSL_KEYFILE", ""),
        log_file=os.environ.get("VERISHIELD_LOG_FILE", ""),
        show_dev_accounts=_bool("VERISHIELD_SHOW_DEV_ACCOUNTS", not production),
    )


settings = load_settings()


def configure(**overrides) -> Settings:
    """Replace settings values (used by tests and the CLI)."""
    for key, value in overrides.items():
        setattr(settings, key, value)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return settings
