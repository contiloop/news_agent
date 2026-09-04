"""Pure health decisions and persisted state for a one-shot job watchdog."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from news_agent.watchdog_signals import LaunchdSnapshot, RunRecord

GOOD_RUN_STATUSES = {"completed", "no_work"}
BAD_RUN_STATUSES = {"failed", "completed_with_errors"}


class _StateModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class RunHistory(_StateModel):
    last_run_at: AwareDatetime | None = None
    last_run_id: str | None = None
    last_status: (
        Literal["completed", "no_work", "failed", "completed_with_errors"] | None
    ) = None
    last_success_at: AwareDatetime | None = None
    last_started_at: AwareDatetime | None = None
    last_started_run_id: str | None = None
    consecutive_failures: int = Field(default=0, ge=0)
    last_error_type: str | None = None
    last_http_status: int | None = Field(default=None, ge=100, le=599)
    recent_run_ids: tuple[str, ...] = ()


class TargetAlertState(_StateModel):
    destination_key: str
    notified_unhealthy: bool = False
    pending_unhealthy: bool | None = None
    attempts: int = Field(default=0, ge=0)
    next_attempt_at: AwareDatetime | None = None
    last_attempt_at: AwareDatetime | None = None
    receipt_ids: tuple[str, ...] = ()


class MonitorState(_StateModel):
    version: Literal[1] = 1
    service_label: str
    source_id: str
    first_checked_at: AwareDatetime
    last_checked_at: AwareDatetime
    resume_grace_until: AwareDatetime | None = None
    running_since: AwareDatetime | None = None
    probe_failure_count: int = Field(default=0, ge=0)
    unexpected_exit_count: int = Field(default=0, ge=0)
    history: RunHistory = Field(default_factory=RunHistory)
    alert_active: bool = False
    incident_reason: str | None = None
    incident_since: AwareDatetime | None = None
    targets: dict[str, TargetAlertState] = Field(default_factory=dict)


@dataclass(frozen=True)
class WatchdogPolicy:
    failure_threshold: int = 2
    stale_after_seconds: int = 2700
    max_run_seconds: int = 1800
    check_interval_seconds: int = 300
    resume_grace_seconds: int = 1200

    def __post_init__(self) -> None:
        for value in (
            self.failure_threshold,
            self.stale_after_seconds,
            self.max_run_seconds,
            self.check_interval_seconds,
            self.resume_grace_seconds,
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError("Watchdog policy values must be positive integers")


@dataclass(frozen=True)
class HealthAssessment:
    status: Literal["healthy", "unhealthy", "pending"]
    reason: str


def merge_run_history(
    previous: RunHistory,
    records: tuple[RunRecord, ...],
) -> RunHistory:
    """Merge bounded log tails without counting an already-seen run twice."""

    history = previous.model_copy(deep=True)
    for record in sorted(records, key=lambda item: (item.timestamp, item.run_id)):
        if record.event == "run_started":
            if (
                history.last_started_at is None
                or record.timestamp > history.last_started_at
            ):
                history.last_started_at = record.timestamp
                history.last_started_run_id = record.run_id
            continue
        if record.event not in {"run_finished", "run_failed"}:
            continue
        status = "failed" if record.event == "run_failed" else record.status
        if status not in GOOD_RUN_STATUSES | BAD_RUN_STATUSES:
            continue
        if record.run_id in history.recent_run_ids:
            continue
        if history.last_run_at is not None and (
            record.timestamp,
            record.run_id,
        ) <= (history.last_run_at, history.last_run_id or ""):
            continue

        history.last_run_at = record.timestamp
        history.last_run_id = record.run_id
        history.recent_run_ids = (*history.recent_run_ids, record.run_id)[-64:]
        history.last_status = status
        history.last_error_type = record.error_type
        history.last_http_status = record.http_status
        if status in GOOD_RUN_STATUSES:
            history.last_success_at = record.timestamp
            history.consecutive_failures = 0
        else:
            history.consecutive_failures += 1
    return history


def evaluate_health(
    launchd: LaunchdSnapshot,
    state: MonitorState,
    *,
    now: datetime,
    policy: WatchdogPolicy,
) -> HealthAssessment:
    """Treat ordinary idle/running as normal, never as proof of recovery."""

    history = state.history
    if launchd.loaded is False:
        return HealthAssessment("unhealthy", "service_unloaded")
    if launchd.disabled:
        return HealthAssessment("unhealthy", "service_disabled")
    if launchd.loaded is None or launchd.error is not None:
        return HealthAssessment(
            "unhealthy" if state.probe_failure_count >= 2 else "pending",
            "probe_failed",
        )

    if state.resume_grace_until is not None and now < state.resume_grace_until:
        resumed_at = state.resume_grace_until.timestamp() - policy.resume_grace_seconds
        if history.last_run_at is None or history.last_run_at.timestamp() < resumed_at:
            return HealthAssessment("pending", "resume_grace")

    active_start = history.last_started_at
    if (
        active_start is not None
        and history.last_run_at is not None
        and active_start <= history.last_run_at
    ):
        active_start = None
    if launchd.running:
        start = active_start or state.running_since or now
        if (now - start).total_seconds() > policy.max_run_seconds:
            return HealthAssessment("unhealthy", "run_hung")
        # A terminal event emitted during this process is sufficient evidence;
        # otherwise merely starting a fresh process cannot close an incident.
        if (
            active_start is not None
            or history.last_run_at is None
            or state.running_since is None
            or history.last_run_at < state.running_since
        ):
            return HealthAssessment("pending", "running")

    if state.unexpected_exit_count >= 2:
        return HealthAssessment("unhealthy", "process_failed")
    if history.consecutive_failures >= policy.failure_threshold:
        return HealthAssessment("unhealthy", "consecutive_failures")

    progress_at = (
        history.last_success_at or history.last_run_at or state.first_checked_at
    )
    if (now - progress_at).total_seconds() > policy.stale_after_seconds:
        return HealthAssessment("unhealthy", "no_progress")
    if active_start is not None:
        if (now - active_start).total_seconds() > policy.max_run_seconds:
            return HealthAssessment("unhealthy", "incomplete_run")
        return HealthAssessment("pending", "awaiting_run_result")
    if history.last_run_at is None:
        return HealthAssessment("pending", "waiting_for_first_run")
    if state.unexpected_exit_count:
        return HealthAssessment("pending", "unconfirmed_exit")
    if history.last_status in GOOD_RUN_STATUSES:
        if (
            state.alert_active
            and state.incident_since is not None
            and (
                history.last_success_at is None
                or history.last_success_at < state.incident_since
            )
        ):
            return HealthAssessment("pending", "awaiting_recovery_run")
        return HealthAssessment("healthy", "run_succeeded")
    return HealthAssessment("pending", "transient_failure")
