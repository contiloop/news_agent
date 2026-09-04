"""Browser-free launchd health checks with durable Telegram transitions."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
from pydantic import ValidationError

from news_agent.config import RssSourceConfig, TelegramNotificationTarget, load_config
from news_agent.notifier import NotificationSendError, TelegramNotifier
from news_agent.run_lock import RunLock, RunLockBusyError, RunLockError
from news_agent.watchdog_health import (
    GOOD_RUN_STATUSES,
    MonitorState,
    RunHistory,
    TargetAlertState,
    WatchdogPolicy,
    evaluate_health,
    merge_run_history,
)
from news_agent.watchdog_signals import probe_launchd, read_run_log
from news_agent.watchdog_state import (
    WatchdogStateError,
    load_watchdog_state,
    save_watchdog_state,
)

KST = timezone(timedelta(hours=9), name="KST")
_REASONS = {
    "service_unloaded": "수집 작업이 launchd에서 내려가 있습니다.",
    "service_disabled": "수집 작업이 비활성화되어 있습니다.",
    "probe_failed": "수집 작업의 상태를 연속해서 확인하지 못했습니다.",
    "process_failed": "수집 프로세스가 정상 결과 없이 비정상 종료했습니다.",
    "consecutive_failures": "최근 수집 실행이 연속해서 실패했습니다.",
    "no_progress": "허용 시간 동안 정상 수집 결과가 확인되지 않았습니다.",
    "run_hung": "실행 중인 수집 작업이 제한 시간을 초과했습니다.",
    "incomplete_run": "시작된 수집 작업의 종료 결과가 확인되지 않습니다.",
}


class WatchdogError(RuntimeError):
    """A sanitized failure of the watchdog itself, not of the watched job."""


@dataclass(frozen=True)
class WatchdogNotificationResult:
    target_id: str
    status: str
    kind: str
    receipt_ids: tuple[str, ...] = ()
    error: str | None = None
    next_attempt_at: str | None = None


@dataclass(frozen=True)
class WatchdogResult:
    status: str
    reason: str
    source_id: str
    service_label: str
    observed_at: str
    incident_active: bool = False
    changed: bool = False
    consecutive_failures: int = 0
    last_run_at: str | None = None
    last_success_at: str | None = None
    last_run_status: str | None = None
    log_error: str | None = None
    dry_run: bool = False
    notifications: tuple[WatchdogNotificationResult, ...] = ()

    @property
    def notification_failed(self) -> bool:
        return any(item.status == "retry_scheduled" for item in self.notifications)

    @property
    def is_idle(self) -> bool:
        return not self.changed and not self.notifications and not self.dry_run

    def as_payload(self) -> dict[str, object]:
        return asdict(self)


def watchdog_once(
    config_path: str | Path,
    *,
    service_label: str,
    run_log_file: str | Path,
    state_file: str | Path,
    policy: WatchdogPolicy | None = None,
    now: datetime | None = None,
    client: httpx.Client | None = None,
    environ: Mapping[str, str] | None = None,
    command_runner: Callable[..., Any] | None = None,
    dry_run: bool = False,
) -> WatchdogResult:
    """Check once; dry-run never sends, creates a lock, or writes state."""

    selected_now = _utc_now(now)
    selected_policy = policy or WatchdogPolicy()
    config = load_config(config_path)
    if config.notifications is None or not config.notifications.targets:
        raise WatchdogError("Watchdog requires configured Telegram targets")
    selected_state = Path(state_file).expanduser().absolute()
    selected_log = Path(run_log_file).expanduser().absolute()
    try:
        conflicts = selected_state.resolve() in {
            Path(config_path).expanduser().resolve(),
            selected_log.resolve(),
            config.database_file.resolve(),
        }
    except (OSError, RuntimeError, ValueError):
        raise WatchdogError("Watchdog paths cannot be safely resolved") from None
    if conflicts:
        raise WatchdogError("Watchdog state must use its own dedicated file")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}", service_label):
        raise WatchdogError("Watchdog service label is invalid")

    arguments = dict(
        config=config,
        service_label=service_label,
        run_log_file=selected_log,
        state_file=selected_state,
        policy=selected_policy,
        now=selected_now,
        client=client,
        environ=environ,
        command_runner=command_runner,
        dry_run=dry_run,
    )
    try:
        if dry_run:
            return _check_once(**arguments)
        with RunLock(Path(f"{selected_state}.lock")):
            return _check_once(**arguments)
    except RunLockBusyError:
        return WatchdogResult(
            status="already_running",
            reason="watchdog_lock_busy",
            source_id=config.source_id,
            service_label=service_label,
            observed_at=_timestamp(selected_now),
        )
    except (WatchdogStateError, RunLockError, ValidationError):
        raise WatchdogError("Watchdog state could not be safely used") from None


def _check_once(
    *,
    config: RssSourceConfig,
    service_label: str,
    run_log_file: Path,
    state_file: Path,
    policy: WatchdogPolicy,
    now: datetime,
    client: httpx.Client | None,
    environ: Mapping[str, str] | None,
    command_runner: Callable[..., Any] | None,
    dry_run: bool,
) -> WatchdogResult:
    raw_state = load_watchdog_state(state_file)
    has_state = bool(raw_state) or state_file.exists()
    state = (
        MonitorState.model_validate(raw_state)
        if has_state
        else MonitorState(
            source_id=config.source_id,
            service_label=service_label,
            first_checked_at=now,
            last_checked_at=now,
        )
    )
    if (state.source_id, state.service_label) != (config.source_id, service_label):
        raise WatchdogError("Watchdog state belongs to a different source or service")
    elapsed = (now - state.last_checked_at).total_seconds()
    if has_state and (elapsed > policy.check_interval_seconds * 3 or elapsed < 0):
        state.resume_grace_until = now + timedelta(seconds=policy.resume_grace_seconds)
        state.running_since = None
    if elapsed < 0:
        # A wall-clock rollback must not leave future history/retry times able
        # to suppress monitoring indefinitely. Await fresh evidence after grace.
        state.first_checked_at = now
        state.history = RunHistory()
        if state.alert_active:
            state.incident_since = now
        for delivery in state.targets.values():
            delivery.next_attempt_at = now + timedelta(
                seconds=policy.check_interval_seconds
            )
    state.last_checked_at = now

    launchd = probe_launchd(service_label, command_runner=command_runner)
    log = read_run_log(run_log_file, now=now)
    state.history = merge_run_history(state.history, log.records)
    if log.error in {"log_unreadable", "log_not_regular_file", "invalid_log_path"}:
        launchd = replace(launchd, error=log.error)
    state.probe_failure_count = (
        state.probe_failure_count + 1
        if launchd.loaded is None or launchd.error is not None
        else 0
    )
    state.running_since = (state.running_since or now) if launchd.running else None
    unfinished = state.history.last_started_at is not None and (
        state.history.last_run_at is None
        or state.history.last_started_at > state.history.last_run_at
    )
    unexpected_exit = (
        not launchd.running
        and launchd.last_exit_code not in {None, 0}
        and (
            state.history.last_status in GOOD_RUN_STATUSES
            or state.history.last_run_at is None
            or unfinished
        )
    )
    state.unexpected_exit_count = (
        state.unexpected_exit_count + 1 if unexpected_exit else 0
    )
    assessment = evaluate_health(launchd, state, now=now, policy=policy)
    previous_active = state.alert_active
    previous_reason = state.incident_reason
    if assessment.status == "unhealthy":
        if not state.alert_active:
            state.incident_since = now
        state.alert_active = True
        state.incident_reason = assessment.reason
    elif assessment.status == "healthy":
        state.alert_active = False
    changed = previous_active != state.alert_active or (
        state.alert_active and previous_reason != state.incident_reason
    )

    if not dry_run:
        _save(state_file, state)
    notifications: list[WatchdogNotificationResult] = []
    if assessment.status != "pending":
        for target in config.notifications.targets:
            result = _notify_transition(
                target,
                state,
                state_file=state_file,
                now=now,
                client=client,
                environ=environ,
                dry_run=dry_run,
            )
            if result is not None:
                notifications.append(result)
    if not dry_run:
        _save(state_file, state)
    return WatchdogResult(
        status=assessment.status,
        reason=assessment.reason,
        source_id=config.source_id,
        service_label=service_label,
        observed_at=_timestamp(now),
        incident_active=state.alert_active,
        changed=changed,
        consecutive_failures=state.history.consecutive_failures,
        last_run_at=_optional_timestamp(state.history.last_run_at),
        last_success_at=_optional_timestamp(state.history.last_success_at),
        last_run_status=state.history.last_status,
        log_error=log.error,
        dry_run=dry_run,
        notifications=tuple(notifications),
    )


def _notify_transition(
    target: TelegramNotificationTarget,
    state: MonitorState,
    *,
    state_file: Path,
    now: datetime,
    client: httpx.Client | None,
    environ: Mapping[str, str] | None,
    dry_run: bool,
) -> WatchdogNotificationResult | None:
    destination = hashlib.sha256(
        json.dumps([str(target.api_base_url), target.chat_id]).encode("utf-8")
    ).hexdigest()
    delivery = state.targets.get(target.id)
    if delivery is None or delivery.destination_key != destination:
        delivery = TargetAlertState(destination_key=destination)
        state.targets[target.id] = delivery
    desired = state.alert_active
    if delivery.notified_unhealthy == desired:
        delivery.pending_unhealthy = None
        delivery.attempts = 0
        delivery.next_attempt_at = None
        return None
    if delivery.pending_unhealthy != desired:
        delivery.pending_unhealthy = desired
        delivery.attempts = 0
        delivery.next_attempt_at = None
    if delivery.next_attempt_at is not None and now < delivery.next_attempt_at:
        return None
    kind = "incident" if desired else "recovery"
    if dry_run:
        return WatchdogNotificationResult(target.id, "would_send", kind)

    delivery.attempts += 1
    delivery.last_attempt_at = now
    delay = min(3600, 300 * 2 ** min(delivery.attempts - 1, 4))
    delivery.next_attempt_at = now + timedelta(seconds=delay)
    # Save the attempt before I/O. A crash after Telegram accepts but before the
    # receipt is saved may duplicate a later retry; Telegram has no idempotency key.
    _save(state_file, state)
    try:
        receipts = TelegramNotifier(target, client=client, environ=environ).send_text(
            _alert_text(state, now=now)
        )
    except NotificationSendError as exc:
        retry_delay = max(delay, exc.retry_after_seconds or 0)
        if not exc.retryable:
            retry_delay = max(retry_delay, 3600)
        delivery.next_attempt_at = now + timedelta(seconds=retry_delay)
        _save(state_file, state)
        return WatchdogNotificationResult(
            target.id,
            "retry_scheduled",
            kind,
            receipt_ids=exc.receipt_ids,
            error=str(exc),
            next_attempt_at=_timestamp(delivery.next_attempt_at),
        )
    delivery.notified_unhealthy = desired
    delivery.pending_unhealthy = None
    delivery.attempts = 0
    delivery.next_attempt_at = None
    delivery.receipt_ids = receipts
    _save(state_file, state)
    return WatchdogNotificationResult(target.id, "sent", kind, receipt_ids=receipts)


def _alert_text(state: MonitorState, *, now: datetime) -> str:
    source = (
        "Reuters"
        if state.source_id == "reuters"
        else re.sub(r"\s+", " ", state.source_id)[:80]
    )
    if not state.alert_active:
        return "\n".join(
            (
                f"✅ {source} 자동 수집 복구",
                "정상 실행 결과를 확인했습니다.",
                f"정상 실행: {_display_time(state.history.last_success_at)}",
                f"결과: {state.history.last_status}",
                "감시는 계속 유지됩니다.",
            )
        )
    lines = [
        f"⚠️ {source} 자동 수집 이상",
        _REASONS.get(state.incident_reason or "", "수집 상태가 비정상입니다."),
        f"확인: {_display_time(now)}",
        f"최근 정상: {_display_time(state.history.last_success_at)}",
        f"최근 실행: {_display_time(state.history.last_run_at)}",
        f"연속 실패: {state.history.consecutive_failures}회",
    ]
    if state.history.last_http_status is not None:
        lines.append(f"최근 HTTP 응답: {state.history.last_http_status}")
    lines.append("같은 장애는 반복 알리지 않고, 정상 실행 확인 후 복구를 알립니다.")
    return "\n".join(lines)


def _save(path: Path, state: MonitorState) -> None:
    save_watchdog_state(path, state.model_dump(mode="json"))


def _utc_now(value: datetime | None) -> datetime:
    current = datetime.now(UTC) if value is None else value
    if (
        not isinstance(current, datetime)
        or current.tzinfo is None
        or current.utcoffset() is None
    ):
        raise WatchdogError("Watchdog time must include a timezone")
    return current.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _optional_timestamp(value: datetime | None) -> str | None:
    return None if value is None else _timestamp(value)


def _display_time(value: datetime | None) -> str:
    return (
        "기록 없음"
        if value is None
        else value.astimezone(KST).strftime("%m-%d %H:%M KST")
    )
