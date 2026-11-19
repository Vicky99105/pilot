"""Default supervisor service: overview queries on the claims database, and hold, resume and
reassignment through the task service, the event log and the workflow signaller."""

from collections import Counter, defaultdict
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter

from app.config import Settings
from app.database.models import ClaimRow, TaskRow, UserRoleRow, UserRow, utcnow
from app.services.monitoring.contract import Monitoring
from app.services.supervision.contract import (
    OVERRIDABLE_KINDS,
    ClaimSignaller,
    HeldClaim,
    InvalidAction,
    NearBreach,
    NotSupervisor,
    QueueDepth,
    SupervisorOverview,
    UserActivity,
)
from app.services.tasks.contract import Task, TaskService
from app.shared.types import Role, Stage

UNFINISHED = ("open", "taken", "escalated")
CLOSED = "closed"
# The Review area's queues: every staff role that has checkpoints.
QUEUE_ROLES = [
    Role.CLAIMS_OPERATOR,
    Role.BILL_PROCESSOR,
    Role.MEDICAL_OFFICER,
    Role.APPROVER,
    Role.CRC_REVIEWER,
    Role.INVESTIGATOR,
    Role.FINANCE,
    Role.QC_AUDITOR,
]
# Outcome keys that carry a person's corrections, and actions that change a suggestion.
_CORRECTIONS = ("labels", "fields", "overrides", "lines")
_CHANGING_ACTIONS = ("override", "overrule")


def changed_a_suggestion(outcome: dict[str, Any] | None) -> bool:
    """True when a completed task's outcome changed what the machine suggested."""
    if not outcome:
        return False
    return any(outcome.get(k) for k in _CORRECTIONS) or outcome.get("action") in _CHANGING_ACTIONS


class DefaultSupervisionService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        tasks: TaskService,
        monitoring: Monitoring,
        signaller: ClaimSignaller,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self._sessions = sessions
        self._tasks = tasks
        self._monitoring = monitoring
        self._signaller = signaller
        self._now = now

    async def overview(
        self, *, by: UUID, window_days: int = 7, near_breach_minutes: int = 60
    ) -> SupervisorOverview:
        await self._supervisor(by)
        now = self._now()
        async with self._sessions() as session:
            unfinished = list(
                await session.scalars(select(TaskRow).where(TaskRow.status.in_(UNFINISHED)))
            )
            done = list(
                await session.scalars(
                    select(TaskRow).where(
                        TaskRow.status == "done",
                        TaskRow.completed_at >= now - timedelta(days=window_days),
                    )
                )
            )
            claims = {
                c.id: c
                for c in await session.scalars(
                    select(ClaimRow).where(ClaimRow.id.in_({t.claim_id for t in unfinished}))
                )
            }
            held = list(
                await session.scalars(
                    select(ClaimRow).where(ClaimRow.held_at.is_not(None)).order_by(ClaimRow.held_at)
                )
            )
            people = {u.id: u.name for u in await session.scalars(select(UserRow))}
        return SupervisorOverview(
            generated_at=now,
            window_days=window_days,
            near_breach_minutes=near_breach_minutes,
            queues=_queues(unfinished, now),
            near_breach=_near_breach(unfinished, claims, people, now, near_breach_minutes),
            users=_activity(done, people),
            held_claims=[
                HeldClaim(
                    claim_id=c.id,
                    claim_number=c.claim_number,
                    status=c.status,
                    held_at=c.held_at,
                    held_by=c.held_by,
                    reason=c.hold_reason or "",
                )
                for c in held
                if c.held_at is not None
            ],
        )

    async def hold(self, claim_id: UUID, reason: str, *, by: UUID) -> HeldClaim:
        await self._supervisor(by)
        reason = _reason(reason)
        async with self._sessions.begin() as session:
            claim = await session.get(ClaimRow, claim_id, with_for_update=True)
            if claim is None:
                raise InvalidAction(f"claim {claim_id} not found")
            if claim.status == CLOSED:
                raise InvalidAction(f"claim {claim.claim_number or claim_id} is closed")
            if claim.held_at is not None:
                raise InvalidAction(f"claim {claim.claim_number or claim_id} is already held")
            claim.held_at, claim.held_by, claim.hold_reason = self._now(), by, reason
            held = HeldClaim(
                claim_id=claim.id,
                claim_number=claim.claim_number,
                status=claim.status,
                held_at=claim.held_at,
                held_by=by,
                reason=reason,
            )
            workflow_id = claim.workflow_id
        await self._monitoring.record_event(
            claim_id=claim_id,
            stage=Stage.MONITORING,
            action="claim_held",
            actor=f"user:{by}",
            detail={"reason": reason},
            moves_status=False,
        )
        if workflow_id:
            await self._signaller.hold(workflow_id=workflow_id)
        return held

    async def resume(self, claim_id: UUID, reason: str, *, by: UUID) -> None:
        await self._supervisor(by)
        reason = _reason(reason)
        async with self._sessions.begin() as session:
            claim = await session.get(ClaimRow, claim_id, with_for_update=True)
            if claim is None:
                raise InvalidAction(f"claim {claim_id} not found")
            if claim.held_at is None:
                raise InvalidAction(f"claim {claim.claim_number or claim_id} is not held")
            held_for = int((self._now() - claim.held_at).total_seconds())
            claim.held_at = claim.held_by = claim.hold_reason = None
            workflow_id = claim.workflow_id
        await self._monitoring.record_event(
            claim_id=claim_id,
            stage=Stage.MONITORING,
            action="claim_resumed",
            actor=f"user:{by}",
            detail={"reason": reason, "held_seconds": held_for},
            moves_status=False,
        )
        if workflow_id:
            await self._signaller.resume(workflow_id=workflow_id)

    async def reassign(self, task_id: UUID, to_user_id: UUID, reason: str, *, by: UUID) -> Task:
        await self._supervisor(by)
        reason = _reason(reason)
        task = await self._tasks.reassign(task_id=task_id, to_user_id=to_user_id)
        await self._monitoring.record_event(
            claim_id=task.claim_id,
            stage=Stage.MONITORING,
            action="task_reassigned",
            actor=f"user:{by}",
            detail={
                "task_id": str(task.id),
                "kind": task.kind,
                "to": str(to_user_id),
                "reason": reason,
            },
            moves_status=False,
        )
        return task

    async def _supervisor(self, user_id: UUID) -> None:
        async with self._sessions() as session:
            found = await session.scalar(
                select(UserRoleRow).where(
                    UserRoleRow.user_id == user_id, UserRoleRow.role == Role.SUPERVISOR.value
                )
            )
        if found is None:
            raise NotSupervisor(f"user {user_id} is not a supervisor")


