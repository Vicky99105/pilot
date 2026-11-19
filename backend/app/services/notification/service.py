"""Notification services: the recording default (shadow mode) and the Microsoft Graph sender
with its allowlist."""

import base64
import logging
from typing import Any

import httpx
from pydantic import SecretStr

from app.config import Settings
from app.services.notification.contract import (
    GraphTransport,
    NotAllowlisted,
    Notification,
    NotificationReceipt,
    NotificationService,
)

logger = logging.getLogger(__name__)

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"


def parse_allowlist(raw: str) -> frozenset[str]:
    """Comma-separated addresses, compared case-insensitively."""
    return frozenset(a.strip().lower() for a in raw.split(",") if a.strip())


class RecordingNotificationService:
    """The default: records and logs each notification and delivers nothing."""

    def __init__(self) -> None:
        self.sent: list[Notification] = []

    async def send(self, notification: Notification) -> NotificationReceipt:
        self.sent.append(notification)
        logger.info(
            "Notification recorded, not sent: %r to %s", notification.subject, notification.to
        )
        return NotificationReceipt(delivered=False, channel="recorded")


class GraphNotificationService:
    """Sends through Microsoft Graph, only to allowlisted addresses."""

    def __init__(self, transport: GraphTransport, *, sender: str, allowlist: frozenset[str]):
        self._transport = transport
        self._sender = sender
        self._allowlist = allowlist

    async def send(self, notification: Notification) -> NotificationReceipt:
        address = notification.to.strip().lower()
        if address not in self._allowlist:
            raise NotAllowlisted(f"{notification.to} is not on NOTIFICATION_ALLOWLIST")
        message: dict[str, Any] = {
            "subject": notification.subject,
            "body": {"contentType": "Text", "content": notification.body},
            "toRecipients": [{"emailAddress": {"address": address}}],
            "attachments": [
                {
                    "@odata.type": "#microsoft.graph.fileAttachment",
                    "name": a.filename,
                    "contentType": a.content_type,
                    "contentBytes": base64.b64encode(a.data).decode("ascii"),
                }
                for a in notification.attachments
            ],
        }
        await self._transport.send_mail(sender=self._sender, message=message)
        return NotificationReceipt(delivered=True, channel="graph")


class HttpGraphTransport:
    """POST /users/{sender}/sendMail with a bearer token. Not used until admin consent."""

    def __init__(self, http: httpx.AsyncClient, token: SecretStr) -> None:
        self._http = http
        self._token = token

    async def send_mail(self, *, sender: str, message: dict[str, object]) -> None:
        response = await self._http.post(
            f"{GRAPH_BASE_URL}/users/{sender}/sendMail",
            headers={"Authorization": f"Bearer {self._token.get_secret_value()}"},
            json={"message": message, "saveToSentItems": True},
        )
        response.raise_for_status()


def build_notification_service(
    settings: Settings, http: httpx.AsyncClient | None = None
) -> NotificationService:
    if not settings.notifications_graph_enabled:
        return RecordingNotificationService()
    token = settings.graph_access_token
    if not settings.graph_sender or token is None or not token.get_secret_value():
        raise ValueError(
            "NOTIFICATIONS_GRAPH_ENABLED needs GRAPH_SENDER and GRAPH_ACCESS_TOKEN to be set"
        )
    return GraphNotificationService(
        HttpGraphTransport(http or httpx.AsyncClient(timeout=20.0), token),
        sender=settings.graph_sender,
        allowlist=parse_allowlist(settings.notification_allowlist),
    )
