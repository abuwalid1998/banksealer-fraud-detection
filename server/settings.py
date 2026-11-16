"""Runtime settings from environment variables (12-factor)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _list(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


@dataclass
class Settings:
    database_url: str = field(default_factory=lambda: os.environ.get(
        "DATABASE_URL", "postgresql+psycopg://banksealer:banksealer@localhost:5432/banksealer"))
    jwt_secret: str = field(default_factory=lambda: os.environ.get("JWT_SECRET", "change-me-in-production-use-a-long-random-secret"))
    jwt_expire_minutes: int = field(default_factory=lambda: int(os.environ.get("JWT_EXPIRE_MINUTES", "480")))
    admin_username: str = field(default_factory=lambda: os.environ.get("ADMIN_USERNAME", "admin"))
    admin_password: str = field(default_factory=lambda: os.environ.get("ADMIN_PASSWORD", "admin123"))
    data_dir: str = field(default_factory=lambda: os.environ.get("DATA_DIR", "data"))
    seed_config: str = field(default_factory=lambda: os.environ.get("SEED_CONFIG", "config/enterprise.yaml"))
    cors_origins: list = field(default_factory=lambda: _list(os.environ.get("CORS_ORIGINS", "*")))
    worker_poll_seconds: float = field(default_factory=lambda: float(os.environ.get("WORKER_POLL_SECONDS", "1.0")))
    worker_batch: int = field(default_factory=lambda: int(os.environ.get("WORKER_BATCH", "2000")))

    @property
    def reports_dir(self) -> str:
        return os.path.join(self.data_dir, "reports")

    @property
    def models_dir(self) -> str:
        return os.path.join(self.data_dir, "models")

    @property
    def uploads_dir(self) -> str:
        return os.path.join(self.data_dir, "uploads")

    def ensure_dirs(self) -> None:
        for path in (self.data_dir, self.reports_dir, self.models_dir, self.uploads_dir):
            os.makedirs(path, exist_ok=True)


settings = Settings()