def _reason(reason: str) -> str:
    reason = reason.strip()
    if not reason:
        raise InvalidAction("a reason is required")
    return reason


def _queues(unfinished: list[TaskRow], now: datetime) -> list[QueueDepth]:
    by_role: dict[str, list[TaskRow]] = defaultdict(list)
    for task in unfinished:
        by_role[task.role].append(task)
    return [
        QueueDepth(
            role=role,
            unfinished=len(by_role[role.value]),
            taken=sum(1 for t in by_role[role.value] if t.status == "taken"),
            overdue=sum(1 for t in by_role[role.value] if t.deadline < now),
            by_kind=dict(Counter(t.kind for t in by_role[role.value])),
        )
        for role in QUEUE_ROLES
    ]


def _near_breach(
    unfinished: list[TaskRow],
    claims: dict[UUID, ClaimRow],
    people: dict[UUID, str],
    now: datetime,
    minutes: int,
) -> list[NearBreach]:
    soon = sorted(
        (t for t in unfinished if t.deadline <= now + timedelta(minutes=minutes)),
        key=lambda t: t.deadline,
    )
    return [
        NearBreach(
            task_id=t.id,
            claim_id=t.claim_id,
            claim_number=claims[t.claim_id].claim_number if t.claim_id in claims else None,
            kind=t.kind,
            role=Role(t.role),
            deadline=t.deadline,
            minutes_left=int((t.deadline - now).total_seconds() // 60),
            assignee=people.get(t.assignee) if t.assignee else None,
            held=t.claim_id in claims and claims[t.claim_id].held_at is not None,
        )
        for t in soon
    ]


def _activity(done: list[TaskRow], people: dict[UUID, str]) -> list[UserActivity]:
    by_user: dict[UUID, list[TaskRow]] = defaultdict(list)
    for task in done:
        if task.assignee is not None:
            by_user[task.assignee].append(task)
    found = []
    for user_id, tasks in by_user.items():
        decided = [t for t in tasks if t.kind in OVERRIDABLE_KINDS]
        overridden = sum(1 for t in decided if changed_a_suggestion(t.outcome))
        found.append(
            UserActivity(
                user_id=user_id,
                name=people.get(user_id, str(user_id)),
                completed=len(tasks),
                decided=len(decided),
                overridden=overridden,
                override_rate=round(overridden / len(decided), 3) if decided else None,
            )
        )
    return sorted(found, key=lambda a: (-a.completed, a.name))


class DefaultClaimSignaller:
    """Sends hold() and resume() to a claim's Temporal workflow."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: Client | None = None

    async def hold(self, *, workflow_id: str) -> None:
        await (await self._get_client()).get_workflow_handle(workflow_id).signal("hold")

    async def resume(self, *, workflow_id: str) -> None:
        await (await self._get_client()).get_workflow_handle(workflow_id).signal("resume")

    async def _get_client(self) -> Client:
        if self._client is None:
            self._client = await Client.connect(
                self._settings.temporal_address,
                namespace=self._settings.temporal_namespace,
                data_converter=pydantic_data_converter,
            )
        return self._client
