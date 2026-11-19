"""Contract for sending notifications (letters by email).

The pilot runs in shadow mode: by default nothing leaves the system. Microsoft Graph sending
is behind a settings flag that is off by default, and it refuses every address that is not
on the NOTIFICATION_ALLOWLIST. Callers import this module only.
"""

from typing import Protocol

from pydantic import BaseModel

from app.shared.errors import PermanentError


class Attachment(BaseModel):
    filename: str
    content_type: str
    data: bytes


class Notification(BaseModel):
    to: str  # one email address
    subject: str
    body: str  # plain text
    attachments: list[Attachment] = []


class NotificationReceipt(BaseModel):
    delivered: bool  # False when the message was only recorded
    channel: str  # "recorded" or "graph"
    message_id: str | None = None


class NotAllowlisted(PermanentError):
    """The recipient is not on the allowlist. Nothing was sent."""


class NotificationService(Protocol):
    async def send(self, notification: Notification) -> NotificationReceipt: ...


class GraphTransport(Protocol):
    """Microsoft Graph's sendMail call. Only GraphNotificationService uses it."""

    async def send_mail(self, *, sender: str, message: dict[str, object]) -> None: ...
