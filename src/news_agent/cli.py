"""One-shot commands for discovery, reading, analysis, and notification."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from uuid import uuid4

import httpx

from news_agent.browser_discovery import (
    BrowserDiscoveryError,
    collect_browser_section_candidates,
)
from news_agent.candidate_selector import (
    CandidateSelectorError,
    select_article_candidates,
)
from news_agent.config import ConfigLoadError, RssSourceConfig, load_config
from news_agent.memory import (
    MemoryError,
    MemoryRefreshResult,
    load_memory_context,
    refresh_memory_from_feedback,
)
from news_agent.notifier import NotificationSendError, TelegramNotifier
from news_agent.reader import ReaderError, read_article_body
from news_agent.rss import (
    DiscoveredItem,
    RssFetchError,
    RssParseError,
)
from news_agent.rss import (
    fetch_and_parse as fetch_and_parse_rss,
)
from news_agent.run_lock import RunLock, RunLockBusyError, RunLockError
from news_agent.sitemap import (
    SitemapFetchError,
    SitemapParseError,
    fetch_and_parse_sitemap,
    fetch_sitemap_conditionally,
    parse_sitemap,
)
from news_agent.storage import (
    ArticleReadFailure,
    ClaimedNotificationDelivery,
    NotificationTarget,
    PendingResult,
    StorageError,
    StoredArticle,
    StoredEventResolution,
    ack_notification_delivery,
    claim_notification_delivery,
    find_article_read_failures_due,
    find_articles_due_analysis_retry,
    find_event_candidates,
    find_pending_articles,
    get_article,
    get_article_by_url,
    get_article_event_resolution,
    get_discovery_http_validators,
    nack_notification_delivery,
    record_article_read_failure,
    record_browser_candidate_selection,
    renew_notification_delivery_lease,
    schedule_article_analysis_retry,
    select_browser_candidates_for_prompt,
    select_discoveries_for_run,
    store_article,
    store_article_analysis,
    store_discovery_http_validators,
)
from news_agent.summarizer import SummarizerError, summarize_article
from news_agent.telegram_feedback import (
    TelegramFeedbackError,
    TelegramFeedbackPollResult,
    poll_telegram_feedback,
)
from news_agent.watchdog import WatchdogError, watchdog_once
from news_agent.watchdog_health import WatchdogPolicy

DEFAULT_CONFIG_PATH = Path("config/cnbc-business.toml")
MAX_ARTICLES_PER_RUN = 3
MAX_ARTICLE_READ_RETRIES_PER_RUN = 1
MAX_ANALYSIS_RETRIES_PER_RUN = 1
MAX_NOTIFICATION_ATTEMPTS_PER_RUN = 3
ReadOneStatus = Literal["stored", "already_stored"]
SummarizeOneStatus = Literal["summarized", "already_summarized"]
NotifyOneStatus = Literal[
    "no_due_notifications",
    "sent",
    "retry_scheduled",
    "dead",
]
TelegramFeedbackOnceStatus = Literal["completed", "no_telegram_targets"]
RunOnceStatus = Literal[
    "completed",
    "completed_with_errors",
    "no_work",
    "already_running",
    "failed",
]
RunArticleStatus = Literal["completed", "failed"]
RunArticleStage = Literal["read", "analyze"]
ArticleReadFailureStatus = Literal["retry_wait", "dead"]
RunDiscoveryStatus = Literal["fetched", "modified", "not_modified"]
RunLogSink = Callable[[dict[str, object]], None]


class ArticleSelectionError(ValueError):
    """Raised when an explicit discovery identity cannot select an article."""


class VisibleBrowserPermissionError(RuntimeError):
    """Raised when a command would open a visible browser without consent."""


@dataclass(frozen=True)
class ReadOneResult:
    """Outcome of reading or re-selecting one complete stored article."""

    status: ReadOneStatus
    article: StoredArticle


@dataclass(frozen=True)
class SummarizeOneResult:
    """Outcome of analyzing or re-selecting one stored article."""

    status: SummarizeOneStatus
    article: StoredArticle
    event_resolution: StoredEventResolution


@dataclass(frozen=True)
class NotifyOneResult:
    """Outcome of attempting at most one due notification delivery."""

    status: NotifyOneStatus
    delivery_id: int | None = None
    event_id: int | None = None
    target_id: str | None = None
    attempt: int | None = None
    retry_at: datetime | None = None
    external_receipt_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class TelegramFeedbackOnceResult:
    """Outcome of polling Telegram reactions and refreshing memory."""

    status: TelegramFeedbackOnceStatus
    targets: tuple[TelegramFeedbackPollResult, ...] = ()
    memory: MemoryRefreshResult | None = None


@dataclass(frozen=True)
class RunArticleResult:
    """One bounded article attempt within a run-once execution."""

    identity: str
    title: str
    url: str
    status: RunArticleStatus
    guid: str | None = None
    read_status: ReadOneStatus | None = None
    analysis_status: SummarizeOneStatus | None = None
    event_resolution: StoredEventResolution | None = None
    failed_stage: RunArticleStage | None = None
    error_type: str | None = None
    error_message: str | None = None
    read_failure_status: ArticleReadFailureStatus | None = None
    read_failure_reason: str | None = None
    read_failure_attempts: int | None = None
    read_retry_at: str | None = None


@dataclass(frozen=True)
class RunOnceResult:
    """Structured, scheduler-friendly result of one bounded workflow run."""

    status: RunOnceStatus
    run_id: str
    started_at: datetime
    finished_at: datetime
    articles: tuple[RunArticleResult, ...] = ()
    notifications: tuple[NotifyOneResult, ...] = ()
    discovery_status: RunDiscoveryStatus | None = None
    discovered_item_count: int = 0
    selected_read_retry_count: int = 0
    skipped_discoveries: tuple[DiscoveredItem, ...] = ()
    discovery_checkpoint_error_type: str | None = None
    discovery_checkpoint_error_message: str | None = None
    notification_error_type: str | None = None
    notification_error_message: str | None = None
    run_error_stage: str | None = None
    run_error_type: str | None = None
    run_error_message: str | None = None


@dataclass(frozen=True)
class _RunWorkItem:
    identity: str
    title: str
    url: str
    stored_article: StoredArticle | None = None


@dataclass(frozen=True)
class _RunDiscoverySnapshot:
    items: tuple[DiscoveredItem, ...]
    status: RunDiscoveryStatus
    etag: str | None = None
    last_modified: str | None = None
    preserve_order: bool = False


def discover_once(
    config_path: str | Path,
    *,
    client: httpx.Client | None = None,
    allow_visible_browser: bool = False,
) -> PendingResult:
    """Return discovered items whose full article body is not stored yet."""

    config = load_config(config_path)
    items = _fetch_discovery_items(
        config,
        client=client,
        allow_visible_browser=allow_visible_browser,
    )
    return find_pending_articles(
        config.database_file,
        config.source_id,
        items,
    )


def read_one(
    config_path: str | Path,
    guid: str,
    *,
    client: httpx.Client | None = None,
    allow_visible_browser: bool = False,
) -> ReadOneResult:
    """Read and store one article selected by its discovery identity."""

    config = load_config(config_path)
    return _read_one_with_config(
        config,
        guid,
        client=client,
        allow_visible_browser=allow_visible_browser,
    )


def _read_one_with_config(
    config: RssSourceConfig,
    guid: str,
    *,
    client: httpx.Client | None = None,
    discovered_items: Sequence[DiscoveredItem] | None = None,
    allow_visible_browser: bool = False,
) -> ReadOneResult:
    """Read one article using one already validated configuration snapshot."""

    selected_guid = guid.strip()
    if not selected_guid:
        raise ArticleSelectionError("Article discovery identity cannot be empty")

    existing = _get_article_by_discovery_identity(
        config,
        selected_guid,
    )
    if existing is not None:
        return ReadOneResult(status="already_stored", article=existing)

    items = (
        tuple(discovered_items)
        if discovered_items is not None
        else _fetch_discovery_items(
            config,
            client=client,
            allow_visible_browser=allow_visible_browser,
        )
    )
    matches = [item for item in items if item.guid == selected_guid]
    if len(matches) != 1:
        raise ArticleSelectionError(
            "Discovery source must contain exactly one article with identity "
            f"{selected_guid!r}"
        )
    item = matches[0]

    existing = get_article_by_url(
        config.database_file,
        config.source_id,
        str(item.url),
    )
    if existing is not None:
        return ReadOneResult(status="already_stored", article=existing)

    _require_visible_browser_permission(
        config,
        allow_visible_browser=allow_visible_browser,
    )

    body = asyncio.run(
        read_article_body(
            item,
            browser_mode=config.browser_mode,
            browser_bundle_id=config.browser_bundle_id,
            browser_executable=config.browser_executable,
            browser_profile_directory=config.browser_profile_directory,
            timeout_seconds=config.timeout_seconds,
            article_body_selector=config.article_body_selector,
            article_access_denied_selector=config.article_access_denied_selector,
            article_access_denied_phrases=config.article_access_denied_phrases,
            browser_launch_mode=config.browser_launch_mode,
        )
    )
    article = StoredArticle(
        source_id=config.source_id,
        guid=item.guid,
        published_at=item.published_at,
        title=item.title,
        url=item.url,
        body=body,
    )
    inserted = store_article(config.database_file, article)
    if inserted:
        return ReadOneResult(status="stored", article=article)

    stored = _get_article_by_discovery_identity(config, item.guid)
    if stored is None:
        raise StorageError("Article insert did not produce a readable stored row")
    return ReadOneResult(status="already_stored", article=stored)


def summarize_one(
    config_path: str | Path,
    guid: str,
) -> SummarizeOneResult:
    """Translate, summarize, and resolve one stored article in one Codex call."""

    config = load_config(config_path)
    return _summarize_one_with_config(config, guid)


def _summarize_one_with_config(
    config: RssSourceConfig,
    guid: str,
) -> SummarizeOneResult:
    """Analyze one stored article using one validated configuration snapshot."""

    selected_guid = guid.strip()
    if not selected_guid:
        raise ArticleSelectionError("Article discovery identity cannot be empty")

    article = _get_article_by_discovery_identity(config, selected_guid)
    if article is None:
        raise ArticleSelectionError(
            f"Stored article not found for identity {selected_guid!r}"
        )
    stored_guid = article.guid
    existing_resolution = get_article_event_resolution(
        config.database_file,
        config.source_id,
        stored_guid,
    )
    if (
        article.translated_title is not None
        and article.summary is not None
        and existing_resolution is not None
    ):
        return SummarizeOneResult(
            status="already_summarized",
            article=article,
            event_resolution=existing_resolution,
        )

    candidates = find_event_candidates(
        config.database_file,
        article.title,
        article.body,
    )

    generated = summarize_article(
        article.title,
        article.body,
        event_candidates=[candidate.to_prompt_dict() for candidate in candidates],
        timeout_seconds=config.codex_timeout_seconds,
    )
    notification_targets = ()
    if config.notifications is not None:
        notification_targets = tuple(
            NotificationTarget(target_id=target.id, adapter=target.adapter)
            for target in config.notifications.targets
        )
    store_result = store_article_analysis(
        config.database_file,
        config.source_id,
        stored_guid,
        translated_title=generated.translated_title,
        summary=generated.summary,
        decision=generated.event_resolution.decision,
        event_id=generated.event_resolution.event_id,
        notification_targets=notification_targets,
    )
    stored = get_article(
        config.database_file,
        config.source_id,
        stored_guid,
    )
    if stored is None or stored.translated_title is None or stored.summary is None:
        raise StorageError("Article analysis did not produce a readable row")
    status: SummarizeOneStatus = (
        "summarized" if store_result.inserted else "already_summarized"
    )
    return SummarizeOneResult(
        status=status,
        article=stored,
        event_resolution=store_result.resolution,
    )


def notify_one(
    config_path: str | Path,
    *,
    client: httpx.Client | None = None,
    environ: Mapping[str, str] | None = None,
    now: datetime | None = None,
) -> NotifyOneResult:
    """Attempt one due configured delivery and persist its outcome."""

    config = load_config(config_path)
    return _notify_one_with_config(
        config,
        client=client,
        environ=environ,
        now=now,
    )


def _notify_one_with_config(
    config: RssSourceConfig,
    *,
    client: httpx.Client | None = None,
    environ: Mapping[str, str] | None = None,
    now: datetime | None = None,
) -> NotifyOneResult:
    """Attempt one delivery using one validated configuration snapshot."""

    settings = config.notifications
    if settings is None:
        return NotifyOneResult(status="no_due_notifications")

    targets_by_id = {target.id: target for target in settings.targets}
    claimed = claim_notification_delivery(
        config.database_file,
        target_ids=targets_by_id,
        lease_seconds=settings.lease_seconds,
        now=now,
    )
    if claimed is None:
        return NotifyOneResult(status="no_due_notifications")

    target = targets_by_id[claimed.target.target_id]
    if target.adapter != claimed.target.adapter:
        return _finish_failed_notification(
            config.database_file,
            claimed,
            error=NotificationSendError(
                "Notification target adapter changed",
                retryable=False,
            ),
            max_attempts=settings.max_attempts,
            retry_base_seconds=settings.retry_base_seconds,
            retry_max_seconds=settings.retry_max_seconds,
            now=now,
        )

    try:
        receipt_ids = TelegramNotifier(
            target,
            client=client,
            environ=environ,
        ).send(
            claimed.translated_title,
            claimed.summary,
            claimed.article_url,
            heartbeat=lambda: renew_notification_delivery_lease(
                config.database_file,
                claimed.delivery_id,
                claimed.claim_token,
                lease_seconds=settings.lease_seconds,
                now=now,
            ),
        )
    except NotificationSendError as exc:
        return _finish_failed_notification(
            config.database_file,
            claimed,
            error=exc,
            max_attempts=settings.max_attempts,
            retry_base_seconds=settings.retry_base_seconds,
            retry_max_seconds=settings.retry_max_seconds,
            now=now,
        )

    receipt_snapshot = json.dumps(receipt_ids, separators=(",", ":"))
    if not ack_notification_delivery(
        config.database_file,
        claimed.delivery_id,
        claimed.claim_token,
        external_receipt_id=receipt_snapshot,
        now=now,
    ):
        raise StorageError(
            "Notification delivery claim expired before acknowledgement"
        )
    return NotifyOneResult(
        status="sent",
        delivery_id=claimed.delivery_id,
        event_id=claimed.event_id,
        target_id=claimed.target.target_id,
        attempt=claimed.attempt,
        external_receipt_ids=receipt_ids,
    )


def telegram_feedback_once(
    config_path: str | Path,
    *,
    client: httpx.Client | None = None,
    environ: Mapping[str, str] | None = None,
    now: datetime | None = None,
) -> TelegramFeedbackOnceResult:
    """Poll Telegram reactions once and refresh shared preference memory."""

    config = load_config(config_path)
    settings = config.notifications
    if settings is None:
        memory = refresh_memory_from_feedback(
            config.database_file,
            config.memory_directory,
            now=now,
        )
        return TelegramFeedbackOnceResult(
            status="no_telegram_targets",
            memory=memory,
        )

    results = tuple(
        poll_telegram_feedback(
            target,
            config.database_file,
            client=client,
            environ=environ,
        )
        for target in settings.targets
        if target.adapter == "telegram"
    )
    memory = refresh_memory_from_feedback(
        config.database_file,
        config.memory_directory,
        now=now,
    )
    return TelegramFeedbackOnceResult(
        status="completed" if results else "no_telegram_targets",
        targets=results,
        memory=memory,
    )


def memory_refresh_once(
    config_path: str | Path,
    *,
    now: datetime | None = None,
) -> MemoryRefreshResult:
    """Refresh shared MEMORY.md from current persisted feedback state."""

    config = load_config(config_path)
    return refresh_memory_from_feedback(
        config.database_file,
        config.memory_directory,
        now=now,
    )


def run_once(
    config_path: str | Path,
    *,
    client: httpx.Client | None = None,
    environ: Mapping[str, str] | None = None,
    now: datetime | None = None,
    lock_file: Path | None = None,
    log_sink: RunLogSink | None = None,
    run_id: str | None = None,
    allow_visible_browser: bool = False,
) -> RunOnceResult:
    """Run a locked, bounded discovery-to-notification workflow once."""

    started_at = _run_now()
    selected_run_id = (run_id or uuid4().hex).strip()
    if not selected_run_id:
        raise ValueError("Run ID cannot be empty")

    try:
        config = load_config(config_path)
        notification_now = _notification_now(now) if now is not None else None
    except (ConfigLoadError, StorageError) as exc:
        _emit_run_log(
            log_sink,
            selected_run_id,
            "run_failed",
            stage="configuration",
            error_type=type(exc).__name__,
            error_message=_safe_error_message(exc),
        )
        return _failed_run_once_result(
            selected_run_id,
            started_at,
            stage="configuration",
            error=exc,
        )

    selected_lock_file = lock_file or Path(
        f"{config.database_file}.run-once.lock"
    )

    try:
        with RunLock(selected_lock_file):
            try:
                return _run_once_locked(
                    config,
                    started_at=started_at,
                    run_id=selected_run_id,
                    client=client,
                    environ=environ,
                    notification_now=notification_now,
                    log_sink=log_sink,
                    allow_visible_browser=allow_visible_browser,
                )
            except (
                BrowserDiscoveryError,
                CandidateSelectorError,
                MemoryError,
                RssFetchError,
                RssParseError,
                SitemapFetchError,
                SitemapParseError,
                StorageError,
                VisibleBrowserPermissionError,
            ) as exc:
                _emit_run_log(
                    log_sink,
                    selected_run_id,
                    "run_failed",
                    stage="workflow_setup",
                    error_type=type(exc).__name__,
                    error_message=_safe_error_message(exc),
                )
                return _failed_run_once_result(
                    selected_run_id,
                    started_at,
                    stage="workflow_setup",
                    error=exc,
                )
    except RunLockBusyError:
        finished_at = _run_now()
        _emit_run_log(
            log_sink,
            selected_run_id,
            "run_skipped",
            status="already_running",
        )
        return RunOnceResult(
            status="already_running",
            run_id=selected_run_id,
            started_at=started_at,
            finished_at=finished_at,
        )
    except RunLockError as exc:
        _emit_run_log(
            log_sink,
            selected_run_id,
            "run_failed",
            stage="lock",
            error_type=type(exc).__name__,
            error_message=_safe_error_message(exc),
        )
        return _failed_run_once_result(
            selected_run_id,
            started_at,
            stage="lock",
            error=exc,
        )


def _failed_run_once_result(
    run_id: str,
    started_at: datetime,
    *,
    stage: str,
    error: Exception,
) -> RunOnceResult:
    return RunOnceResult(
        status="failed",
        run_id=run_id,
        started_at=started_at,
        finished_at=_run_now(),
        run_error_stage=stage,
        run_error_type=type(error).__name__,
        run_error_message=_safe_error_message(error),
    )


def _run_once_locked(
    config: RssSourceConfig,
    *,
    started_at: datetime,
    run_id: str,
    client: httpx.Client | None,
    environ: Mapping[str, str] | None,
    notification_now: datetime | None,
    log_sink: RunLogSink | None,
    allow_visible_browser: bool,
) -> RunOnceResult:
    _require_visible_browser_permission(
        config,
        allow_visible_browser=allow_visible_browser,
    )
    _emit_run_log(
        log_sink,
        run_id,
        "run_started",
        max_fresh_articles=MAX_ARTICLES_PER_RUN,
        max_article_read_retries=MAX_ARTICLE_READ_RETRIES_PER_RUN,
        max_analysis_retries=MAX_ANALYSIS_RETRIES_PER_RUN,
        max_notification_attempts=MAX_NOTIFICATION_ATTEMPTS_PER_RUN,
    )

    discovery = _fetch_run_discovery_snapshot(
        config,
        client=client,
        now=started_at,
        allow_visible_browser=allow_visible_browser,
    )
    _emit_run_log(
        log_sink,
        run_id,
        "discovery_checked",
        status=discovery.status,
        item_count=len(discovery.items),
    )
    read_retries = find_article_read_failures_due(
        config.database_file,
        config.source_id,
        limit=MAX_ARTICLE_READ_RETRIES_PER_RUN,
        now=started_at,
    )
    selection = select_discoveries_for_run(
        config.database_file,
        config.source_id,
        discovery.items,
        limit=MAX_ARTICLES_PER_RUN,
        now=started_at,
        preserve_order=discovery.preserve_order,
    )
    analysis_retries = find_articles_due_analysis_retry(
        config.database_file,
        config.source_id,
        limit=MAX_ANALYSIS_RETRIES_PER_RUN,
    )
    selected_fresh_identities = {
        item.guid for item in selection.selected_items
    }
    selected_fresh_urls = {
        str(item.url) for item in selection.selected_items
    }
    selected_read_retries = tuple(
        failure
        for failure in read_retries
        if failure.guid not in selected_fresh_identities
        and failure.url not in selected_fresh_urls
    )[:MAX_ARTICLE_READ_RETRIES_PER_RUN]
    work_items = _select_run_work_items(
        analysis_retries,
        selection.selected_items,
        read_retries=selected_read_retries,
        preserve_discovery_order=discovery.preserve_order,
    )
    _emit_run_log(
        log_sink,
        run_id,
        "work_selected",
        analysis_retry_count=len(analysis_retries),
        read_retry_count=len(selected_read_retries),
        discovery_selected_count=len(selection.selected_items),
        discovery_skipped_count=len(selection.skipped_items),
        selected_article_count=len(work_items),
    )
    if selection.skipped_items:
        _emit_run_log(
            log_sink,
            run_id,
            "discovery_overflow_skipped",
            count=len(selection.skipped_items),
            items=[
                _discovery_item_payload(item)
                for item in selection.skipped_items
            ],
        )

    run_discovered_items = selection.selected_items + tuple(
        _read_failure_discovered_item(failure)
        for failure in selected_read_retries
    )
    article_results = tuple(
        _process_run_article(
            config,
            work_item,
            discovered_items=run_discovered_items,
            client=client,
            log_sink=log_sink,
            run_id=run_id,
            allow_visible_browser=allow_visible_browser,
        )
        for work_item in work_items
    )
    (
        discovery_checkpoint_error_type,
        discovery_checkpoint_error_message,
    ) = _checkpoint_run_discovery(
        config,
        discovery,
        article_results,
        log_sink=log_sink,
        run_id=run_id,
    )
    (
        notification_results,
        notification_error_type,
        notification_error_message,
    ) = _drain_run_notifications(
        config,
        client=client,
        environ=environ,
        now=notification_now,
        log_sink=log_sink,
        run_id=run_id,
    )

    has_errors = (
        any(result.status == "failed" for result in article_results)
        or any(
            result.status in {"retry_scheduled", "dead"}
            for result in notification_results
        )
        or discovery_checkpoint_error_type is not None
        or notification_error_type is not None
    )
    if has_errors:
        status: RunOnceStatus = "completed_with_errors"
    elif not article_results and not notification_results:
        status = "no_work"
    else:
        status = "completed"

    finished_at = _run_now()
    _emit_run_log(
        log_sink,
        run_id,
        "run_finished",
        status=status,
        article_count=len(article_results),
        failed_article_count=sum(
            result.status == "failed" for result in article_results
        ),
        discovery_skipped_count=len(selection.skipped_items),
        notification_attempt_count=len(notification_results),
    )
    return RunOnceResult(
        status=status,
        run_id=run_id,
        started_at=started_at,
        finished_at=finished_at,
        articles=article_results,
        notifications=notification_results,
        discovery_status=discovery.status,
        discovered_item_count=len(discovery.items),
        selected_read_retry_count=len(selected_read_retries),
        skipped_discoveries=selection.skipped_items,
        discovery_checkpoint_error_type=discovery_checkpoint_error_type,
        discovery_checkpoint_error_message=discovery_checkpoint_error_message,
        notification_error_type=notification_error_type,
        notification_error_message=notification_error_message,
    )


def _select_run_work_items(
    analysis_backlog: Sequence[StoredArticle],
    discovery_selected: Sequence[DiscoveredItem],
    *,
    read_retries: Sequence[ArticleReadFailure] = (),
    preserve_discovery_order: bool = False,
) -> tuple[_RunWorkItem, ...]:
    work_items: list[_RunWorkItem] = []
    selected_identities: set[str] = set()
    selected_urls: set[str] = set()

    if discovery_selected:
        selected_items = tuple(discovery_selected)
        if not preserve_discovery_order:
            identity_sorted = sorted(selected_items, key=lambda item: item.guid)
            selected_items = tuple(
                sorted(
                    identity_sorted,
                    key=lambda item: item.published_at
                    or datetime.min.replace(tzinfo=UTC),
                    reverse=True,
                )
            )
        for item in selected_items[:MAX_ARTICLES_PER_RUN]:
            work_items.append(
                _RunWorkItem(
                    identity=item.guid,
                    title=item.title,
                    url=str(item.url),
                )
            )
            selected_identities.add(item.guid)
            selected_urls.add(str(item.url))

    selected_read_retry_count = 0
    for failure in read_retries:
        if selected_read_retry_count >= MAX_ARTICLE_READ_RETRIES_PER_RUN:
            break
        if failure.guid in selected_identities or failure.url in selected_urls:
            continue
        work_items.append(
            _RunWorkItem(
                identity=failure.guid,
                title=failure.title,
                url=failure.url,
            )
        )
        selected_identities.add(failure.guid)
        selected_urls.add(failure.url)
        selected_read_retry_count += 1

    selected_retry_count = 0
    for article in analysis_backlog:
        if selected_retry_count >= MAX_ANALYSIS_RETRIES_PER_RUN:
            break
        if article.guid in selected_identities or str(article.url) in selected_urls:
            continue
        work_items.append(
            _RunWorkItem(
                identity=article.guid,
                title=article.title,
                url=str(article.url),
                stored_article=article,
            )
        )
        selected_identities.add(article.guid)
        selected_urls.add(str(article.url))
        selected_retry_count += 1

    return tuple(work_items)


def _read_failure_discovered_item(
    failure: ArticleReadFailure,
) -> DiscoveredItem:
    return DiscoveredItem(
        guid=failure.guid,
        title=failure.title,
        url=failure.url,
        published_at=None,
    )


def _checkpoint_run_discovery(
    config: RssSourceConfig,
    discovery: _RunDiscoverySnapshot,
    article_results: Sequence[RunArticleResult],
    *,
    log_sink: RunLogSink | None,
    run_id: str,
) -> tuple[str | None, str | None]:
    if discovery.status != "modified":
        return None, None
    if discovery.etag is None and discovery.last_modified is None:
        return None, None
    read_failure_count = sum(
        result.failed_stage == "read" for result in article_results
    )

    try:
        store_discovery_http_validators(
            config.database_file,
            config.source_id,
            discovery.etag,
            discovery.last_modified,
            now=_run_now(),
        )
    except StorageError as exc:
        message = _safe_error_message(exc)
        _emit_run_log(
            log_sink,
            run_id,
            "discovery_checkpoint_failed",
            error_type=type(exc).__name__,
            error_message=message,
        )
        return type(exc).__name__, message

    _emit_run_log(
        log_sink,
        run_id,
        "discovery_checkpoint_stored",
        read_failure_count=read_failure_count,
        read_retry="persistent_failure_ledger" if read_failure_count else "none",
    )
    return None, None


def _process_run_article(
    config: RssSourceConfig,
    work_item: _RunWorkItem,
    *,
    discovered_items: Sequence[DiscoveredItem],
    client: httpx.Client | None,
    log_sink: RunLogSink | None,
    run_id: str,
    allow_visible_browser: bool,
) -> RunArticleResult:
    _emit_run_log(
        log_sink,
        run_id,
        "article_started",
        identity=work_item.identity,
        title=work_item.title,
        url=work_item.url,
    )
    article = work_item.stored_article
    read_status: ReadOneStatus = "already_stored"
    if article is None:
        try:
            read_result = _read_one_with_config(
                config,
                work_item.identity,
                client=client,
                discovered_items=discovered_items,
                allow_visible_browser=allow_visible_browser,
            )
            article = read_result.article
            read_status = read_result.status
        except ReaderError as exc:
            read_failure: ArticleReadFailure | None = None
            try:
                read_failure = record_article_read_failure(
                    config.database_file,
                    config.source_id,
                    work_item.identity,
                    work_item.url,
                    work_item.title,
                    reason=exc.reason,
                    error=_safe_error_message(exc),
                    retryable=exc.retryable,
                    max_attempts=config.article_read_retry_max_attempts,
                    base_retry_seconds=config.article_read_retry_base_seconds,
                    max_retry_seconds=config.article_read_retry_max_seconds,
                    now=_run_now(),
                )
            except StorageError as ledger_exc:
                _emit_run_log(
                    log_sink,
                    run_id,
                    "article_read_failure_record_failed",
                    identity=work_item.identity,
                    error_type=type(ledger_exc).__name__,
                    error_message=_safe_error_message(ledger_exc),
                )
            else:
                _emit_run_log(
                    log_sink,
                    run_id,
                    "article_read_failure_recorded",
                    identity=work_item.identity,
                    reason=read_failure.reason,
                    status=read_failure.status,
                    attempts=read_failure.attempts,
                    retry_at=(
                        read_failure.retry_at
                        if read_failure.retry_at is not None
                        else None
                    ),
                )
            return _failed_run_article(
                work_item,
                stage="read",
                error=exc,
                read_failure=read_failure,
                log_sink=log_sink,
                run_id=run_id,
            )
        except (ArticleSelectionError, StorageError) as exc:
            return _failed_run_article(
                work_item,
                stage="read",
                error=exc,
                log_sink=log_sink,
                run_id=run_id,
            )

    try:
        analysis_result = _summarize_one_with_config(config, article.guid)
    except (ArticleSelectionError, SummarizerError, StorageError) as exc:
        try:
            attempts = schedule_article_analysis_retry(
                config.database_file,
                article.source_id,
                article.guid,
                now=_run_now(),
            )
        except StorageError as retry_exc:
            _emit_run_log(
                log_sink,
                run_id,
                "analysis_retry_schedule_failed",
                identity=work_item.identity,
                guid=article.guid,
                error_type=type(retry_exc).__name__,
                error_message=_safe_error_message(retry_exc),
            )
        else:
            _emit_run_log(
                log_sink,
                run_id,
                "analysis_retry_scheduled",
                identity=work_item.identity,
                guid=article.guid,
                attempts=attempts,
            )
        return _failed_run_article(
            work_item,
            stage="analyze",
            error=exc,
            guid=article.guid,
            read_status=read_status,
            log_sink=log_sink,
            run_id=run_id,
        )

    _emit_run_log(
        log_sink,
        run_id,
        "article_completed",
        identity=work_item.identity,
        guid=analysis_result.article.guid,
        decision=analysis_result.event_resolution.decision,
        event_id=analysis_result.event_resolution.event_id,
    )
    return RunArticleResult(
        identity=work_item.identity,
        title=work_item.title,
        url=work_item.url,
        status="completed",
        guid=analysis_result.article.guid,
        read_status=read_status,
        analysis_status=analysis_result.status,
        event_resolution=analysis_result.event_resolution,
    )


def _failed_run_article(
    work_item: _RunWorkItem,
    *,
    stage: RunArticleStage,
    error: Exception,
    log_sink: RunLogSink | None,
    run_id: str,
    guid: str | None = None,
    read_status: ReadOneStatus | None = None,
    read_failure: ArticleReadFailure | None = None,
) -> RunArticleResult:
    message = _safe_error_message(error)
    _emit_run_log(
        log_sink,
        run_id,
        "article_failed",
        identity=work_item.identity,
        stage=stage,
        error_type=type(error).__name__,
        error_message=message,
    )
    return RunArticleResult(
        identity=work_item.identity,
        title=work_item.title,
        url=work_item.url,
        status="failed",
        guid=guid,
        read_status=read_status,
        failed_stage=stage,
        error_type=type(error).__name__,
        error_message=message,
        read_failure_status=(
            read_failure.status if read_failure is not None else None
        ),
        read_failure_reason=(
            read_failure.reason if read_failure is not None else None
        ),
        read_failure_attempts=(
            read_failure.attempts if read_failure is not None else None
        ),
        read_retry_at=(
            read_failure.retry_at if read_failure is not None else None
        ),
    )


def _drain_run_notifications(
    config: RssSourceConfig,
    *,
    client: httpx.Client | None,
    environ: Mapping[str, str] | None,
    now: datetime | None,
    log_sink: RunLogSink | None,
    run_id: str,
) -> tuple[tuple[NotifyOneResult, ...], str | None, str | None]:
    results: list[NotifyOneResult] = []
    for _ in range(MAX_NOTIFICATION_ATTEMPTS_PER_RUN):
        try:
            result = _notify_one_with_config(
                config,
                client=client,
                environ=environ,
                now=now,
            )
        except StorageError as exc:
            message = _safe_error_message(exc)
            _emit_run_log(
                log_sink,
                run_id,
                "notification_failed",
                error_type=type(exc).__name__,
                error_message=message,
            )
            return tuple(results), type(exc).__name__, message

        if result.status == "no_due_notifications":
            break
        results.append(result)
        _emit_run_log(
            log_sink,
            run_id,
            "notification_finished",
            status=result.status,
            delivery_id=result.delivery_id,
            event_id=result.event_id,
            target_id=result.target_id,
            attempt=result.attempt,
        )

    return tuple(results), None, None


def _safe_error_message(error: Exception) -> str:
    message = " ".join(str(error).split()) or type(error).__name__
    return message[:500]


def _emit_run_log(
    sink: RunLogSink | None,
    run_id: str,
    event: str,
    **fields: object,
) -> None:
    if sink is None:
        return
    sink(
        {
            "timestamp": _run_timestamp(_run_now()),
            "run_id": run_id,
            "event": event,
            **fields,
        }
    )


def _run_now() -> datetime:
    return datetime.now(UTC)


def _run_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _finish_failed_notification(
    database_file: str | Path,
    claimed: ClaimedNotificationDelivery,
    *,
    error: NotificationSendError,
    max_attempts: int,
    retry_base_seconds: float,
    retry_max_seconds: float,
    now: datetime | None,
) -> NotifyOneResult:
    failure_time = _notification_now(now)
    retry_at: datetime | None = None
    if error.retryable and not error.receipt_ids and claimed.attempt < max_attempts:
        exponential_delay = retry_base_seconds * (2 ** (claimed.attempt - 1))
        delay_seconds = min(exponential_delay, retry_max_seconds)
        if error.retry_after_seconds is not None:
            delay_seconds = max(delay_seconds, error.retry_after_seconds)
        retry_at = failure_time + timedelta(seconds=delay_seconds)

    partial_receipt_snapshot = None
    if error.receipt_ids:
        partial_receipt_snapshot = json.dumps(
            error.receipt_ids,
            separators=(",", ":"),
        )
    if not nack_notification_delivery(
        database_file,
        claimed.delivery_id,
        claimed.claim_token,
        error=str(error),
        retry_at=retry_at,
        external_receipt_id=partial_receipt_snapshot,
        now=failure_time,
    ):
        raise StorageError("Notification delivery claim expired before failure update")

    return NotifyOneResult(
        status="retry_scheduled" if retry_at is not None else "dead",
        delivery_id=claimed.delivery_id,
        event_id=claimed.event_id,
        target_id=claimed.target.target_id,
        attempt=claimed.attempt,
        retry_at=retry_at,
        external_receipt_ids=error.receipt_ids,
    )


def _notification_now(value: datetime | None) -> datetime:
    selected = datetime.now(UTC) if value is None else value
    if selected.tzinfo is None or selected.utcoffset() is None:
        raise StorageError("Notification time must include a timezone")
    return selected.astimezone(UTC)


def _fetch_run_discovery_snapshot(
    config: RssSourceConfig,
    *,
    client: httpx.Client | None = None,
    now: datetime | None = None,
    allow_visible_browser: bool = False,
) -> _RunDiscoverySnapshot:
    """Fetch one run snapshot, using persisted validators for News Sitemap."""

    if config.browser_section_urls:
        return _RunDiscoverySnapshot(
            items=_fetch_browser_section_discovery_items(
                config,
                now=now,
                allow_visible_browser=allow_visible_browser,
            ),
            status="fetched",
            preserve_order=True,
        )

    if config.news_sitemap_url is None:
        return _RunDiscoverySnapshot(
            items=_fetch_discovery_items(config, client=client),
            status="fetched",
        )

    validators = get_discovery_http_validators(
        config.database_file,
        config.source_id,
    )
    fetched = fetch_sitemap_conditionally(
        config,
        etag=validators.etag if validators is not None else None,
        last_modified=(
            validators.last_modified if validators is not None else None
        ),
        client=client,
    )
    if fetched.status == "not_modified":
        return _RunDiscoverySnapshot(
            items=(),
            status="not_modified",
            etag=fetched.etag,
            last_modified=fetched.last_modified,
        )
    if fetched.document is None:
        raise SitemapParseError("Modified News Sitemap response has no document")

    return _RunDiscoverySnapshot(
        items=parse_sitemap(
            fetched.document,
            required_keyword=config.news_sitemap_keyword,
            excluded_keyword=config.news_sitemap_excluded_keyword,
        ),
        status="modified",
        etag=fetched.etag,
        last_modified=fetched.last_modified,
    )


def _fetch_discovery_items(
    config: RssSourceConfig,
    *,
    client: httpx.Client | None = None,
    allow_visible_browser: bool = False,
) -> tuple[DiscoveredItem, ...]:
    """Use browser sections or News Sitemap when configured; otherwise RSS."""

    if config.browser_section_urls:
        return _fetch_browser_section_discovery_items(
            config,
            allow_visible_browser=allow_visible_browser,
        )
    if config.news_sitemap_url is not None:
        return fetch_and_parse_sitemap(config, client=client)
    return fetch_and_parse_rss(config, client=client)


def _fetch_browser_section_discovery_items(
    config: RssSourceConfig,
    *,
    now: datetime | None = None,
    allow_visible_browser: bool = False,
) -> tuple[DiscoveredItem, ...]:
    _require_visible_browser_permission(
        config,
        allow_visible_browser=allow_visible_browser,
    )
    if config.browser_executable is None or config.browser_profile_directory is None:
        raise BrowserDiscoveryError(
            "Browser section discovery requires dedicated Chrome settings"
        )
    selection_profile = config.browser_candidate_selection_profile
    if selection_profile is None:
        raise CandidateSelectorError("Browser candidate selection profile is missing")

    collected = asyncio.run(
        collect_browser_section_candidates(
            tuple(str(url) for url in config.browser_section_urls),
            executable=config.browser_executable,
            profile_directory=config.browser_profile_directory,
            timeout_seconds=config.timeout_seconds,
            allowed_hosts=config.browser_article_allowed_hosts,
            allowed_path_prefixes=config.browser_article_allowed_path_prefixes,
            excluded_path_prefixes=config.browser_article_excluded_path_prefixes,
            total_limit=config.browser_candidate_collect_limit,
            launch_mode=config.browser_launch_mode,
        )
    )
    prompt_candidates = select_browser_candidates_for_prompt(
        config.database_file,
        config.source_id,
        collected,
        limit=config.browser_candidate_prompt_limit,
        now=now,
    )
    if not prompt_candidates:
        return ()

    selected_candidates = select_article_candidates(
        prompt_candidates,
        selection_profile=selection_profile,
        memory_context=load_memory_context(
            config.memory_directory,
            max_characters=config.memory_context_max_characters,
        ),
        selection_limit=config.browser_candidate_selection_limit,
        timeout_seconds=config.codex_timeout_seconds,
    )
    record_browser_candidate_selection(
        config.database_file,
        config.source_id,
        prompt_candidates,
        selected_candidates,
        rejected_cooldown_hours=config.browser_candidate_rejected_cooldown_hours,
        now=now,
    )
    read_limit = min(config.browser_candidate_read_limit, MAX_ARTICLES_PER_RUN)
    return tuple(
        candidate.to_discovered_item()
        for candidate in selected_candidates[:read_limit]
    )


def _require_visible_browser_permission(
    config: RssSourceConfig,
    *,
    allow_visible_browser: bool,
) -> None:
    if config.browser_launch_mode != "regular_cdp" or allow_visible_browser:
        return
    raise VisibleBrowserPermissionError(
        "This source requires a visible Chrome window; rerun manually with "
        "--allow-visible-browser"
    )


def _get_article_by_discovery_identity(
    config: RssSourceConfig,
    identity: str,
) -> StoredArticle | None:
    """Resolve either a stored GUID or a canonical URL identity."""

    by_guid = get_article(config.database_file, config.source_id, identity)
    if by_guid is not None:
        return by_guid
    return get_article_by_url(config.database_file, config.source_id, identity)


def main(argv: Sequence[str] | None = None) -> int:
    """Execute the CLI and translate known workflow failures to an exit code."""

    arguments = _parser().parse_args(argv)
    exit_code = 0
    should_print_payload = True

    try:
        if arguments.command == "discover":
            result = discover_once(
                arguments.config,
                allow_visible_browser=arguments.allow_visible_browser,
            )
            payload = _result_payload(result)
        elif arguments.command == "read-one":
            result = read_one(
                arguments.config,
                arguments.guid,
                allow_visible_browser=arguments.allow_visible_browser,
            )
            payload = _read_one_payload(result)
        elif arguments.command == "summarize-one":
            result = summarize_one(arguments.config, arguments.guid)
            payload = _summarize_one_payload(result)
        elif arguments.command == "notify-one":
            result = notify_one(arguments.config)
            payload = _notify_one_payload(result)
        elif arguments.command == "telegram-feedback-once":
            result = telegram_feedback_once(arguments.config)
            payload = _telegram_feedback_once_payload(result)
            should_print_payload = not (
                arguments.quiet_when_idle
                and _telegram_feedback_result_is_idle(result)
            )
        elif arguments.command == "memory-refresh-once":
            result = memory_refresh_once(arguments.config)
            payload = _memory_refresh_payload(result)
        elif arguments.command == "watchdog-once":
            result = watchdog_once(
                arguments.config,
                service_label=arguments.service_label,
                run_log_file=arguments.run_log,
                state_file=arguments.state_file,
                policy=WatchdogPolicy(
                    failure_threshold=arguments.failure_threshold,
                    stale_after_seconds=arguments.stale_after_seconds,
                    max_run_seconds=arguments.max_run_seconds,
                ),
                dry_run=arguments.dry_run,
            )
            payload = result.as_payload()
            should_print_payload = not (
                arguments.quiet_when_idle and result.is_idle
            )
            if result.notification_failed:
                exit_code = 1
        else:
            result = run_once(
                arguments.config,
                log_sink=_stderr_run_log,
                allow_visible_browser=arguments.allow_visible_browser,
            )
            payload = _run_once_payload(result)
            if result.status in {"failed", "completed_with_errors"}:
                exit_code = 1
    except (
        BrowserDiscoveryError,
        CandidateSelectorError,
        ConfigLoadError,
        MemoryError,
        RssFetchError,
        RssParseError,
        SitemapFetchError,
        SitemapParseError,
        RunLockError,
        StorageError,
        TelegramFeedbackError,
        VisibleBrowserPermissionError,
        WatchdogError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (ArticleSelectionError, ReaderError, SummarizerError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if should_print_payload:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return exit_code


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="news-agent")
    commands = parser.add_subparsers(dest="command", required=True)
    discover = commands.add_parser(
        "discover",
        help=(
            "Check the configured discovery source for articles without stored "
            "bodies."
        ),
    )
    discover.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"TOML config path (default: {DEFAULT_CONFIG_PATH})",
    )
    discover.add_argument(
        "--allow-visible-browser",
        action="store_true",
        help="Explicitly permit this manual command to open a visible browser.",
    )
    read_one_command = commands.add_parser(
        "read-one",
        help="Read and store one article selected by its exact discovery identity.",
    )
    read_one_command.add_argument(
        "--guid",
        required=True,
        help="Exact GUID or canonical URL currently present in discovery.",
    )
    read_one_command.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"TOML config path (default: {DEFAULT_CONFIG_PATH})",
    )
    read_one_command.add_argument(
        "--allow-visible-browser",
        action="store_true",
        help="Explicitly permit this manual command to open a visible browser.",
    )
    summarize_one_command = commands.add_parser(
        "summarize-one",
        help=(
            "Translate, summarize, and resolve one article whose body is already "
            "stored."
        ),
    )
    summarize_one_command.add_argument(
        "--guid",
        required=True,
        help="Exact GUID of one article already stored in SQLite.",
    )
    summarize_one_command.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"TOML config path (default: {DEFAULT_CONFIG_PATH})",
    )
    notify_one_command = commands.add_parser(
        "notify-one",
        help="Attempt at most one due configured notification delivery.",
    )
    notify_one_command.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"TOML config path (default: {DEFAULT_CONFIG_PATH})",
    )
    telegram_feedback_command = commands.add_parser(
        "telegram-feedback-once",
        help=(
            "Poll Telegram message reactions once and refresh shared preference "
            "memory."
        ),
    )
    telegram_feedback_command.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"TOML config path (default: {DEFAULT_CONFIG_PATH})",
    )
    telegram_feedback_command.add_argument(
        "--quiet-when-idle",
        action="store_true",
        help="Write no JSON when no update was processed and memory was unchanged.",
    )
    memory_refresh_command = commands.add_parser(
        "memory-refresh-once",
        help="Refresh shared MEMORY.md from current Telegram feedback state.",
    )
    memory_refresh_command.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"TOML config path (default: {DEFAULT_CONFIG_PATH})",
    )
    watchdog_command = commands.add_parser(
        "watchdog-once",
        help="Check one launchd job and send deduplicated Telegram health alerts.",
    )
    watchdog_command.add_argument(
        "--config",
        type=Path,
        required=True,
        help="Source config containing the existing Telegram destination.",
    )
    watchdog_command.add_argument("--service-label", required=True)
    watchdog_command.add_argument("--run-log", type=Path, required=True)
    watchdog_command.add_argument("--state-file", type=Path, required=True)
    watchdog_command.add_argument(
        "--failure-threshold", type=_positive_cli_integer, default=2,
    )
    watchdog_command.add_argument(
        "--stale-after-seconds", type=_positive_cli_integer, default=2700,
    )
    watchdog_command.add_argument(
        "--max-run-seconds", type=_positive_cli_integer, default=1800,
    )
    watchdog_command.add_argument(
        "--quiet-when-idle",
        action="store_true",
        help="Write no JSON if no health transition or notification attempt occurred.",
    )
    watchdog_command.add_argument(
        "--dry-run",
        action="store_true",
        help="Inspect only: do not send, write state, or create a lock file.",
    )
    run_once_command = commands.add_parser(
        "run-once",
        help=(
            "Process up to three fresh articles, one due article read retry, "
            "one stored analysis retry, and three due notifications under one "
            "overlap lock."
        ),
    )
    run_once_command.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"TOML config path (default: {DEFAULT_CONFIG_PATH})",
    )
    run_once_command.add_argument(
        "--allow-visible-browser",
        action="store_true",
        help="Explicitly permit this manual run to open a visible browser.",
    )
    return parser


def _positive_cli_integer(value: str) -> int:
    try:
        selected = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a positive integer") from None
    if selected <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return selected


def _result_payload(result: PendingResult) -> dict[str, object]:
    items = sorted(
        result.pending_items,
        key=lambda item: item.published_at or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )
    return {
        "status": result.status,
        "items": [
            {
                "guid": item.guid,
                "title": item.title,
                "url": str(item.url),
            }
            for item in items
        ],
    }


def _read_one_payload(result: ReadOneResult) -> dict[str, object]:
    return {
        "status": result.status,
        "guid": result.article.guid,
        "title": result.article.title,
        "url": str(result.article.url),
        "body_characters": len(result.article.body),
    }


def _summarize_one_payload(result: SummarizeOneResult) -> dict[str, object]:
    if result.article.translated_title is None or result.article.summary is None:
        raise StorageError("Stored article summary is incomplete")
    return {
        "status": result.status,
        "guid": result.article.guid,
        "translated_title": result.article.translated_title,
        "summary": result.article.summary,
        "event_resolution": {
            "decision": result.event_resolution.decision,
            "event_id": result.event_resolution.event_id,
        },
    }


def _notify_one_payload(result: NotifyOneResult) -> dict[str, object]:
    if result.status == "no_due_notifications":
        return {"status": result.status}
    if (
        result.delivery_id is None
        or result.event_id is None
        or result.target_id is None
        or result.attempt is None
    ):
        raise StorageError("Notification result is incomplete")

    payload: dict[str, object] = {
        "status": result.status,
        "delivery_id": result.delivery_id,
        "event_id": result.event_id,
        "target_id": result.target_id,
        "attempt": result.attempt,
    }
    if result.status == "sent":
        payload["external_receipt_ids"] = list(result.external_receipt_ids)
    elif result.status == "retry_scheduled":
        if result.retry_at is None:
            raise StorageError("Notification retry result is incomplete")
        payload["retry_at"] = result.retry_at.isoformat().replace("+00:00", "Z")
    elif result.external_receipt_ids:
        payload["partial_external_receipt_ids"] = list(
            result.external_receipt_ids
        )
    return payload


def _telegram_feedback_once_payload(
    result: TelegramFeedbackOnceResult,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "status": result.status,
        "targets": [
            {
                "target_id": target.target_id,
                "processed_update_count": target.processed_update_count,
                "recorded_count": target.recorded_count,
                "duplicate_count": target.duplicate_count,
                "ignored_count": target.ignored_count,
                "unmatched_count": target.unmatched_count,
                "next_update_id": target.next_update_id,
            }
            for target in result.targets
        ],
    }
    if result.memory is not None:
        payload["memory"] = _memory_refresh_payload(result.memory)
    return payload


def _telegram_feedback_result_is_idle(
    result: TelegramFeedbackOnceResult,
) -> bool:
    return (
        result.memory is not None
        and not result.memory.changed
        and all(target.processed_update_count == 0 for target in result.targets)
    )


def _memory_refresh_payload(result: MemoryRefreshResult) -> dict[str, object]:
    return {
        "status": "refreshed",
        "changed": result.changed,
        "memory_path": str(result.memory_path),
        "item_count": result.item_count,
        "more_like_this_count": result.more_like_this_count,
        "less_like_this_count": result.less_like_this_count,
        "strong_negative_count": result.strong_negative_count,
        "updated_at": result.updated_at,
    }


def _run_once_payload(result: RunOnceResult) -> dict[str, object]:
    payload: dict[str, object] = {
        "status": result.status,
        "run_id": result.run_id,
        "started_at": _run_timestamp(result.started_at),
        "finished_at": _run_timestamp(result.finished_at),
        "max_fresh_articles": MAX_ARTICLES_PER_RUN,
        "max_article_read_retries": MAX_ARTICLE_READ_RETRIES_PER_RUN,
        "max_analysis_retries": MAX_ANALYSIS_RETRIES_PER_RUN,
        "selected_article_count": len(result.articles),
        "selected_read_retry_count": result.selected_read_retry_count,
        "articles": [_run_article_payload(item) for item in result.articles],
        "max_notification_attempts": MAX_NOTIFICATION_ATTEMPTS_PER_RUN,
        "notifications": [
            _notify_one_payload(notification)
            for notification in result.notifications
        ],
    }
    if result.discovery_status is not None:
        payload["discovery"] = {
            "status": result.discovery_status,
            "item_count": result.discovered_item_count,
            "skipped_due_to_cap_count": len(result.skipped_discoveries),
            "skipped_due_to_cap": [
                _discovery_item_payload(item)
                for item in result.skipped_discoveries
            ],
        }
    if result.discovery_checkpoint_error_type is not None:
        payload["discovery_checkpoint_error"] = {
            "type": result.discovery_checkpoint_error_type,
            "message": result.discovery_checkpoint_error_message,
        }
    if result.notification_error_type is not None:
        payload["notification_error"] = {
            "type": result.notification_error_type,
            "message": result.notification_error_message,
        }
    if result.run_error_type is not None:
        payload["run_error"] = {
            "stage": result.run_error_stage,
            "type": result.run_error_type,
            "message": result.run_error_message,
        }
    return payload


def _discovery_item_payload(item: DiscoveredItem) -> dict[str, object]:
    payload: dict[str, object] = {
        "identity": item.guid,
        "title": item.title,
        "url": str(item.url),
    }
    if item.published_at is not None:
        payload["published_at"] = _run_timestamp(item.published_at)
    return payload


def _run_article_payload(result: RunArticleResult) -> dict[str, object]:
    payload: dict[str, object] = {
        "identity": result.identity,
        "title": result.title,
        "url": result.url,
        "status": result.status,
    }
    if result.guid is not None:
        payload["guid"] = result.guid
    if result.read_status is not None:
        payload["read_status"] = result.read_status
    if result.analysis_status is not None:
        payload["analysis_status"] = result.analysis_status
    if result.event_resolution is not None:
        payload["event_resolution"] = {
            "decision": result.event_resolution.decision,
            "event_id": result.event_resolution.event_id,
        }
    if result.failed_stage is not None:
        payload["failed_stage"] = result.failed_stage
    if result.error_type is not None:
        payload["error"] = {
            "type": result.error_type,
            "message": result.error_message,
        }
    if result.read_failure_status is not None:
        read_failure: dict[str, object] = {
            "status": result.read_failure_status,
            "reason": result.read_failure_reason,
            "attempts": result.read_failure_attempts,
        }
        if result.read_retry_at is not None:
            read_failure["retry_at"] = result.read_retry_at
        payload["read_failure"] = read_failure
    return payload


def _stderr_run_log(entry: dict[str, object]) -> None:
    print(
        json.dumps(entry, ensure_ascii=False, separators=(",", ":")),
        file=sys.stderr,
        flush=True,
    )


if __name__ == "__main__":
    raise SystemExit(main())
