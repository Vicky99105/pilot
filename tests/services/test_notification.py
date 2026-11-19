"""Notification: the Graph sender's allowlist, with a fake transport. Graph is never called."""

import base64

import pytest

from app.config import Settings
from app.services.notification.contract import (
    Attachment,
    NotAllowlisted,
    Notification,
    NotificationService,
)
from app.services.notification.service import (
    GraphNotificationService,
    RecordingNotificationService,
    build_notification_service,
    parse_allowlist,
)
from tests.fakes.notifications import FakeGraphTransport

PDF = Attachment(filename="letter.pdf", content_type="application/pdf", data=b"%PDF-1.4 x")


def note(to: str) -> Notification:
    return Notification(
        to=to, subject="Claim letter", body="Your letter is attached.", attachments=[PDF]
    )


def graph(transport: FakeGraphTransport, allowlist: str) -> NotificationService:
    return GraphNotificationService(
        transport, sender="claims@insurer.example", allowlist=parse_allowlist(allowlist)
    )


async def test_graph_refuses_an_address_not_on_the_allowlist() -> None:
    transport = FakeGraphTransport()
    service = graph(transport, "pilot.lead@insurer.example")

    with pytest.raises(NotAllowlisted, match=r"asha@member\.example"):
        await service.send(note("asha@member.example"))

    assert transport.messages == []


async def test_an_empty_allowlist_refuses_every_address() -> None:
    transport = FakeGraphTransport()

    with pytest.raises(NotAllowlisted):
        await graph(transport, " ").send(note("pilot.lead@insurer.example"))

    assert transport.messages == []


async def test_graph_sends_to_an_allowlisted_address_with_the_attachment() -> None:
    transport = FakeGraphTransport()
    service = graph(transport, "Pilot.Lead@Insurer.example, qa@insurer.example")

    receipt = await service.send(note("pilot.lead@insurer.example"))

    assert (receipt.delivered, receipt.channel) == (True, "graph")
    [(sender, message)] = transport.messages
    assert sender == "claims@insurer.example"
    assert message["toRecipients"] == [{"emailAddress": {"address": "pilot.lead@insurer.example"}}]
    [attachment] = message["attachments"]
    assert attachment["name"] == "letter.pdf"
    assert base64.b64decode(attachment["contentBytes"]) == PDF.data


async def test_the_default_is_the_recording_service_and_it_sends_nothing() -> None:
    service = build_notification_service(Settings())

    assert isinstance(service, RecordingNotificationService)
    receipt = await service.send(note("asha@member.example"))
    assert (receipt.delivered, receipt.channel) == (False, "recorded")
    assert [n.to for n in service.sent] == ["asha@member.example"]


def test_graph_on_without_a_sender_or_token_fails_at_startup() -> None:
    with pytest.raises(ValueError, match="GRAPH_SENDER"):
        build_notification_service(Settings(notifications_graph_enabled=True))
