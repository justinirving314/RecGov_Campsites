"""Runtime configuration, read from environment variables (prefix ``CAMPWATCH_``) or ``.env``."""

from __future__ import annotations

import secrets
from functools import cached_property
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CAMPWATCH_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Storage
    data_dir: Path = Path("data")

    # Polling
    poll_interval_seconds: int = Field(60, ge=30)
    request_delay_seconds: float = Field(1.0, ge=0)
    run_scheduler: bool = True

    # Web app login. Leave unset only when the app is reachable from your own machine alone.
    app_password: SecretStr | None = None
    secret_key: SecretStr | None = None
    cookie_secure: bool = False

    # Notification channels. Configure any subset.
    ntfy_topic: str | None = None
    ntfy_server: str = "https://ntfy.sh"
    ntfy_token: SecretStr | None = None

    pushover_user_key: SecretStr | None = None
    pushover_app_token: SecretStr | None = None

    smtp_host: str | None = None
    smtp_port: int = 465
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_from: str | None = None
    email_to: str | None = None  # comma-separated

    # Recreation.gov account, used only by the "hold in cart" action.
    recgov_email: str | None = None
    recgov_password: SecretStr | None = None
    booking_headless: bool = True

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.data_dir / 'campwatch.db'}"

    @property
    def email_recipients(self) -> list[str]:
        return [e.strip() for e in (self.email_to or "").split(",") if e.strip()]

    @property
    def auth_required(self) -> bool:
        return bool(self.app_password and self.app_password.get_secret_value())

    @property
    def booking_configured(self) -> bool:
        return bool(self.recgov_email and self.recgov_password)

    @cached_property
    def session_secret(self) -> bytes:
        """Key that signs login cookies. Generated once and kept in the data dir if not set."""
        if self.secret_key:
            return self.secret_key.get_secret_value().encode()
        path = self.data_dir / "secret_key"
        if not path.exists():
            self.data_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(secrets.token_urlsafe(32))
            path.chmod(0o600)
        return path.read_text().strip().encode()
