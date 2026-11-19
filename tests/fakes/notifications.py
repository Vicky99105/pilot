from typing import Any

from app.services.notification.contract import Notification, NotificationReceipt


class FakeNotificationService:
    """Implements the NotificationService contract: records every send and delivers it."""

    def __init__(self) -> None:
        self.sent: list[Notification] = []

    async def send(self, notification: Notification) -> NotificationReceipt:
        self.sent.append(notification)
        return NotificationReceipt(
            delivered=True, channel="fake", message_id=f"fake-{len(self.sent)}"
        )


class FakeGraphTransport:
    """Stands in for Microsoft Graph's sendMail. Records each message; never calls out."""

    def __init__(self) -> None:
        self.messages: list[tuple[str, dict[str, Any]]] = []

    async def send_mail(self, *, sender: str, message: dict[str, Any]) -> None:
        self.messages.append((sender, message))
