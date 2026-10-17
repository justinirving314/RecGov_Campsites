"""Push alerts to your phone (ntfy, Pushover) and/or email."""

from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage

import httpx

from .config import Settings

log = logging.getLogger(__name__)


@dataclass
class Notification:
    title: str
    message: str
    url: str | None = None
    url_label: str = "Open"
    urgent: bool = True


class Notifier:
    def __init__(self, settings: Settings, http: httpx.AsyncClient):
        self.settings = settings
        self._http = http

    @property
    def channels(self) -> list[str]:
        s = self.settings
        channels = []
        if s.ntfy_topic:
            channels.append("ntfy")
        if s.pushover_user_key and s.pushover_app_token:
            channels.append("pushover")
        if s.smtp_host and s.email_recipients:
            channels.append("email")
        return channels

    async def send(self, note: Notification) -> dict[str, str | None]:
        """Send on every configured channel. Returns channel -> error message (None if sent)."""
        senders = {"ntfy": self._ntfy, "pushover": self._pushover, "email": self._email}
        results: dict[str, str | None] = {}
        for channel in self.channels:
            try:
                await senders[channel](note)
                results[channel] = None
            except Exception as exc:  # one broken channel must not block the others
                log.warning("notification via %s failed: %s", channel, exc)
                results[channel] = str(exc) or type(exc).__name__
        if not results:
            log.warning("no notification channel configured; alert only shown in the app")
        return results

    async def _ntfy(self, note: Notification) -> None:
        s = self.settings
        body: dict = {
            "topic": s.ntfy_topic,
            "title": note.title,
            "message": note.message,
            "priority": 5 if note.urgent else 3,
            "tags": ["camping"],
        }
        if note.url:
            body["click"] = note.url
            body["actions"] = [{"action": "view", "label": note.url_label, "url": note.url}]
        headers = {}
        if s.ntfy_token:
            headers["Authorization"] = f"Bearer {s.ntfy_token.get_secret_value()}"
        resp = await self._http.post(s.ntfy_server.rstrip("/"), json=body, headers=headers)
        resp.raise_for_status()

    async def _pushover(self, note: Notification) -> None:
        s = self.settings
        data = {
            "token": s.pushover_app_token.get_secret_value(),
            "user": s.pushover_user_key.get_secret_value(),
            "title": note.title,
            "message": note.message,
            "priority": 1 if note.urgent else 0,
        }
        if note.url:
            data["url"] = note.url
            data["url_title"] = note.url_label
        resp = await self._http.post("https://api.pushover.net/1/messages.json", data=data)
        resp.raise_for_status()

    async def _email(self, note: Notification) -> None:
        s = self.settings
        msg = EmailMessage()
        msg["Subject"] = note.title
        msg["From"] = s.smtp_from or s.smtp_username or "campwatch@localhost"
        msg["To"] = ", ".join(s.email_recipients)
        msg.set_content(f"{note.message}\n\n{note.url}" if note.url else note.message)
        await asyncio.to_thread(self._smtp_send, msg)

    def _smtp_send(self, msg: EmailMessage) -> None:
        s = self.settings
        context = ssl.create_default_context()
        if s.smtp_port == 465:
            server = smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, context=context, timeout=30)
        else:
            server = smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=30)
            server.starttls(context=context)
        with server:
            if s.smtp_username and s.smtp_password:
                server.login(s.smtp_username, s.smtp_password.get_secret_value())
            server.send_message(msg)
