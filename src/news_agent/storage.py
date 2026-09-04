"""SQLite storage for articles whose full body was extracted successfully."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    ValidationError,
    field_serializer,
    field_validator,
    model_validator,
)

from news_agent.candidates import ArticleCandidate
from news_agent.rss import DiscoveredItem

NonEmptyText = Annotated[str, Field(min_length=1)]
PendingStatus = Literal["no_pending_items", "pending_items"]
ArticleReadFailureStatus = Literal["retry_wait", "dead"]
EventDecision = Literal["new_event", "existing_event", "non_event"]
NotificationDeliveryStatus = Literal[
    "pending",
    "sending",
    "retry_wait",
    "sent",
    "dead",
]
FeedbackSignal = Literal[
    "more_like_this",
    "less_like_this",
    "strong_negative",
    "cleared",
]

MAX_EVENT_CANDIDATES = 2
_MAX_FTS_QUERY_TERMS = 64
_MAX_READ_FAILURE_ERROR_LENGTH = 1000
_MAX_READ_FAILURE_REASON_LENGTH = 128
_WORD_PATTERN = re.compile(r"[^\W_]+(?:['’\-][^\W_]+)*", re.UNICODE)


class StorageError(RuntimeError):
    """Raised when the article database cannot be used safely."""


class StoredArticle(BaseModel):
    """Complete article shape persisted after successful body extraction."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    source_id: NonEmptyText
    guid: NonEmptyText
    published_at: datetime | None
    title: NonEmptyText
    url: HttpUrl
    body: NonEmptyText
    translated_title: NonEmptyText | None = None
    summary: NonEmptyText | None = None

    @model_validator(mode="after")
    def require_complete_summary_pair(self) -> StoredArticle:
        if (self.translated_title is None) != (self.summary is None):
            raise ValueError(
                "translated_title and summary must both be null or both be present"
            )
        return self

    @field_validator("published_at", mode="before")
    @classmethod
    def require_iso_datetime_input(
        cls,
        value: object,
    ) -> datetime | str | None:
        if value is None or isinstance(value, datetime):
            return value
        if not isinstance(value, str):
            raise ValueError("published_at must be an ISO 8601 string or null")

        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            return datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise ValueError(
                "published_at must be an ISO 8601 string or null"
            ) from exc

    @field_validator("published_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("published_at must include a timezone")
        return value.astimezone(UTC)

    @field_serializer("published_at")
    def serialize_published_at(self, value: datetime | None) -> str | None:
        if value is None:
            return None
        return value.isoformat().replace("+00:00", "Z")


class NotificationTarget(BaseModel):
    """Stable delivery destination resolved by a channel adapter."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    target_id: NonEmptyText
    adapter: NonEmptyText


@dataclass(frozen=True)
class PendingResult:
    """Discovered items that do not yet have a complete stored article."""

    status: PendingStatus
    pending_items: tuple[DiscoveredItem, ...]


@dataclass(frozen=True)
class DiscoverySelection:
    """Fresh discoveries selected now and overflow permanently skipped."""

    selected_items: tuple[DiscoveredItem, ...]
    skipped_items: tuple[DiscoveredItem, ...]


@dataclass(frozen=True)
class DiscoveryHttpValidators:
    """Persisted conditional-request validators for one discovery source."""

    source_id: str
    etag: str | None
    last_modified: str | None
    updated_at: str


@dataclass(frozen=True)
class ArticleReadFailure:
    """Current retry or terminal state for one article URL."""

    source_id: str
    guid: str
    url: str
    title: str
    status: ArticleReadFailureStatus
    reason: str
    attempts: int
    first_failed_at: str
    last_failed_at: str
    next_attempt_at: str | None
    last_error: str

    @property
    def retry_at(self) -> str | None:
        """Compatibility alias for callers that describe the next attempt as retry."""

        return self.next_attempt_at


@dataclass(frozen=True)
class EventArticleTitle:
    """Original and translated titles from one article linked to an Event."""

    original_title: str
    translated_title: str


@dataclass(frozen=True)
class EventCandidate:
    """Compact Event evidence supplied to the article-analysis model."""

    event_id: int
    first_seen_at: str | None
    last_seen_at: str | None
    linked_article_titles: tuple[EventArticleTitle, ...]
    latest_article_summary: str

    def to_prompt_dict(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "first_seen_at": self.first_seen_at,
            "last_seen_at": self.last_seen_at,
            "linked_article_titles": [
                {
                    "original_title": title.original_title,
                    "translated_title": title.translated_title,
                }
                for title in self.linked_article_titles
            ],
            "latest_article_summary": self.latest_article_summary,
        }


@dataclass(frozen=True)
class StoredEventResolution:
    """Persisted decision connecting one article to an Event, or to none."""

    decision: EventDecision
    event_id: int | None


@dataclass(frozen=True)
class AnalysisStoreResult:
    """Atomic persistence result for generated article text and Event resolution."""

    inserted: bool
    resolution: StoredEventResolution


@dataclass(frozen=True)
class ClaimedNotificationDelivery:
    """One leased delivery with its immutable channel-neutral payload."""

    delivery_id: int
    outbox_id: int
    event_id: int
    source_id: str
    guid: str
    article_url: str
    target: NotificationTarget
    translated_title: str
    summary: str
    attempt: int
    claim_token: str
    lease_until: str


@dataclass(frozen=True)
class TelegramDeliveryMatch:
    """A sent Telegram message mapped back to one notification payload."""

    delivery_id: int
    event_id: int
    source_id: str
    guid: str
    article_url: str


@dataclass(frozen=True)
class FeedbackObservation:
    """One Telegram reaction update resolved to one news notification."""

    update_id: int
    target_id: str
    chat_id: str
    message_id: str
    delivery_id: int
    event_id: int
    source_id: str
    guid: str
    article_url: str
    reaction_emoji: str
    signal: FeedbackSignal
    raw_old_reaction: str
    raw_new_reaction: str


@dataclass(frozen=True)
class FeedbackMemoryItem:
    """Current user feedback state for memory rendering."""

    signal: str
    reaction_emoji: str
    updated_at: str
    event_id: int
    source_id: str
    guid: str
    article_url: str
    title: str


_CREATE_ARTICLES_TABLE = """
CREATE TABLE IF NOT EXISTS articles (
    source_id TEXT NOT NULL,
    guid TEXT NOT NULL,
    published_at TEXT,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    body TEXT NOT NULL CHECK (length(trim(body)) > 0),
    translated_title TEXT,
    summary TEXT,
    PRIMARY KEY (source_id, guid)
)
"""

_CREATE_DISCOVERY_SKIPS_TABLE = """
CREATE TABLE IF NOT EXISTS discovery_skips (
    source_id TEXT NOT NULL CHECK (length(trim(source_id)) > 0),
    guid TEXT NOT NULL CHECK (length(trim(guid)) > 0),
    url TEXT NOT NULL CHECK (length(trim(url)) > 0),
    title TEXT NOT NULL CHECK (length(trim(title)) > 0),
    published_at TEXT
        CHECK (published_at IS NULL OR length(trim(published_at)) > 0),
    reason TEXT NOT NULL DEFAULT 'run_cap'
        CHECK (reason = 'run_cap'),
    skipped_at TEXT NOT NULL CHECK (length(trim(skipped_at)) > 0),
    PRIMARY KEY (source_id, guid),
    UNIQUE (source_id, url)
)
"""

_CREATE_DISCOVERY_HTTP_VALIDATORS_TABLE = """
CREATE TABLE IF NOT EXISTS discovery_http_validators (
    source_id TEXT NOT NULL PRIMARY KEY
        CHECK (length(trim(source_id)) > 0),
    etag TEXT CHECK (etag IS NULL OR length(trim(etag)) > 0),
    last_modified TEXT
        CHECK (last_modified IS NULL OR length(trim(last_modified)) > 0),
    updated_at TEXT NOT NULL CHECK (length(trim(updated_at)) > 0),
    CHECK (
        (etag IS NOT NULL AND length(trim(etag)) > 0)
        OR (last_modified IS NOT NULL AND length(trim(last_modified)) > 0)
    )
)
"""

_CREATE_DISCOVERY_CANDIDATES_TABLE = """
CREATE TABLE IF NOT EXISTS discovery_candidates (
    source_id TEXT NOT NULL CHECK (length(trim(source_id)) > 0),
    url TEXT NOT NULL CHECK (length(trim(url)) > 0),
    title TEXT NOT NULL CHECK (length(trim(title)) > 0),
    section_url TEXT NOT NULL CHECK (length(trim(section_url)) > 0),
    section_title TEXT NOT NULL CHECK (length(trim(section_title)) > 0),
    visible_text TEXT NOT NULL CHECK (length(trim(visible_text)) > 0),
    published_at TEXT
        CHECK (published_at IS NULL OR length(trim(published_at)) > 0),
    first_seen_at TEXT NOT NULL CHECK (length(trim(first_seen_at)) > 0),
    last_seen_at TEXT NOT NULL CHECK (length(trim(last_seen_at)) > 0),
    last_prompted_at TEXT
        CHECK (last_prompted_at IS NULL OR length(trim(last_prompted_at)) > 0),
    rejected_until TEXT
        CHECK (rejected_until IS NULL OR length(trim(rejected_until)) > 0),
    selected_count INTEGER NOT NULL DEFAULT 0 CHECK (selected_count >= 0),
    prompt_count INTEGER NOT NULL DEFAULT 0 CHECK (prompt_count >= 0),
    PRIMARY KEY (source_id, url)
)
"""

_CREATE_EVENTS_TABLE = """
CREATE TABLE IF NOT EXISTS events (
    event_id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL
)
"""

_CREATE_ARTICLE_EVENT_RESOLUTIONS_TABLE = """
CREATE TABLE IF NOT EXISTS article_event_resolutions (
    source_id TEXT NOT NULL,
    guid TEXT NOT NULL,
    decision TEXT NOT NULL
        CHECK (decision IN ('new_event', 'existing_event', 'non_event')),
    event_id INTEGER,
    resolved_at TEXT NOT NULL,
    PRIMARY KEY (source_id, guid),
    FOREIGN KEY (source_id, guid)
        REFERENCES articles (source_id, guid),
    FOREIGN KEY (event_id)
        REFERENCES events (event_id),
    CHECK (
        (decision IN ('new_event', 'existing_event') AND event_id IS NOT NULL)
        OR (decision = 'non_event' AND event_id IS NULL)
    )
)
"""

_CREATE_ARTICLE_ANALYSIS_RETRIES_TABLE = """
CREATE TABLE IF NOT EXISTS article_analysis_retries (
    source_id TEXT NOT NULL CHECK (length(trim(source_id)) > 0),
    guid TEXT NOT NULL CHECK (length(trim(guid)) > 0),
    attempts INTEGER NOT NULL CHECK (attempts >= 1),
    last_failed_at TEXT NOT NULL
        CHECK (length(trim(last_failed_at)) > 0),
    PRIMARY KEY (source_id, guid),
    FOREIGN KEY (source_id, guid)
        REFERENCES articles (source_id, guid)
        ON DELETE CASCADE
)
"""

_CREATE_ARTICLE_READ_FAILURES_TABLE = """
CREATE TABLE IF NOT EXISTS article_read_failures (
    source_id TEXT NOT NULL
        CHECK (
            length(trim(source_id)) > 0
            AND source_id = trim(source_id)
        ),
    url TEXT NOT NULL
        CHECK (
            length(trim(url)) > 0
            AND url = trim(url)
        ),
    guid TEXT NOT NULL
        CHECK (
            length(trim(guid)) > 0
            AND guid = trim(guid)
        ),
    title TEXT NOT NULL
        CHECK (
            length(trim(title)) > 0
            AND title = trim(title)
        ),
    status TEXT NOT NULL
        CHECK (status IN ('retry_wait', 'dead')),
    reason TEXT NOT NULL
        CHECK (
            length(trim(reason)) > 0
            AND reason = trim(reason)
            AND length(reason) <= 128
            AND instr(reason, char(10)) = 0
            AND instr(reason, char(13)) = 0
        ),
    attempts INTEGER NOT NULL CHECK (attempts >= 1),
    first_failed_at TEXT NOT NULL
        CHECK (length(trim(first_failed_at)) > 0),
    last_failed_at TEXT NOT NULL
        CHECK (length(trim(last_failed_at)) > 0),
    next_attempt_at TEXT,
    last_error TEXT NOT NULL
        CHECK (
            length(trim(last_error)) > 0
            AND last_error = trim(last_error)
            AND length(last_error) <= 1000
            AND instr(last_error, char(10)) = 0
            AND instr(last_error, char(13)) = 0
        ),
    PRIMARY KEY (source_id, url),
    UNIQUE (source_id, guid),
    CHECK (first_failed_at <= last_failed_at),
    CHECK (
        (status = 'retry_wait'
            AND next_attempt_at IS NOT NULL
            AND length(trim(next_attempt_at)) > 0
            AND next_attempt_at > last_failed_at)
        OR (status = 'dead' AND next_attempt_at IS NULL)
    )
)
"""

_CREATE_EVENT_ARTICLES_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS event_articles_fts USING fts5(
    event_id UNINDEXED,
    source_id UNINDEXED,
    guid UNINDEXED,
    title,
    lead,
    tokenize='unicode61'
)
"""

_CREATE_NOTIFICATION_OUTBOX_TABLE = """
CREATE TABLE IF NOT EXISTS notification_outbox (
    outbox_id INTEGER PRIMARY KEY,
    event_id INTEGER NOT NULL UNIQUE,
    source_id TEXT NOT NULL,
    guid TEXT NOT NULL,
    article_url TEXT NOT NULL
        CHECK (length(trim(article_url)) > 0),
    translated_title TEXT NOT NULL
        CHECK (length(trim(translated_title)) > 0),
    summary TEXT NOT NULL
        CHECK (length(trim(summary)) > 0),
    created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
    FOREIGN KEY (event_id)
        REFERENCES events (event_id),
    FOREIGN KEY (source_id, guid)
        REFERENCES articles (source_id, guid)
)
"""

_CREATE_NOTIFICATION_DELIVERIES_TABLE = """
CREATE TABLE IF NOT EXISTS notification_deliveries (
    delivery_id INTEGER PRIMARY KEY,
    outbox_id INTEGER NOT NULL,
    target_id TEXT NOT NULL CHECK (length(trim(target_id)) > 0),
    adapter TEXT NOT NULL CHECK (length(trim(adapter)) > 0),
    status TEXT NOT NULL
        CHECK (status IN ('pending', 'sending', 'retry_wait', 'sent', 'dead')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at TEXT,
    claim_token TEXT UNIQUE,
    lease_until TEXT,
    last_error TEXT,
    external_receipt_id TEXT,
    created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
    updated_at TEXT NOT NULL CHECK (length(trim(updated_at)) > 0),
    sent_at TEXT,
    UNIQUE (outbox_id, target_id),
    FOREIGN KEY (outbox_id)
        REFERENCES notification_outbox (outbox_id),
    CHECK (
        external_receipt_id IS NULL
        OR length(trim(external_receipt_id)) > 0
    ),
    CHECK (
        (status = 'pending'
            AND attempts = 0
            AND next_attempt_at IS NULL
            AND claim_token IS NULL
            AND lease_until IS NULL
            AND last_error IS NULL
            AND external_receipt_id IS NULL
            AND sent_at IS NULL)
        OR (status = 'sending'
            AND attempts >= 1
            AND next_attempt_at IS NULL
            AND claim_token IS NOT NULL
            AND length(trim(claim_token)) > 0
            AND lease_until IS NOT NULL
            AND length(trim(lease_until)) > 0
            AND last_error IS NULL
            AND external_receipt_id IS NULL
            AND sent_at IS NULL)
        OR (status = 'retry_wait'
            AND attempts >= 1
            AND next_attempt_at IS NOT NULL
            AND length(trim(next_attempt_at)) > 0
            AND claim_token IS NULL
            AND lease_until IS NULL
            AND last_error IS NOT NULL
            AND length(trim(last_error)) > 0
            AND external_receipt_id IS NULL
            AND sent_at IS NULL)
        OR (status = 'sent'
            AND attempts >= 1
            AND next_attempt_at IS NULL
            AND claim_token IS NULL
            AND lease_until IS NULL
            AND last_error IS NULL
            AND sent_at IS NOT NULL
            AND length(trim(sent_at)) > 0)
        OR (status = 'dead'
            AND attempts >= 1
            AND next_attempt_at IS NULL
            AND claim_token IS NULL
            AND lease_until IS NULL
            AND last_error IS NOT NULL
            AND length(trim(last_error)) > 0
            AND sent_at IS NULL)
    )
)
"""

_CREATE_TELEGRAM_UPDATE_OFFSETS_TABLE = """
CREATE TABLE IF NOT EXISTS telegram_update_offsets (
    target_id TEXT NOT NULL PRIMARY KEY
        CHECK (length(trim(target_id)) > 0),
    next_update_id INTEGER NOT NULL CHECK (next_update_id > 0),
    updated_at TEXT NOT NULL CHECK (length(trim(updated_at)) > 0)
)
"""

_CREATE_FEEDBACK_OBSERVATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS feedback_observations (
    observation_id INTEGER PRIMARY KEY,
    update_id INTEGER NOT NULL UNIQUE CHECK (update_id > 0),
    observed_at TEXT NOT NULL CHECK (length(trim(observed_at)) > 0),
    target_id TEXT NOT NULL CHECK (length(trim(target_id)) > 0),
    chat_id TEXT NOT NULL CHECK (length(trim(chat_id)) > 0),
    message_id TEXT NOT NULL CHECK (length(trim(message_id)) > 0),
    delivery_id INTEGER NOT NULL,
    event_id INTEGER NOT NULL,
    source_id TEXT NOT NULL CHECK (length(trim(source_id)) > 0),
    guid TEXT NOT NULL CHECK (length(trim(guid)) > 0),
    article_url TEXT NOT NULL CHECK (length(trim(article_url)) > 0),
    reaction_emoji TEXT NOT NULL CHECK (length(trim(reaction_emoji)) > 0),
    signal TEXT NOT NULL
        CHECK (
            signal IN (
                'more_like_this',
                'less_like_this',
                'strong_negative',
                'cleared'
            )
        ),
    raw_old_reaction TEXT NOT NULL CHECK (length(trim(raw_old_reaction)) > 0),
    raw_new_reaction TEXT NOT NULL CHECK (length(trim(raw_new_reaction)) > 0),
    FOREIGN KEY (delivery_id)
        REFERENCES notification_deliveries (delivery_id),
    FOREIGN KEY (source_id, guid)
        REFERENCES articles (source_id, guid)
)
"""

_CREATE_MESSAGE_FEEDBACK_STATE_TABLE = """
CREATE TABLE IF NOT EXISTS message_feedback_state (
    target_id TEXT NOT NULL CHECK (length(trim(target_id)) > 0),
    chat_id TEXT NOT NULL CHECK (length(trim(chat_id)) > 0),
    message_id TEXT NOT NULL CHECK (length(trim(message_id)) > 0),
    delivery_id INTEGER NOT NULL,
    event_id INTEGER NOT NULL,
    source_id TEXT NOT NULL CHECK (length(trim(source_id)) > 0),
    guid TEXT NOT NULL CHECK (length(trim(guid)) > 0),
    article_url TEXT NOT NULL CHECK (length(trim(article_url)) > 0),
    reaction_emoji TEXT NOT NULL CHECK (length(trim(reaction_emoji)) > 0),
    signal TEXT NOT NULL
        CHECK (
            signal IN (
                'more_like_this',
                'less_like_this',
                'strong_negative'
            )
        ),
    updated_at TEXT NOT NULL CHECK (length(trim(updated_at)) > 0),
    PRIMARY KEY (target_id, chat_id, message_id),
    FOREIGN KEY (delivery_id)
        REFERENCES notification_deliveries (delivery_id),
    FOREIGN KEY (source_id, guid)
        REFERENCES articles (source_id, guid)
)
"""

_LEGACY_COLUMNS = (
    ("source_id", "TEXT", 1, 1),
    ("guid", "TEXT", 1, 2),
    ("published_at", "TEXT", 0, 0),
    ("title", "TEXT", 1, 0),
    ("url", "TEXT", 1, 0),
    ("body", "TEXT", 1, 0),
)

_EXPECTED_COLUMNS = _LEGACY_COLUMNS + (
    ("translated_title", "TEXT", 0, 0),
    ("summary", "TEXT", 0, 0),
)

_EXPECTED_DISCOVERY_SKIP_COLUMNS = (
    ("source_id", "TEXT", 1, 1),
    ("guid", "TEXT", 1, 2),
    ("url", "TEXT", 1, 0),
    ("title", "TEXT", 1, 0),
    ("published_at", "TEXT", 0, 0),
    ("reason", "TEXT", 1, 0),
    ("skipped_at", "TEXT", 1, 0),
)

_EXPECTED_DISCOVERY_HTTP_VALIDATOR_COLUMNS = (
    ("source_id", "TEXT", 1, 1),
    ("etag", "TEXT", 0, 0),
    ("last_modified", "TEXT", 0, 0),
    ("updated_at", "TEXT", 1, 0),
)

_EXPECTED_DISCOVERY_CANDIDATE_COLUMNS = (
    ("source_id", "TEXT", 1, 1),
    ("url", "TEXT", 1, 2),
    ("title", "TEXT", 1, 0),
    ("section_url", "TEXT", 1, 0),
    ("section_title", "TEXT", 1, 0),
    ("visible_text", "TEXT", 1, 0),
    ("published_at", "TEXT", 0, 0),
    ("first_seen_at", "TEXT", 1, 0),
    ("last_seen_at", "TEXT", 1, 0),
    ("last_prompted_at", "TEXT", 0, 0),
    ("rejected_until", "TEXT", 0, 0),
    ("selected_count", "INTEGER", 1, 0),
    ("prompt_count", "INTEGER", 1, 0),
)

_EXPECTED_EVENTS_COLUMNS = (
    ("event_id", "INTEGER", 0, 1),
    ("created_at", "TEXT", 1, 0),
)

_EXPECTED_RESOLUTION_COLUMNS = (
    ("source_id", "TEXT", 1, 1),
    ("guid", "TEXT", 1, 2),
    ("decision", "TEXT", 1, 0),
    ("event_id", "INTEGER", 0, 0),
    ("resolved_at", "TEXT", 1, 0),
)

_EXPECTED_ARTICLE_ANALYSIS_RETRY_COLUMNS = (
    ("source_id", "TEXT", 1, 1),
    ("guid", "TEXT", 1, 2),
    ("attempts", "INTEGER", 1, 0),
    ("last_failed_at", "TEXT", 1, 0),
)

_EXPECTED_ARTICLE_READ_FAILURE_COLUMNS = (
    ("source_id", "TEXT", 1, 1),
    ("url", "TEXT", 1, 2),
    ("guid", "TEXT", 1, 0),
    ("title", "TEXT", 1, 0),
    ("status", "TEXT", 1, 0),
    ("reason", "TEXT", 1, 0),
    ("attempts", "INTEGER", 1, 0),
    ("first_failed_at", "TEXT", 1, 0),
    ("last_failed_at", "TEXT", 1, 0),
    ("next_attempt_at", "TEXT", 0, 0),
    ("last_error", "TEXT", 1, 0),
)

_EXPECTED_FTS_COLUMNS = ("event_id", "source_id", "guid", "title", "lead")

_EXPECTED_NOTIFICATION_OUTBOX_COLUMNS = (
    ("outbox_id", "INTEGER", 0, 1),
    ("event_id", "INTEGER", 1, 0),
    ("source_id", "TEXT", 1, 0),
    ("guid", "TEXT", 1, 0),
    ("article_url", "TEXT", 1, 0),
    ("translated_title", "TEXT", 1, 0),
    ("summary", "TEXT", 1, 0),
    ("created_at", "TEXT", 1, 0),
)

_EXPECTED_NOTIFICATION_DELIVERY_COLUMNS = (
    ("delivery_id", "INTEGER", 0, 1),
    ("outbox_id", "INTEGER", 1, 0),
    ("target_id", "TEXT", 1, 0),
    ("adapter", "TEXT", 1, 0),
    ("status", "TEXT", 1, 0),
    ("attempts", "INTEGER", 1, 0),
    ("next_attempt_at", "TEXT", 0, 0),
    ("claim_token", "TEXT", 0, 0),
    ("lease_until", "TEXT", 0, 0),
    ("last_error", "TEXT", 0, 0),
    ("external_receipt_id", "TEXT", 0, 0),
    ("created_at", "TEXT", 1, 0),
    ("updated_at", "TEXT", 1, 0),
    ("sent_at", "TEXT", 0, 0),
)

_EXPECTED_TELEGRAM_UPDATE_OFFSET_COLUMNS = (
    ("target_id", "TEXT", 1, 1),
    ("next_update_id", "INTEGER", 1, 0),
    ("updated_at", "TEXT", 1, 0),
)

_EXPECTED_FEEDBACK_OBSERVATION_COLUMNS = (
    ("observation_id", "INTEGER", 0, 1),
    ("update_id", "INTEGER", 1, 0),
    ("observed_at", "TEXT", 1, 0),
    ("target_id", "TEXT", 1, 0),
    ("chat_id", "TEXT", 1, 0),
    ("message_id", "TEXT", 1, 0),
    ("delivery_id", "INTEGER", 1, 0),
    ("event_id", "INTEGER", 1, 0),
    ("source_id", "TEXT", 1, 0),
    ("guid", "TEXT", 1, 0),
    ("article_url", "TEXT", 1, 0),
    ("reaction_emoji", "TEXT", 1, 0),
    ("signal", "TEXT", 1, 0),
    ("raw_old_reaction", "TEXT", 1, 0),
    ("raw_new_reaction", "TEXT", 1, 0),
)

_EXPECTED_MESSAGE_FEEDBACK_STATE_COLUMNS = (
    ("target_id", "TEXT", 1, 1),
    ("chat_id", "TEXT", 1, 2),
    ("message_id", "TEXT", 1, 3),
    ("delivery_id", "INTEGER", 1, 0),
    ("event_id", "INTEGER", 1, 0),
    ("source_id", "TEXT", 1, 0),
    ("guid", "TEXT", 1, 0),
    ("article_url", "TEXT", 1, 0),
    ("reaction_emoji", "TEXT", 1, 0),
    ("signal", "TEXT", 1, 0),
    ("updated_at", "TEXT", 1, 0),
)


def find_pending_articles(
    database_file: str | Path,
    source_id: str,
    items: Iterable[DiscoveredItem],
    *,
    now: datetime | None = None,
) -> PendingResult:
    """Return discoveries not stored, skipped, or held by a read failure."""

    selected_source = _required_nonempty_text(source_id, "Discovery source ID")
    unique_items = _unique_items(items)
    selected_now = _discovery_timestamp(
        _discovery_datetime(now, "Pending discovery time")
    )
    pending: list[DiscoveredItem] = []

    with _database(database_file) as connection:
        for item in unique_items:
            stored = connection.execute(
                """
                SELECT 1
                FROM articles
                WHERE source_id = ?
                  AND (guid = ? OR url = ?)
                UNION ALL
                SELECT 1
                FROM discovery_skips
                WHERE source_id = ?
                  AND (guid = ? OR url = ?)
                UNION ALL
                SELECT 1
                FROM article_read_failures
                WHERE source_id = ?
                  AND (guid = ? OR url = ?)
                  AND (
                      status = 'dead'
                      OR (status = 'retry_wait' AND next_attempt_at > ?)
                  )
                LIMIT 1
                """,
                (
                    selected_source,
                    item.guid,
                    str(item.url),
                    selected_source,
                    item.guid,
                    str(item.url),
                    selected_source,
                    item.guid,
                    str(item.url),
                    selected_now,
                ),
            ).fetchone()
            if stored is None:
                pending.append(item)

    status: PendingStatus = "pending_items" if pending else "no_pending_items"
    return PendingResult(status=status, pending_items=tuple(pending))


def select_discoveries_for_run(
    database_file: str | Path,
    source_id: str,
    items: Iterable[DiscoveredItem],
    *,
    limit: int = 3,
    now: datetime | None = None,
    preserve_order: bool = False,
) -> DiscoverySelection:
    """Select fresh newest items and permanently discard this run's overflow."""

    selected_source = _required_nonempty_text(source_id, "Discovery source ID")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise StorageError("Discovery selection limit must be a positive integer")
    if not isinstance(preserve_order, bool):
        raise StorageError("Discovery order flag must be boolean")
    selected_now = _discovery_datetime(now, "Discovery selection time")
    unique_items = _selection_items(items)
    ordered_items = (
        unique_items
        if preserve_order
        else tuple(sorted(unique_items, key=_discovery_sort_key))
    )
    skipped_at = _discovery_timestamp(selected_now)

    with _database(database_file) as connection:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            candidates = tuple(
                item
                for item in ordered_items
                if not _discovery_identity_exists(
                    connection,
                    selected_source,
                    item,
                )
            )
            selected_items = candidates[:limit]
            skipped_items = candidates[limit:]
            connection.executemany(
                """
                INSERT INTO discovery_skips (
                    source_id,
                    guid,
                    url,
                    title,
                    published_at,
                    reason,
                    skipped_at
                )
                VALUES (?, ?, ?, ?, ?, 'run_cap', ?)
                """,
                (
                    (
                        selected_source,
                        item.guid,
                        str(item.url),
                        item.title,
                        _discovery_published_timestamp(item.published_at),
                        skipped_at,
                    )
                    for item in skipped_items
                ),
            )

    return DiscoverySelection(
        selected_items=selected_items,
        skipped_items=skipped_items,
    )


def select_browser_candidates_for_prompt(
    database_file: str | Path,
    source_id: str,
    candidates: Iterable[ArticleCandidate],
    *,
    limit: int,
    now: datetime | None = None,
) -> tuple[ArticleCandidate, ...]:
    """Persist visible candidates and return promptable, non-cooled-down rows."""

    selected_source = _required_nonempty_text(source_id, "Candidate source ID")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise StorageError("Candidate prompt limit must be a positive integer")
    selected_now = _discovery_datetime(now, "Candidate discovery time")
    timestamp = _discovery_timestamp(selected_now)
    unique_candidates = _browser_candidate_items(candidates)
    if not unique_candidates:
        return ()

    with _database(database_file) as connection:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.executemany(
                """
                INSERT INTO discovery_candidates (
                    source_id,
                    url,
                    title,
                    section_url,
                    section_title,
                    visible_text,
                    published_at,
                    first_seen_at,
                    last_seen_at,
                    last_prompted_at,
                    rejected_until,
                    selected_count,
                    prompt_count
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, 0, 0)
                ON CONFLICT (source_id, url) DO UPDATE SET
                    title = excluded.title,
                    section_url = excluded.section_url,
                    section_title = excluded.section_title,
                    visible_text = excluded.visible_text,
                    published_at = excluded.published_at,
                    last_seen_at = excluded.last_seen_at
                """,
                (
                    _candidate_row(selected_source, candidate, timestamp)
                    for candidate in unique_candidates
                ),
            )
            promptable = tuple(
                candidate
                for candidate in unique_candidates
                if not _discovery_candidate_is_suppressed(
                    connection,
                    selected_source,
                    candidate,
                    now=timestamp,
                )
            )

    return promptable[:limit]


def record_browser_candidate_selection(
    database_file: str | Path,
    source_id: str,
    prompted_candidates: Iterable[ArticleCandidate],
    selected_candidates: Iterable[ArticleCandidate],
    *,
    rejected_cooldown_hours: float,
    now: datetime | None = None,
) -> None:
    """Record LLM candidate selection and temporarily cool down rejected URLs."""

    selected_source = _required_nonempty_text(source_id, "Candidate source ID")
    prompted = _browser_candidate_items(prompted_candidates)
    selected = _browser_candidate_items(selected_candidates)
    if not prompted:
        return
    if (
        not isinstance(rejected_cooldown_hours, (int, float))
        or isinstance(rejected_cooldown_hours, bool)
        or not 0 < float(rejected_cooldown_hours) < float("inf")
    ):
        raise StorageError("Candidate rejection cooldown must be positive and finite")

    prompted_urls = {str(candidate.url) for candidate in prompted}
    selected_urls = {str(candidate.url) for candidate in selected}
    if not selected_urls <= prompted_urls:
        raise StorageError("Selected candidates must come from prompted candidates")

    selected_now = _discovery_datetime(now, "Candidate selection time")
    prompted_at = _discovery_timestamp(selected_now)
    rejected_until = _discovery_timestamp(
        selected_now + timedelta(hours=float(rejected_cooldown_hours))
    )

    with _database(database_file) as connection:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            for candidate in prompted:
                url = str(candidate.url)
                is_selected = url in selected_urls
                connection.execute(
                    """
                    UPDATE discovery_candidates
                    SET
                        last_prompted_at = ?,
                        prompt_count = prompt_count + 1,
                        selected_count = selected_count + ?,
                        rejected_until = ?
                    WHERE source_id = ? AND url = ?
                    """,
                    (
                        prompted_at,
                        1 if is_selected else 0,
                        None if is_selected else rejected_until,
                        selected_source,
                        url,
                    ),
                )


def get_discovery_http_validators(
    database_file: str | Path,
    source_id: str,
) -> DiscoveryHttpValidators | None:
    """Load conditional-request validators for one discovery source."""

    selected_source = _required_nonempty_text(source_id, "Discovery source ID")
    with _database(database_file) as connection:
        row = connection.execute(
            """
            SELECT source_id, etag, last_modified, updated_at
            FROM discovery_http_validators
            WHERE source_id = ?
            """,
            (selected_source,),
        ).fetchone()

    if row is None:
        return None
    try:
        stored_source = _required_nonempty_text(row[0], "Discovery source ID")
        etag = _optional_nonempty_text(row[1], "Discovery ETag")
        last_modified = _optional_nonempty_text(
            row[2],
            "Discovery Last-Modified value",
        )
        if etag is None and last_modified is None:
            raise StorageError("Discovery validators cannot both be empty")
        updated_at = _stored_discovery_timestamp(row[3])
    except StorageError as exc:
        raise StorageError(
            "Discovery database contains invalid HTTP validators"
        ) from exc

    return DiscoveryHttpValidators(
        source_id=stored_source,
        etag=etag,
        last_modified=last_modified,
        updated_at=updated_at,
    )


def store_discovery_http_validators(
    database_file: str | Path,
    source_id: str,
    etag: str | None,
    last_modified: str | None,
    *,
    now: datetime | None = None,
) -> None:
    """Upsert trimmed conditional-request validators for one source."""

    selected_source = _required_nonempty_text(source_id, "Discovery source ID")
    selected_etag = _optional_nonempty_text(etag, "Discovery ETag")
    selected_last_modified = _optional_nonempty_text(
        last_modified,
        "Discovery Last-Modified value",
    )
    if selected_etag is None and selected_last_modified is None:
        raise StorageError("At least one discovery HTTP validator is required")
    updated_at = _discovery_timestamp(
        _discovery_datetime(now, "Discovery validator update time")
    )

    with _database(database_file) as connection:
        with connection:
            connection.execute(
                """
                INSERT INTO discovery_http_validators (
                    source_id,
                    etag,
                    last_modified,
                    updated_at
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT (source_id) DO UPDATE SET
                    etag = excluded.etag,
                    last_modified = excluded.last_modified,
                    updated_at = excluded.updated_at
                """,
                (
                    selected_source,
                    selected_etag,
                    selected_last_modified,
                    updated_at,
                ),
            )


def record_article_read_failure(
    database_file: str | Path,
    source_id: str,
    guid: str,
    url: str | HttpUrl,
    title: str,
    *,
    reason: str,
    error: str,
    retryable: bool,
    max_attempts: int,
    base_retry_seconds: float,
    max_retry_seconds: float,
    now: datetime | None = None,
) -> ArticleReadFailure:
    """Record a body-read failure and return its durable retry state."""

    selected_source = _required_nonempty_text(
        source_id,
        "Article-read failure source ID",
    )
    selected_guid = _required_nonempty_text(
        guid,
        "Article-read failure GUID",
    )
    selected_url = _article_read_failure_url(url)
    selected_title = _required_nonempty_text(
        title,
        "Article-read failure title",
    )
    selected_reason = _article_read_failure_reason(reason)
    selected_error = _sanitize_article_read_failure_error(error)
    if not isinstance(retryable, bool):
        raise StorageError("Article-read retryable flag must be boolean")
    selected_max_attempts = _positive_database_int(
        max_attempts,
        "Article-read maximum attempts",
    )
    base_seconds = _positive_finite_seconds(
        base_retry_seconds,
        "Article-read base retry delay",
    )
    maximum_seconds = _positive_finite_seconds(
        max_retry_seconds,
        "Article-read maximum retry delay",
    )
    if maximum_seconds < base_seconds:
        raise StorageError(
            "Article-read maximum retry delay cannot be less than base delay"
        )
    failed_time = _discovery_datetime(now, "Article-read failure time")
    failed_at = _discovery_timestamp(failed_time)

    with _database(database_file) as connection:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            matching_rows = connection.execute(
                """
                SELECT
                    source_id,
                    guid,
                    url,
                    title,
                    status,
                    reason,
                    attempts,
                    first_failed_at,
                    last_failed_at,
                    next_attempt_at,
                    last_error
                FROM article_read_failures
                WHERE source_id = ?
                  AND (url = ? OR guid = ?)
                ORDER BY url
                """,
                (selected_source, selected_url, selected_guid),
            ).fetchall()
            if len(matching_rows) > 1:
                raise StorageError(
                    "Article-read failure database contains ambiguous identity rows"
                )
            existing = (
                None
                if not matching_rows
                else _article_read_failure_from_row(matching_rows[0])
            )
            if existing is not None and existing.status == "dead":
                return existing

            attempts = 1 if existing is None else existing.attempts + 1
            first_failed_at = (
                failed_at if existing is None else existing.first_failed_at
            )
            if existing is not None:
                prior_failed_time = _parse_article_read_failure_timestamp(
                    existing.last_failed_at,
                    "Article-read last failure time",
                )
                if failed_time < prior_failed_time:
                    raise StorageError(
                        "Article-read failure time cannot precede its prior failure"
                    )

            if not retryable or attempts >= selected_max_attempts:
                status: ArticleReadFailureStatus = "dead"
                next_attempt_at = None
            else:
                status = "retry_wait"
                delay_seconds = _article_read_retry_delay_seconds(
                    attempts,
                    base_seconds=base_seconds,
                    maximum_seconds=maximum_seconds,
                )
                try:
                    retry_time = failed_time + timedelta(seconds=delay_seconds)
                except OverflowError as exc:
                    raise StorageError(
                        "Article-read retry delay is outside the supported range"
                    ) from exc
                next_attempt_at = _discovery_timestamp(retry_time)

            if existing is None:
                connection.execute(
                    """
                    INSERT INTO article_read_failures (
                        source_id,
                        url,
                        guid,
                        title,
                        status,
                        reason,
                        attempts,
                        first_failed_at,
                        last_failed_at,
                        next_attempt_at,
                        last_error
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        selected_source,
                        selected_url,
                        selected_guid,
                        selected_title,
                        status,
                        selected_reason,
                        attempts,
                        first_failed_at,
                        failed_at,
                        next_attempt_at,
                        selected_error,
                    ),
                )
            else:
                connection.execute(
                    """
                    UPDATE article_read_failures
                    SET
                        url = ?,
                        guid = ?,
                        title = ?,
                        status = ?,
                        reason = ?,
                        attempts = ?,
                        last_failed_at = ?,
                        next_attempt_at = ?,
                        last_error = ?
                    WHERE source_id = ? AND url = ?
                    """,
                    (
                        selected_url,
                        selected_guid,
                        selected_title,
                        status,
                        selected_reason,
                        attempts,
                        failed_at,
                        next_attempt_at,
                        selected_error,
                        selected_source,
                        existing.url,
                    ),
                )

            stored_row = connection.execute(
                """
                SELECT
                    source_id,
                    guid,
                    url,
                    title,
                    status,
                    reason,
                    attempts,
                    first_failed_at,
                    last_failed_at,
                    next_attempt_at,
                    last_error
                FROM article_read_failures
                WHERE source_id = ? AND url = ?
                """,
                (selected_source, selected_url),
            ).fetchone()
            if stored_row is None:
                raise StorageError("Article-read failure could not be stored")
            return _article_read_failure_from_row(stored_row)


def get_article_read_failure(
    database_file: str | Path,
    source_id: str,
    guid: str | None = None,
    url: str | HttpUrl | None = None,
) -> ArticleReadFailure | None:
    """Load one current read-failure state by URL, GUID, or both."""

    selected_source, selected_guid, selected_url = _article_read_failure_identity(
        source_id,
        guid,
        url,
    )
    clauses: list[str] = []
    parameters: list[str] = [selected_source]
    if selected_url is not None:
        clauses.append("url = ?")
        parameters.append(selected_url)
    if selected_guid is not None:
        clauses.append("guid = ?")
        parameters.append(selected_guid)

    with _database(database_file) as connection:
        rows = connection.execute(
            f"""
            SELECT
                source_id,
                guid,
                url,
                title,
                status,
                reason,
                attempts,
                first_failed_at,
                last_failed_at,
                next_attempt_at,
                last_error
            FROM article_read_failures
            WHERE source_id = ? AND ({" OR ".join(clauses)})
            ORDER BY url
            """,
            parameters,
        ).fetchall()

    if not rows:
        return None
    if len(rows) > 1:
        raise StorageError(
            "Article-read failure database contains ambiguous identity rows"
        )
    return _article_read_failure_from_row(rows[0])


def load_article_read_failure(
    database_file: str | Path,
    source_id: str,
    guid: str | None = None,
    url: str | HttpUrl | None = None,
) -> ArticleReadFailure | None:
    """Alias for :func:`get_article_read_failure`."""

    return get_article_read_failure(database_file, source_id, guid, url)


def list_article_read_failures(
    database_file: str | Path,
    source_id: str | None = None,
) -> tuple[ArticleReadFailure, ...]:
    """List durable read-failure states in stable source/URL order."""

    if source_id is None:
        selected_source = None
    else:
        selected_source = _required_nonempty_text(
            source_id,
            "Article-read failure source ID",
        )

    with _database(database_file) as connection:
        if selected_source is None:
            rows = connection.execute(
                """
                SELECT
                    source_id,
                    guid,
                    url,
                    title,
                    status,
                    reason,
                    attempts,
                    first_failed_at,
                    last_failed_at,
                    next_attempt_at,
                    last_error
                FROM article_read_failures
                ORDER BY source_id, url
                """
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT
                    source_id,
                    guid,
                    url,
                    title,
                    status,
                    reason,
                    attempts,
                    first_failed_at,
                    last_failed_at,
                    next_attempt_at,
                    last_error
                FROM article_read_failures
                WHERE source_id = ?
                ORDER BY url
                """,
                (selected_source,),
            ).fetchall()

    return tuple(_article_read_failure_from_row(row) for row in rows)


def find_article_read_failures_due(
    database_file: str | Path,
    source_id: str,
    *,
    limit: int = 1,
    now: datetime | None = None,
) -> tuple[ArticleReadFailure, ...]:
    """Return retryable article reads due now, oldest retry time first."""

    selected_source = _required_nonempty_text(
        source_id,
        "Due article-read failure source ID",
    )
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise StorageError(
            "Due article-read failure limit must be a positive integer"
        )
    selected_now = _discovery_timestamp(
        _discovery_datetime(now, "Due article-read failure time")
    )

    with _database(database_file) as connection:
        rows = connection.execute(
            """
            SELECT
                source_id,
                guid,
                url,
                title,
                status,
                reason,
                attempts,
                first_failed_at,
                last_failed_at,
                next_attempt_at,
                last_error
            FROM article_read_failures
            WHERE source_id = ?
              AND status = 'retry_wait'
              AND next_attempt_at <= ?
            ORDER BY next_attempt_at ASC, url ASC
            LIMIT ?
            """,
            (selected_source, selected_now, limit),
        ).fetchall()

    return tuple(_article_read_failure_from_row(row) for row in rows)


def clear_article_read_failure(
    database_file: str | Path,
    source_id: str,
    guid: str | None = None,
    url: str | HttpUrl | None = None,
) -> bool:
    """Delete current read-failure state matching a successful article identity."""

    selected_source, selected_guid, selected_url = _article_read_failure_identity(
        source_id,
        guid,
        url,
    )
    clauses: list[str] = []
    parameters: list[str] = [selected_source]
    if selected_url is not None:
        clauses.append("url = ?")
        parameters.append(selected_url)
    if selected_guid is not None:
        clauses.append("guid = ?")
        parameters.append(selected_guid)

    with _database(database_file) as connection:
        with connection:
            cursor = connection.execute(
                f"""
                DELETE FROM article_read_failures
                WHERE source_id = ? AND ({" OR ".join(clauses)})
                """,
                parameters,
            )
        return cursor.rowcount > 0


def find_articles_pending_analysis(
    database_file: str | Path,
    source_id: str,
    *,
    limit: int = 3,
) -> tuple[StoredArticle, ...]:
    """Return the newest stored articles without an Event resolution."""

    if not isinstance(source_id, str) or not source_id.strip():
        raise StorageError("Pending-analysis source ID cannot be empty")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise StorageError("Pending-analysis limit must be a positive integer")

    selected_source = source_id.strip()
    with _database(database_file) as connection:
        rows = connection.execute(
            """
            SELECT
                article.source_id,
                article.guid,
                article.published_at,
                article.title,
                article.url,
                article.body,
                article.translated_title,
                article.summary
            FROM articles AS article
                WHERE article.source_id = ?
                  AND NOT EXISTS (
                      SELECT 1
                      FROM article_event_resolutions AS resolution
                      WHERE resolution.source_id = article.source_id
                        AND resolution.guid = article.guid
                  )
                ORDER BY
                    article.published_at IS NULL ASC,
                    unixepoch(article.published_at) DESC,
                    CASE
                        WHEN instr(article.published_at, '.') > 0
                        THEN CAST(
                            '0' || substr(
                                article.published_at,
                                instr(article.published_at, '.')
                            ) AS REAL
                        )
                        ELSE 0.0
                    END DESC,
                    article.guid ASC
                LIMIT ?
            """,
            (selected_source, limit),
        ).fetchall()

    try:
        return tuple(
            StoredArticle(
                source_id=row[0],
                guid=row[1],
                published_at=row[2],
                title=row[3],
                url=row[4],
                body=row[5],
                translated_title=row[6],
                summary=row[7],
            )
            for row in rows
        )
    except ValidationError as exc:
        raise StorageError("Article database contains an invalid record") from exc


def schedule_article_analysis_retry(
    database_file: str | Path,
    source_id: str,
    guid: str,
    *,
    now: datetime | None = None,
) -> int:
    """Record a failed analysis for one unresolved stored article."""

    selected_source = _required_nonempty_text(
        source_id,
        "Article-analysis retry source ID",
    )
    selected_guid = _required_nonempty_text(
        guid,
        "Article-analysis retry GUID",
    )
    failed_at = _discovery_timestamp(
        _discovery_datetime(now, "Article-analysis retry time")
    )

    with _database(database_file) as connection:
        with connection:
            row = connection.execute(
                """
                INSERT INTO article_analysis_retries (
                    source_id,
                    guid,
                    attempts,
                    last_failed_at
                )
                SELECT ?, ?, 1, ?
                WHERE EXISTS (
                    SELECT 1
                    FROM articles AS article
                    WHERE article.source_id = ?
                      AND article.guid = ?
                )
                  AND NOT EXISTS (
                    SELECT 1
                    FROM article_event_resolutions AS resolution
                    WHERE resolution.source_id = ?
                      AND resolution.guid = ?
                )
                ON CONFLICT (source_id, guid) DO UPDATE SET
                    attempts = article_analysis_retries.attempts + 1,
                    last_failed_at = excluded.last_failed_at
                RETURNING attempts
                """,
                (
                    selected_source,
                    selected_guid,
                    failed_at,
                    selected_source,
                    selected_guid,
                    selected_source,
                    selected_guid,
                ),
            ).fetchone()
            if row is None:
                raise StorageError(
                    "Cannot schedule analysis retry for a missing or resolved article"
                )
            attempts = row[0]
            if (
                not isinstance(attempts, int)
                or isinstance(attempts, bool)
                or attempts < 1
            ):
                raise StorageError(
                    "Article database contains an invalid analysis retry"
                )

    return attempts


def find_articles_due_analysis_retry(
    database_file: str | Path,
    source_id: str,
    *,
    limit: int = 3,
) -> tuple[StoredArticle, ...]:
    """Return unresolved retries in oldest-failure-first order for fairness."""

    selected_source = _required_nonempty_text(
        source_id,
        "Due-analysis-retry source ID",
    )
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise StorageError("Due-analysis-retry limit must be a positive integer")

    with _database(database_file) as connection:
        rows = connection.execute(
            """
            SELECT
                article.source_id,
                article.guid,
                article.published_at,
                article.title,
                article.url,
                article.body,
                article.translated_title,
                article.summary,
                retry.attempts,
                retry.last_failed_at
            FROM article_analysis_retries AS retry
            JOIN articles AS article
              ON article.source_id = retry.source_id
             AND article.guid = retry.guid
            WHERE retry.source_id = ?
              AND NOT EXISTS (
                  SELECT 1
                  FROM article_event_resolutions AS resolution
                  WHERE resolution.source_id = article.source_id
                    AND resolution.guid = article.guid
              )
            ORDER BY retry.last_failed_at ASC, article.guid ASC
            LIMIT ?
            """,
            (selected_source, limit),
        ).fetchall()

    try:
        articles: list[StoredArticle] = []
        for row in rows:
            _validate_stored_analysis_retry(row[8], row[9])
            articles.append(
                StoredArticle(
                    source_id=row[0],
                    guid=row[1],
                    published_at=row[2],
                    title=row[3],
                    url=row[4],
                    body=row[5],
                    translated_title=row[6],
                    summary=row[7],
                )
            )
        return tuple(articles)
    except ValidationError as exc:
        raise StorageError("Article database contains an invalid record") from exc


def store_article(database_file: str | Path, article: StoredArticle) -> bool:
    """Insert one complete article atomically; return false if it already exists."""

    values = article.model_dump(mode="json")

    with _database(database_file) as connection:
        with connection:
            cursor = connection.execute(
                """
                INSERT INTO articles (
                    source_id,
                    guid,
                    published_at,
                    title,
                    url,
                    body,
                    translated_title,
                    summary
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (source_id, guid) DO NOTHING
                """,
                (
                    values["source_id"],
                    values["guid"],
                    values["published_at"],
                    values["title"],
                    values["url"],
                    values["body"],
                    values["translated_title"],
                    values["summary"],
                ),
            )
            connection.execute(
                """
                DELETE FROM article_read_failures
                WHERE source_id = ? AND (guid = ? OR url = ?)
                """,
                (
                    values["source_id"],
                    values["guid"],
                    values["url"],
                ),
            )
        return cursor.rowcount == 1


def store_article_summary(
    database_file: str | Path,
    source_id: str,
    guid: str,
    *,
    translated_title: str,
    summary: str,
) -> bool:
    """Atomically add one validated translation and summary without overwriting it."""

    selected_source = source_id.strip()
    selected_guid = guid.strip()
    selected_title = translated_title.strip()
    selected_summary = summary.strip()
    if not all((selected_source, selected_guid, selected_title, selected_summary)):
        raise StorageError("Article summary fields cannot be empty")

    with _database(database_file) as connection:
        with connection:
            cursor = connection.execute(
                """
                UPDATE articles
                SET translated_title = ?, summary = ?
                WHERE source_id = ?
                  AND guid = ?
                  AND translated_title IS NULL
                  AND summary IS NULL
                """,
                (
                    selected_title,
                    selected_summary,
                    selected_source,
                    selected_guid,
                ),
            )

        if cursor.rowcount == 1:
            return True

        row = connection.execute(
            """
            SELECT translated_title, summary
            FROM articles
            WHERE source_id = ? AND guid = ?
            """,
            (selected_source, selected_guid),
        ).fetchone()
        if row is None:
            raise StorageError("Cannot summarize an article that is not stored")
        if row[0] is None or row[1] is None:
            raise StorageError("Article database contains an incomplete summary")
        return False


def find_event_candidates(
    database_file: str | Path,
    title: str,
    body: str,
    *,
    limit: int = MAX_EVENT_CANDIDATES,
) -> tuple[EventCandidate, ...]:
    """Return the best matching Events using local FTS5 BM25 ranking."""

    if limit < 1 or limit > MAX_EVENT_CANDIDATES:
        raise StorageError(
            f"Event candidate limit must be between 1 and {MAX_EVENT_CANDIDATES}"
        )

    query = _event_search_query(title, body)
    if query is None:
        return ()

    with _database(database_file) as connection:
        rows = connection.execute(
            """
            SELECT
                CAST(event_id AS INTEGER) AS matched_event_id,
                bm25(event_articles_fts, 0.0, 0.0, 0.0, 8.0, 1.0)
                    AS article_score
            FROM event_articles_fts
            WHERE event_articles_fts MATCH ?
            ORDER BY article_score ASC, matched_event_id ASC
            """,
            (query,),
        ).fetchall()

        event_ids: list[int] = []
        seen_event_ids: set[int] = set()
        for row in rows:
            event_id = int(row[0])
            if event_id in seen_event_ids:
                continue
            seen_event_ids.add(event_id)
            event_ids.append(event_id)
            if len(event_ids) == limit:
                break

        candidates = tuple(
            _load_event_candidate(connection, event_id) for event_id in event_ids
        )

    return candidates


def get_article_event_resolution(
    database_file: str | Path,
    source_id: str,
    guid: str,
) -> StoredEventResolution | None:
    """Load the persisted Event decision for one source-scoped article."""

    with _database(database_file) as connection:
        row = connection.execute(
            """
            SELECT decision, event_id
            FROM article_event_resolutions
            WHERE source_id = ? AND guid = ?
            """,
            (source_id, guid),
        ).fetchone()

    if row is None:
        return None
    return _stored_resolution(row[0], row[1])


def store_article_analysis(
    database_file: str | Path,
    source_id: str,
    guid: str,
    *,
    translated_title: str,
    summary: str,
    decision: EventDecision,
    event_id: int | None,
    notification_targets: Iterable[NotificationTarget] = (),
) -> AnalysisStoreResult:
    """Atomically store analysis, Event data, and a new-Event notification."""

    selected_source = source_id.strip()
    selected_guid = guid.strip()
    selected_title = translated_title.strip()
    selected_summary = summary.strip()
    if not all((selected_source, selected_guid, selected_title, selected_summary)):
        raise StorageError("Article analysis fields cannot be empty")
    _validate_resolution_input(decision, event_id)
    selected_targets = _notification_targets(notification_targets)

    with _database(database_file) as connection:
        with connection:
            article_row = connection.execute(
                """
                SELECT title, body, translated_title, summary, url
                FROM articles
                WHERE source_id = ? AND guid = ?
                """,
                (selected_source, selected_guid),
            ).fetchone()
            if article_row is None:
                raise StorageError("Cannot analyze an article that is not stored")

            existing_resolution_row = connection.execute(
                """
                SELECT decision, event_id
                FROM article_event_resolutions
                WHERE source_id = ? AND guid = ?
                """,
                (selected_source, selected_guid),
            ).fetchone()
            if existing_resolution_row is not None:
                if article_row[2] is None or article_row[3] is None:
                    raise StorageError(
                        "Article database contains an incomplete analysis"
                    )
                connection.execute(
                    """
                    DELETE FROM article_analysis_retries
                    WHERE source_id = ? AND guid = ?
                    """,
                    (selected_source, selected_guid),
                )
                return AnalysisStoreResult(
                    inserted=False,
                    resolution=_stored_resolution(
                        existing_resolution_row[0],
                        existing_resolution_row[1],
                    ),
                )

            if (article_row[2] is None) != (article_row[3] is None):
                raise StorageError("Article database contains an incomplete summary")
            if article_row[2] is None:
                connection.execute(
                    """
                    UPDATE articles
                    SET translated_title = ?, summary = ?
                    WHERE source_id = ?
                      AND guid = ?
                      AND translated_title IS NULL
                      AND summary IS NULL
                    """,
                    (
                        selected_title,
                        selected_summary,
                        selected_source,
                        selected_guid,
                    ),
                )

            timestamp = _utc_timestamp()
            resolved_event_id = event_id
            if decision == "new_event":
                cursor = connection.execute(
                    "INSERT INTO events (created_at) VALUES (?)",
                    (timestamp,),
                )
                resolved_event_id = int(cursor.lastrowid)
            elif decision == "existing_event":
                event_exists = connection.execute(
                    "SELECT 1 FROM events WHERE event_id = ?",
                    (event_id,),
                ).fetchone()
                if event_exists is None:
                    raise StorageError("Selected Event does not exist")

            connection.execute(
                """
                INSERT INTO article_event_resolutions (
                    source_id,
                    guid,
                    decision,
                    event_id,
                    resolved_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    selected_source,
                    selected_guid,
                    decision,
                    resolved_event_id,
                    timestamp,
                ),
            )

            if resolved_event_id is not None:
                connection.execute(
                    """
                    INSERT INTO event_articles_fts (
                        event_id,
                        source_id,
                        guid,
                        title,
                        lead
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        resolved_event_id,
                        selected_source,
                        selected_guid,
                        article_row[0],
                        _article_lead(article_row[1]),
                    ),
                )

            if decision == "new_event":
                assert resolved_event_id is not None
                payload_title = article_row[2] or selected_title
                payload_summary = article_row[3] or selected_summary
                outbox_cursor = connection.execute(
                    """
                    INSERT INTO notification_outbox (
                        event_id,
                        source_id,
                        guid,
                        article_url,
                        translated_title,
                        summary,
                        created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        resolved_event_id,
                        selected_source,
                        selected_guid,
                        article_row[4],
                        payload_title,
                        payload_summary,
                        timestamp,
                    ),
                )
                outbox_id = int(outbox_cursor.lastrowid)
                for target in selected_targets:
                    connection.execute(
                        """
                        INSERT INTO notification_deliveries (
                            outbox_id,
                            target_id,
                            adapter,
                            status,
                            created_at,
                            updated_at
                        )
                        VALUES (?, ?, ?, 'pending', ?, ?)
                        """,
                        (
                            outbox_id,
                            target.target_id,
                            target.adapter,
                            timestamp,
                            timestamp,
                        ),
                    )

            connection.execute(
                """
                DELETE FROM article_analysis_retries
                WHERE source_id = ? AND guid = ?
                """,
                (selected_source, selected_guid),
            )

        return AnalysisStoreResult(
            inserted=True,
            resolution=StoredEventResolution(
                decision=decision,
                event_id=resolved_event_id,
            ),
        )


def claim_notification_delivery(
    database_file: str | Path,
    *,
    target_ids: Iterable[str] | None = None,
    adapter: str | None = None,
    lease_seconds: int = 300,
    now: datetime | None = None,
) -> ClaimedNotificationDelivery | None:
    """Lease the next due delivery, reclaiming an expired lease when necessary."""

    selected_targets = _notification_target_ids(target_ids)
    selected_adapter = _optional_nonempty_text(adapter, "Notification adapter")
    if (
        not isinstance(lease_seconds, int)
        or isinstance(lease_seconds, bool)
        or lease_seconds < 1
    ):
        raise StorageError("Notification lease seconds must be a positive integer")

    current = _notification_datetime(now, "Notification claim time")
    claimed_at = _notification_timestamp(current)
    lease_until = _notification_timestamp(
        current + timedelta(seconds=lease_seconds)
    )
    claim_token = uuid4().hex

    filters = [
        """
        (
            status = 'pending'
            OR (status = 'retry_wait' AND next_attempt_at <= ?)
            OR (status = 'sending' AND lease_until <= ?)
        )
        """
    ]
    parameters: list[object] = [claimed_at, claimed_at]
    if selected_targets is not None:
        if not selected_targets:
            filters.append("0")
        else:
            placeholders = ", ".join("?" for _ in selected_targets)
            filters.append(f"target_id IN ({placeholders})")
            parameters.extend(selected_targets)
    if selected_adapter is not None:
        filters.append("adapter = ?")
        parameters.append(selected_adapter)

    where_clause = " AND ".join(f"({condition.strip()})" for condition in filters)
    parameters.extend((claim_token, lease_until, claimed_at))

    with _database(database_file) as connection, connection:
        cursor = connection.execute(
            f"""
            WITH next_delivery AS (
                SELECT delivery_id
                FROM notification_deliveries
                WHERE {where_clause}
                ORDER BY
                    CASE status
                        WHEN 'pending' THEN created_at
                        WHEN 'retry_wait' THEN next_attempt_at
                        ELSE lease_until
                    END,
                    delivery_id
                LIMIT 1
            )
            UPDATE notification_deliveries
            SET
                status = 'sending',
                attempts = attempts + 1,
                next_attempt_at = NULL,
                claim_token = ?,
                lease_until = ?,
                last_error = NULL,
                updated_at = ?
            WHERE delivery_id = (SELECT delivery_id FROM next_delivery)
            RETURNING delivery_id
            """,
            parameters,
        )
        claimed_row = cursor.fetchone()
        if claimed_row is None:
            return None
        row = connection.execute(
            """
            SELECT
                deliveries.delivery_id,
                deliveries.outbox_id,
                outbox.event_id,
                outbox.source_id,
                outbox.guid,
                outbox.article_url,
                deliveries.target_id,
                deliveries.adapter,
                outbox.translated_title,
                outbox.summary,
                deliveries.attempts,
                deliveries.claim_token,
                deliveries.lease_until
            FROM notification_deliveries AS deliveries
            JOIN notification_outbox AS outbox
                ON outbox.outbox_id = deliveries.outbox_id
            WHERE deliveries.delivery_id = ?
            """,
            (claimed_row[0],),
        ).fetchone()
        if row is None:
            raise StorageError("Notification database lost a claimed delivery")
        return _claimed_notification_delivery(row)


def renew_notification_delivery_lease(
    database_file: str | Path,
    delivery_id: int,
    claim_token: str,
    *,
    lease_seconds: int = 300,
    now: datetime | None = None,
) -> bool:
    """Extend a live sending claim without reviving an expired or stale claim."""

    selected_delivery = _notification_delivery_id(delivery_id)
    selected_token = _required_nonempty_text(claim_token, "Notification claim token")
    if (
        not isinstance(lease_seconds, int)
        or isinstance(lease_seconds, bool)
        or lease_seconds < 1
    ):
        raise StorageError("Notification lease seconds must be a positive integer")

    current = _notification_datetime(now, "Notification lease renewal time")
    renewed_at = _notification_timestamp(current)
    proposed_lease_until = _notification_timestamp(
        current + timedelta(seconds=lease_seconds)
    )

    with _database(database_file) as connection:
        with connection:
            cursor = connection.execute(
                """
                UPDATE notification_deliveries
                SET
                    lease_until = CASE
                        WHEN lease_until < ? THEN ?
                        ELSE lease_until
                    END,
                    updated_at = ?
                WHERE delivery_id = ?
                  AND status = 'sending'
                  AND claim_token = ?
                  AND lease_until > ?
                """,
                (
                    proposed_lease_until,
                    proposed_lease_until,
                    renewed_at,
                    selected_delivery,
                    selected_token,
                    renewed_at,
                ),
            )
        return cursor.rowcount == 1


def ack_notification_delivery(
    database_file: str | Path,
    delivery_id: int,
    claim_token: str,
    *,
    external_receipt_id: str | None = None,
    now: datetime | None = None,
) -> bool:
    """Mark a live claim sent; return false for stale or mismatched claims."""

    selected_delivery = _notification_delivery_id(delivery_id)
    selected_token = _required_nonempty_text(claim_token, "Notification claim token")
    selected_receipt = _optional_nonempty_text(
        external_receipt_id,
        "Notification external receipt ID",
    )
    sent_at = _notification_timestamp(
        _notification_datetime(now, "Notification acknowledgement time")
    )

    with _database(database_file) as connection:
        with connection:
            cursor = connection.execute(
                """
                UPDATE notification_deliveries
                SET
                    status = 'sent',
                    next_attempt_at = NULL,
                    claim_token = NULL,
                    lease_until = NULL,
                    last_error = NULL,
                    external_receipt_id = ?,
                    updated_at = ?,
                    sent_at = ?
                WHERE delivery_id = ?
                  AND status = 'sending'
                  AND claim_token = ?
                  AND lease_until > ?
                """,
                (
                    selected_receipt,
                    sent_at,
                    sent_at,
                    selected_delivery,
                    selected_token,
                    sent_at,
                ),
            )
        return cursor.rowcount == 1


def nack_notification_delivery(
    database_file: str | Path,
    delivery_id: int,
    claim_token: str,
    *,
    error: str,
    external_receipt_id: str | None = None,
    retry_at: datetime | None = None,
    now: datetime | None = None,
) -> bool:
    """Release a live claim into retry_wait, or dead when retry_at is null."""

    selected_delivery = _notification_delivery_id(delivery_id)
    selected_token = _required_nonempty_text(claim_token, "Notification claim token")
    selected_error = _required_nonempty_text(error, "Notification delivery error")
    selected_receipt = _optional_nonempty_text(
        external_receipt_id,
        "Notification external receipt ID",
    )
    current = _notification_datetime(now, "Notification failure time")
    failed_at = _notification_timestamp(current)
    if retry_at is None:
        status: NotificationDeliveryStatus = "dead"
        next_attempt_at = None
    else:
        if selected_receipt is not None:
            raise StorageError(
                "Notification retry cannot retain an external receipt ID"
            )
        retry_time = _notification_datetime(retry_at, "Notification retry time")
        if retry_time <= current:
            raise StorageError("Notification retry time must be after failure time")
        status = "retry_wait"
        next_attempt_at = _notification_timestamp(retry_time)

    with _database(database_file) as connection:
        with connection:
            cursor = connection.execute(
                """
                UPDATE notification_deliveries
                SET
                    status = ?,
                    next_attempt_at = ?,
                    claim_token = NULL,
                    lease_until = NULL,
                    last_error = ?,
                    external_receipt_id = ?,
                    updated_at = ?,
                    sent_at = NULL
                WHERE delivery_id = ?
                  AND status = 'sending'
                  AND claim_token = ?
                  AND lease_until > ?
                """,
                (
                    status,
                    next_attempt_at,
                    selected_error,
                    selected_receipt,
                    failed_at,
                    selected_delivery,
                    selected_token,
                    failed_at,
                ),
            )
        return cursor.rowcount == 1


def get_telegram_update_offset(
    database_file: str | Path,
    target_id: str,
) -> int | None:
    """Return the next Telegram update ID to poll for one notification target."""

    selected_target = _required_nonempty_text(target_id, "Telegram target ID")
    with _database(database_file) as connection:
        row = connection.execute(
            """
            SELECT next_update_id
            FROM telegram_update_offsets
            WHERE target_id = ?
            """,
            (selected_target,),
        ).fetchone()

    if row is None:
        return None
    update_id = row[0]
    if (
        not isinstance(update_id, int)
        or isinstance(update_id, bool)
        or update_id < 1
    ):
        raise StorageError("Feedback database contains an invalid Telegram offset")
    return update_id


def store_telegram_update_offset(
    database_file: str | Path,
    target_id: str,
    next_update_id: int,
    *,
    now: datetime | None = None,
) -> None:
    """Persist the next Telegram update ID after a poll completes."""

    selected_target = _required_nonempty_text(target_id, "Telegram target ID")
    selected_update = _feedback_update_id(next_update_id, "Telegram update offset")
    updated_at = _notification_timestamp(
        _notification_datetime(now, "Telegram offset update time")
    )

    with _database(database_file) as connection:
        with connection:
            connection.execute(
                """
                INSERT INTO telegram_update_offsets (
                    target_id,
                    next_update_id,
                    updated_at
                )
                VALUES (?, ?, ?)
                ON CONFLICT (target_id) DO UPDATE SET
                    next_update_id = excluded.next_update_id,
                    updated_at = excluded.updated_at
                """,
                (selected_target, selected_update, updated_at),
            )


def find_telegram_delivery_match(
    database_file: str | Path,
    target_id: str,
    message_id: str | int,
) -> TelegramDeliveryMatch | None:
    """Map a Telegram message ID back to its notification delivery payload."""

    selected_target = _required_nonempty_text(target_id, "Telegram target ID")
    selected_message = _required_nonempty_text(
        str(message_id),
        "Telegram message ID",
    )
    with _database(database_file) as connection:
        rows = connection.execute(
            """
            SELECT
                deliveries.delivery_id,
                outbox.event_id,
                outbox.source_id,
                outbox.guid,
                outbox.article_url,
                deliveries.external_receipt_id
            FROM notification_deliveries AS deliveries
            JOIN notification_outbox AS outbox
                ON outbox.outbox_id = deliveries.outbox_id
            WHERE deliveries.target_id = ?
              AND deliveries.adapter = 'telegram'
              AND deliveries.external_receipt_id IS NOT NULL
            ORDER BY deliveries.delivery_id DESC
            """,
            (selected_target,),
        ).fetchall()

    for row in rows:
        receipt_ids = _notification_receipt_ids(row[5])
        if selected_message not in receipt_ids:
            continue
        return _telegram_delivery_match(row)
    return None


def store_feedback_observation(
    database_file: str | Path,
    observation: FeedbackObservation,
    *,
    now: datetime | None = None,
) -> bool:
    """Store a feedback observation and update current message feedback state."""

    selected = _feedback_observation(observation)
    observed_at = _notification_timestamp(
        _notification_datetime(now, "Feedback observation time")
    )

    with _database(database_file) as connection:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                INSERT INTO feedback_observations (
                    update_id,
                    observed_at,
                    target_id,
                    chat_id,
                    message_id,
                    delivery_id,
                    event_id,
                    source_id,
                    guid,
                    article_url,
                    reaction_emoji,
                    signal,
                    raw_old_reaction,
                    raw_new_reaction
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (update_id) DO NOTHING
                """,
                (
                    selected.update_id,
                    observed_at,
                    selected.target_id,
                    selected.chat_id,
                    selected.message_id,
                    selected.delivery_id,
                    selected.event_id,
                    selected.source_id,
                    selected.guid,
                    selected.article_url,
                    selected.reaction_emoji,
                    selected.signal,
                    selected.raw_old_reaction,
                    selected.raw_new_reaction,
                ),
            )
            if cursor.rowcount != 1:
                return False

            if selected.signal == "cleared":
                connection.execute(
                    """
                    DELETE FROM message_feedback_state
                    WHERE target_id = ?
                      AND chat_id = ?
                      AND message_id = ?
                    """,
                    (selected.target_id, selected.chat_id, selected.message_id),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO message_feedback_state (
                        target_id,
                        chat_id,
                        message_id,
                        delivery_id,
                        event_id,
                        source_id,
                        guid,
                        article_url,
                        reaction_emoji,
                        signal,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (target_id, chat_id, message_id) DO UPDATE SET
                        delivery_id = excluded.delivery_id,
                        event_id = excluded.event_id,
                        source_id = excluded.source_id,
                        guid = excluded.guid,
                        article_url = excluded.article_url,
                        reaction_emoji = excluded.reaction_emoji,
                        signal = excluded.signal,
                        updated_at = excluded.updated_at
                    """,
                    (
                        selected.target_id,
                        selected.chat_id,
                        selected.message_id,
                        selected.delivery_id,
                        selected.event_id,
                        selected.source_id,
                        selected.guid,
                        selected.article_url,
                        selected.reaction_emoji,
                        selected.signal,
                        observed_at,
                    ),
                )
    return True


def get_feedback_memory_items(
    database_file: str | Path,
    *,
    limit: int = 50,
) -> tuple[FeedbackMemoryItem, ...]:
    """Return current feedback states, newest first, for memory rendering."""

    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise StorageError("Feedback memory limit must be a positive integer")

    with _database(database_file) as connection:
        rows = connection.execute(
            """
            SELECT
                state.signal,
                state.reaction_emoji,
                state.updated_at,
                state.event_id,
                state.source_id,
                state.guid,
                state.article_url,
                articles.title
            FROM message_feedback_state AS state
            JOIN articles
              ON articles.source_id = state.source_id
             AND articles.guid = state.guid
            ORDER BY state.updated_at DESC, state.target_id, state.message_id
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    return tuple(_feedback_memory_item(row) for row in rows)


def get_article(
    database_file: str | Path,
    source_id: str,
    guid: str,
) -> StoredArticle | None:
    """Load one complete article by its source-scoped RSS identity."""

    with _database(database_file) as connection:
        row = connection.execute(
            """
            SELECT
                source_id,
                guid,
                published_at,
                title,
                url,
                body,
                translated_title,
                summary
            FROM articles
            WHERE source_id = ? AND guid = ?
            """,
            (source_id, guid),
        ).fetchone()

    if row is None:
        return None

    try:
        return StoredArticle(
            source_id=row[0],
            guid=row[1],
            published_at=row[2],
            title=row[3],
            url=row[4],
            body=row[5],
            translated_title=row[6],
            summary=row[7],
        )
    except ValidationError as exc:
        raise StorageError("Article database contains an invalid record") from exc


def get_article_by_url(
    database_file: str | Path,
    source_id: str,
    url: str,
) -> StoredArticle | None:
    """Load one article by canonical URL for discovery-identity migrations."""

    with _database(database_file) as connection:
        row = connection.execute(
            """
            SELECT
                source_id,
                guid,
                published_at,
                title,
                url,
                body,
                translated_title,
                summary
            FROM articles
            WHERE source_id = ? AND url = ?
            ORDER BY rowid
            LIMIT 1
            """,
            (source_id, url),
        ).fetchone()

    if row is None:
        return None

    try:
        return StoredArticle(
            source_id=row[0],
            guid=row[1],
            published_at=row[2],
            title=row[3],
            url=row[4],
            body=row[5],
            translated_title=row[6],
            summary=row[7],
        )
    except ValidationError as exc:
        raise StorageError("Article database contains an invalid record") from exc


def _unique_items(items: Iterable[DiscoveredItem]) -> tuple[DiscoveredItem, ...]:
    if isinstance(items, (str, bytes)):
        raise StorageError("Discoveries must be an iterable of discovered items")
    try:
        selected_items = tuple(items)
    except TypeError as exc:
        raise StorageError(
            "Discoveries must be an iterable of discovered items"
        ) from exc

    by_guid: dict[str, DiscoveredItem] = {}
    for item in selected_items:
        if not isinstance(item, DiscoveredItem):
            raise StorageError("Discoveries must contain only discovered items")
        by_guid[item.guid] = item

    by_url: dict[str, DiscoveredItem] = {}
    for item in by_guid.values():
        by_url[str(item.url)] = item
    return tuple(by_url.values())


def _selection_items(
    items: Iterable[DiscoveredItem],
) -> tuple[DiscoveredItem, ...]:
    selected_items = _unique_items(items)
    for item in selected_items:
        published_at = item.published_at
        if published_at is None:
            continue
        if not isinstance(published_at, datetime):
            raise StorageError("Discovery publication time must be a datetime or null")
        if published_at.tzinfo is None or published_at.utcoffset() is None:
            raise StorageError("Discovery publication time must include a timezone")
    return selected_items


def _discovery_sort_key(
    item: DiscoveredItem,
) -> tuple[int, int, int, int, int, int, str]:
    published_at = item.published_at
    if published_at is None:
        return (1, 0, 0, 0, 0, 0, item.guid)
    utc_value = published_at.astimezone(UTC)
    return (
        0,
        -utc_value.toordinal(),
        -utc_value.hour,
        -utc_value.minute,
        -utc_value.second,
        -utc_value.microsecond,
        item.guid,
    )


def _discovery_identity_exists(
    connection: sqlite3.Connection,
    source_id: str,
    item: DiscoveredItem,
) -> bool:
    row = connection.execute(
        """
        SELECT 1
        FROM articles
        WHERE source_id = ?
          AND (guid = ? OR url = ?)
        UNION ALL
        SELECT 1
        FROM discovery_skips
        WHERE source_id = ?
          AND (guid = ? OR url = ?)
        UNION ALL
        SELECT 1
        FROM article_read_failures
        WHERE source_id = ?
          AND (guid = ? OR url = ?)
        LIMIT 1
        """,
        (
            source_id,
            item.guid,
            str(item.url),
            source_id,
            item.guid,
            str(item.url),
            source_id,
            item.guid,
            str(item.url),
        ),
    ).fetchone()
    return row is not None


def _browser_candidate_items(
    candidates: Iterable[ArticleCandidate],
) -> tuple[ArticleCandidate, ...]:
    if isinstance(candidates, (str, bytes)):
        raise StorageError("Candidates must be an iterable of article candidates")
    try:
        selected_candidates = tuple(candidates)
    except TypeError as exc:
        raise StorageError(
            "Candidates must be an iterable of article candidates"
        ) from exc

    by_url: dict[str, ArticleCandidate] = {}
    for candidate in selected_candidates:
        if not isinstance(candidate, ArticleCandidate):
            raise StorageError("Candidates must contain only article candidates")
        by_url[str(candidate.url)] = candidate
    return tuple(by_url.values())


def _candidate_row(
    source_id: str,
    candidate: ArticleCandidate,
    timestamp: str,
) -> tuple[str, str, str, str, str, str, str | None, str, str]:
    payload = candidate.model_dump(mode="json")
    return (
        source_id,
        payload["url"],
        payload["title"],
        payload["section_url"],
        payload["section_title"],
        payload["visible_text"],
        payload["published_at"],
        timestamp,
        timestamp,
    )


def _discovery_candidate_is_suppressed(
    connection: sqlite3.Connection,
    source_id: str,
    candidate: ArticleCandidate,
    *,
    now: str,
) -> bool:
    url = str(candidate.url)
    row = connection.execute(
        """
        SELECT 1
        FROM articles
        WHERE source_id = ?
          AND url = ?
        UNION ALL
        SELECT 1
        FROM discovery_skips
        WHERE source_id = ?
          AND url = ?
        UNION ALL
        SELECT 1
        FROM article_read_failures
        WHERE source_id = ?
          AND url = ?
        LIMIT 1
        """,
        (source_id, url, source_id, url, source_id, url),
    ).fetchone()
    if row is not None:
        return True

    cooldown_row = connection.execute(
        """
        SELECT rejected_until
        FROM discovery_candidates
        WHERE source_id = ? AND url = ?
        """,
        (source_id, url),
    ).fetchone()
    return (
        cooldown_row is not None
        and cooldown_row[0] is not None
        and cooldown_row[0] > now
    )


def _discovery_datetime(value: datetime | None, label: str) -> datetime:
    selected = datetime.now(UTC) if value is None else value
    if not isinstance(selected, datetime):
        raise StorageError(f"{label} must be a datetime")
    if selected.tzinfo is None or selected.utcoffset() is None:
        raise StorageError(f"{label} must include a timezone")
    return selected.astimezone(UTC)


def _discovery_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace(
        "+00:00",
        "Z",
    )


def _discovery_published_timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _discovery_timestamp(value)


def _stored_discovery_timestamp(value: object) -> str:
    selected = _required_nonempty_text(value, "Discovery validator update time")
    normalized = selected[:-1] + "+00:00" if selected.endswith("Z") else selected
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise StorageError(
            "Discovery validator update time must be an ISO 8601 datetime"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise StorageError("Discovery validator update time must include a timezone")
    return _discovery_timestamp(parsed)


def _article_read_failure_identity(
    source_id: str,
    guid: str | None,
    url: str | HttpUrl | None,
) -> tuple[str, str | None, str | None]:
    selected_source = _required_nonempty_text(
        source_id,
        "Article-read failure source ID",
    )
    selected_guid = _optional_nonempty_text(
        guid,
        "Article-read failure GUID",
    )
    selected_url = None if url is None else _article_read_failure_url(url)
    if selected_guid is None and selected_url is None:
        raise StorageError("Article-read failure GUID or URL is required")
    return selected_source, selected_guid, selected_url


def _article_read_failure_url(value: object) -> str:
    try:
        return str(HttpUrl(value))
    except (TypeError, ValueError, ValidationError) as exc:
        raise StorageError("Article-read failure URL is invalid") from exc


def _article_read_failure_reason(value: object) -> str:
    selected = _required_nonempty_text(value, "Article-read failure reason")
    normalized = " ".join(selected.split())
    if len(normalized) > _MAX_READ_FAILURE_REASON_LENGTH:
        raise StorageError(
            "Article-read failure reason exceeds the supported length"
        )
    return normalized


def _sanitize_article_read_failure_error(value: object) -> str:
    selected = _required_nonempty_text(value, "Article-read failure error")
    normalized = " ".join(selected.split())
    if len(normalized) <= _MAX_READ_FAILURE_ERROR_LENGTH:
        return normalized
    shortened = normalized[: _MAX_READ_FAILURE_ERROR_LENGTH - 1].rstrip()
    return f"{shortened}…"


def _positive_finite_seconds(value: object, label: str) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not 0 < float(value) < float("inf")
    ):
        raise StorageError(f"{label} must be positive and finite")
    return float(value)


def _article_read_retry_delay_seconds(
    attempts: int,
    *,
    base_seconds: float,
    maximum_seconds: float,
) -> float:
    delay = base_seconds
    remaining_doublings = attempts - 1
    while remaining_doublings > 0 and delay < maximum_seconds:
        if delay >= maximum_seconds / 2:
            return maximum_seconds
        delay *= 2
        remaining_doublings -= 1
    return min(delay, maximum_seconds)


def _parse_article_read_failure_timestamp(value: object, label: str) -> datetime:
    selected = _required_nonempty_text(value, label)
    normalized = selected[:-1] + "+00:00" if selected.endswith("Z") else selected
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise StorageError(f"{label} must be an ISO 8601 datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise StorageError(f"{label} must include a timezone")
    return parsed.astimezone(UTC)


def _stored_article_read_failure_timestamp(value: object, label: str) -> str:
    selected = _required_nonempty_text(value, label)
    canonical = _discovery_timestamp(
        _parse_article_read_failure_timestamp(selected, label)
    )
    if selected != canonical:
        raise StorageError(f"{label} is not stored in canonical UTC form")
    return canonical


def _article_read_failure_status(value: object) -> ArticleReadFailureStatus:
    if value not in {"retry_wait", "dead"}:
        raise StorageError("Article-read failure status is invalid")
    return value


def _article_read_failure_from_row(
    row: sqlite3.Row | tuple[object, ...],
) -> ArticleReadFailure:
    try:
        source_id = _required_nonempty_text(
            row[0],
            "Article-read failure source ID",
        )
        guid = _required_nonempty_text(row[1], "Article-read failure GUID")
        url = _article_read_failure_url(row[2])
        title = _required_nonempty_text(row[3], "Article-read failure title")
        status = _article_read_failure_status(row[4])
        reason = _article_read_failure_reason(row[5])
        attempts = _positive_database_int(
            row[6],
            "Article-read failure attempts",
        )
        first_failed_at = _stored_article_read_failure_timestamp(
            row[7],
            "Article-read first failure time",
        )
        last_failed_at = _stored_article_read_failure_timestamp(
            row[8],
            "Article-read last failure time",
        )
        next_attempt_at = (
            None
            if row[9] is None
            else _stored_article_read_failure_timestamp(
                row[9],
                "Article-read next attempt time",
            )
        )
        last_error = _sanitize_article_read_failure_error(row[10])
        if (
            row[0] != source_id
            or row[1] != guid
            or row[2] != url
            or row[3] != title
            or row[5] != reason
            or row[10] != last_error
        ):
            raise StorageError("Article-read failure fields are not normalized")

        first_time = _parse_article_read_failure_timestamp(
            first_failed_at,
            "Article-read first failure time",
        )
        last_time = _parse_article_read_failure_timestamp(
            last_failed_at,
            "Article-read last failure time",
        )
        if first_time > last_time:
            raise StorageError("Article-read failure timestamps are inconsistent")
        if status == "retry_wait":
            if next_attempt_at is None:
                raise StorageError("Article-read retry is missing its next attempt")
            retry_time = _parse_article_read_failure_timestamp(
                next_attempt_at,
                "Article-read next attempt time",
            )
            if retry_time <= last_time:
                raise StorageError(
                    "Article-read next attempt must follow its last failure"
                )
        elif next_attempt_at is not None:
            raise StorageError("Dead article-read failure cannot retain a retry time")
    except (IndexError, TypeError, ValueError, ValidationError, StorageError) as exc:
        raise StorageError(
            "Article database contains an invalid read-failure record"
        ) from exc

    return ArticleReadFailure(
        source_id=source_id,
        guid=guid,
        url=url,
        title=title,
        status=status,
        reason=reason,
        attempts=attempts,
        first_failed_at=first_failed_at,
        last_failed_at=last_failed_at,
        next_attempt_at=next_attempt_at,
        last_error=last_error,
    )


def _validate_stored_analysis_retry(
    attempts: object,
    last_failed_at: object,
) -> None:
    if (
        not isinstance(attempts, int)
        or isinstance(attempts, bool)
        or attempts < 1
    ):
        raise StorageError("Article database contains an invalid analysis retry")

    timestamp = _required_nonempty_text(
        last_failed_at,
        "Article-analysis retry timestamp",
    )
    normalized = timestamp[:-1] + "+00:00" if timestamp.endswith("Z") else timestamp
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise StorageError(
            "Article-analysis retry timestamp must be an ISO 8601 datetime"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise StorageError(
            "Article-analysis retry timestamp must include a timezone"
        )


def _event_search_query(title: str, body: str) -> str | None:
    searchable_text = f"{title}\n{_article_lead(body)}"
    terms: list[str] = []
    seen: set[str] = set()
    for match in _WORD_PATTERN.finditer(searchable_text):
        term = match.group(0).casefold()
        if len(term) < 2 and not term.isdigit():
            continue
        if term in seen:
            continue
        seen.add(term)
        terms.append(term)
        if len(terms) == _MAX_FTS_QUERY_TERMS:
            break

    if not terms:
        return None
    return " OR ".join(f'"{term}"' for term in terms)


def _article_lead(body: str) -> str:
    paragraphs = (paragraph.strip() for paragraph in body.split("\n\n"))
    return next((paragraph for paragraph in paragraphs if paragraph), body.strip())


def _load_event_candidate(
    connection: sqlite3.Connection,
    event_id: int,
) -> EventCandidate:
    rows = connection.execute(
        """
        SELECT
            articles.title,
            articles.translated_title,
            articles.summary,
            articles.published_at,
            article_event_resolutions.resolved_at
        FROM article_event_resolutions
        JOIN articles USING (source_id, guid)
        WHERE article_event_resolutions.event_id = ?
        ORDER BY
            COALESCE(articles.published_at, article_event_resolutions.resolved_at),
            article_event_resolutions.resolved_at,
            articles.source_id,
            articles.guid
        """,
        (event_id,),
    ).fetchall()
    if not rows:
        raise StorageError("Event database contains an Event without articles")
    if any(row[1] is None or row[2] is None for row in rows):
        raise StorageError("Event database contains an incomplete article analysis")

    observed_at = [row[3] or row[4] for row in rows]
    return EventCandidate(
        event_id=event_id,
        first_seen_at=observed_at[0],
        last_seen_at=observed_at[-1],
        linked_article_titles=tuple(
            EventArticleTitle(
                original_title=row[0],
                translated_title=row[1],
            )
            for row in rows
        ),
        latest_article_summary=rows[-1][2],
    )


def _stored_resolution(
    decision: object,
    event_id: object,
) -> StoredEventResolution:
    if decision not in {"new_event", "existing_event", "non_event"}:
        raise StorageError("Event database contains an invalid decision")
    if decision == "non_event":
        if event_id is not None:
            raise StorageError("Event database contains an invalid resolution")
        return StoredEventResolution(decision="non_event", event_id=None)
    if not isinstance(event_id, int) or event_id < 1:
        raise StorageError("Event database contains an invalid resolution")
    return StoredEventResolution(decision=decision, event_id=event_id)


def _validate_resolution_input(
    decision: object,
    event_id: object,
) -> None:
    if decision not in {"new_event", "existing_event", "non_event"}:
        raise StorageError("Article Event decision is invalid")
    if decision == "existing_event":
        if not isinstance(event_id, int) or isinstance(event_id, bool) or event_id < 1:
            raise StorageError("Existing Event decision requires a valid Event ID")
        return
    if event_id is not None:
        raise StorageError("New or non-Event decisions cannot include an Event ID")


def _notification_targets(
    targets: Iterable[NotificationTarget],
) -> tuple[NotificationTarget, ...]:
    if isinstance(targets, (str, bytes)):
        raise StorageError("Notification targets must be target descriptors")
    try:
        selected = tuple(targets)
    except TypeError as exc:
        raise StorageError("Notification targets must be target descriptors") from exc

    target_ids: set[str] = set()
    for target in selected:
        if not isinstance(target, NotificationTarget):
            raise StorageError("Notification targets must be target descriptors")
        if target.target_id in target_ids:
            raise StorageError("Notification target IDs must be unique")
        target_ids.add(target.target_id)
    return selected


def _notification_target_ids(
    target_ids: Iterable[str] | None,
) -> tuple[str, ...] | None:
    if target_ids is None:
        return None
    if isinstance(target_ids, (str, bytes)):
        raise StorageError("Notification target IDs must be an iterable of strings")
    try:
        values = tuple(target_ids)
    except TypeError as exc:
        raise StorageError(
            "Notification target IDs must be an iterable of strings"
        ) from exc

    selected: list[str] = []
    seen: set[str] = set()
    for value in values:
        target_id = _required_nonempty_text(value, "Notification target ID")
        if target_id not in seen:
            seen.add(target_id)
            selected.append(target_id)
    return tuple(selected)


def _required_nonempty_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StorageError(f"{label} cannot be empty")
    return value.strip()


def _optional_nonempty_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _required_nonempty_text(value, label)


def _notification_delivery_id(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise StorageError("Notification delivery ID must be a positive integer")
    return value


def _notification_datetime(value: datetime | None, label: str) -> datetime:
    selected = datetime.now(UTC) if value is None else value
    if not isinstance(selected, datetime):
        raise StorageError(f"{label} must be a datetime")
    if selected.tzinfo is None or selected.utcoffset() is None:
        raise StorageError(f"{label} must include a timezone")
    return selected.astimezone(UTC)


def _notification_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace(
        "+00:00",
        "Z",
    )


def _claimed_notification_delivery(
    row: sqlite3.Row | tuple[object, ...],
) -> ClaimedNotificationDelivery:
    try:
        (
            delivery_id,
            outbox_id,
            event_id,
            source_id,
            guid,
            article_url,
            target_id,
            adapter,
            translated_title,
            summary,
            attempt,
            claim_token,
            lease_until,
        ) = row
        if not all(
            isinstance(value, int) and not isinstance(value, bool) and value >= 1
            for value in (delivery_id, outbox_id, event_id, attempt)
        ):
            raise ValueError
        target = NotificationTarget(target_id=target_id, adapter=adapter)
        selected_source = _required_nonempty_text(
            source_id,
            "Notification source ID",
        )
        selected_guid = _required_nonempty_text(guid, "Notification article GUID")
        selected_article_url = str(HttpUrl(article_url))
        selected_title = _required_nonempty_text(
            translated_title,
            "Notification translated title",
        )
        selected_summary = _required_nonempty_text(
            summary,
            "Notification summary",
        )
        selected_token = _required_nonempty_text(
            claim_token,
            "Notification claim token",
        )
        selected_lease = _required_nonempty_text(
            lease_until,
            "Notification lease",
        )
    except (TypeError, ValueError, ValidationError) as exc:
        raise StorageError("Notification database contains an invalid claim") from exc

    return ClaimedNotificationDelivery(
        delivery_id=delivery_id,
        outbox_id=outbox_id,
        event_id=event_id,
        source_id=selected_source,
        guid=selected_guid,
        article_url=selected_article_url,
        target=target,
        translated_title=selected_title,
        summary=selected_summary,
        attempt=attempt,
        claim_token=selected_token,
        lease_until=selected_lease,
    )


def _notification_receipt_ids(value: object) -> tuple[str, ...]:
    selected = _required_nonempty_text(value, "Notification receipt ID")
    try:
        payload = json.loads(selected)
    except (TypeError, ValueError) as exc:
        raise StorageError(
            "Notification database contains an invalid receipt ID"
        ) from exc

    if isinstance(payload, str):
        values: tuple[object, ...] = (payload,)
    elif isinstance(payload, list):
        values = tuple(payload)
    else:
        raise StorageError("Notification database contains an invalid receipt ID")

    receipt_ids: list[str] = []
    for item in values:
        if isinstance(item, bool) or not isinstance(item, (int, str)):
            raise StorageError(
                "Notification database contains an invalid receipt ID"
            )
        receipt_id = _required_nonempty_text(
            str(item),
            "Notification receipt ID",
        )
        receipt_ids.append(receipt_id)
    return tuple(receipt_ids)


def _telegram_delivery_match(
    row: sqlite3.Row | tuple[object, ...],
) -> TelegramDeliveryMatch:
    try:
        delivery_id = _positive_database_int(row[0], "Notification delivery ID")
        event_id = _positive_database_int(row[1], "Notification event ID")
        source_id = _required_nonempty_text(row[2], "Notification source ID")
        guid = _required_nonempty_text(row[3], "Notification article GUID")
        article_url = str(HttpUrl(row[4]))
    except (TypeError, ValueError, ValidationError) as exc:
        raise StorageError(
            "Notification database contains an invalid Telegram receipt"
        ) from exc

    return TelegramDeliveryMatch(
        delivery_id=delivery_id,
        event_id=event_id,
        source_id=source_id,
        guid=guid,
        article_url=article_url,
    )


def _feedback_observation(observation: FeedbackObservation) -> FeedbackObservation:
    if not isinstance(observation, FeedbackObservation):
        raise StorageError("Feedback observation must be a feedback record")
    try:
        article_url = str(HttpUrl(observation.article_url))
    except ValidationError as exc:
        raise StorageError("Feedback observation article URL is invalid") from exc
    signal = _feedback_signal(observation.signal)
    return FeedbackObservation(
        update_id=_feedback_update_id(
            observation.update_id,
            "Telegram update ID",
        ),
        target_id=_required_nonempty_text(
            observation.target_id,
            "Feedback target ID",
        ),
        chat_id=_required_nonempty_text(observation.chat_id, "Feedback chat ID"),
        message_id=_required_nonempty_text(
            observation.message_id,
            "Feedback message ID",
        ),
        delivery_id=_positive_database_int(
            observation.delivery_id,
            "Feedback delivery ID",
        ),
        event_id=_positive_database_int(
            observation.event_id,
            "Feedback event ID",
        ),
        source_id=_required_nonempty_text(
            observation.source_id,
            "Feedback source ID",
        ),
        guid=_required_nonempty_text(observation.guid, "Feedback article GUID"),
        article_url=article_url,
        reaction_emoji=_required_nonempty_text(
            observation.reaction_emoji,
            "Feedback reaction emoji",
        ),
        signal=signal,
        raw_old_reaction=_required_nonempty_text(
            observation.raw_old_reaction,
            "Feedback old reaction payload",
        ),
        raw_new_reaction=_required_nonempty_text(
            observation.raw_new_reaction,
            "Feedback new reaction payload",
        ),
    )


def _feedback_signal(value: object) -> FeedbackSignal:
    if value not in {
        "more_like_this",
        "less_like_this",
        "strong_negative",
        "cleared",
    }:
        raise StorageError("Feedback signal is invalid")
    return value


def _feedback_update_id(value: object, label: str) -> int:
    return _positive_database_int(value, label)


def _positive_database_int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise StorageError(f"{label} must be a positive integer")
    return value


def _feedback_memory_item(
    row: sqlite3.Row | tuple[object, ...],
) -> FeedbackMemoryItem:
    try:
        signal = _feedback_signal(row[0])
        if signal == "cleared":
            raise ValueError
        reaction_emoji = _required_nonempty_text(row[1], "Feedback reaction emoji")
        updated_at = _stored_discovery_timestamp(row[2])
        event_id = _positive_database_int(row[3], "Feedback event ID")
        source_id = _required_nonempty_text(row[4], "Feedback source ID")
        guid = _required_nonempty_text(row[5], "Feedback article GUID")
        article_url = str(HttpUrl(row[6]))
        title = _required_nonempty_text(row[7], "Feedback article title")
    except (TypeError, ValueError, ValidationError) as exc:
        raise StorageError("Feedback database contains an invalid memory item") from exc

    return FeedbackMemoryItem(
        signal=signal,
        reaction_emoji=reaction_emoji,
        updated_at=updated_at,
        event_id=event_id,
        source_id=source_id,
        guid=guid,
        article_url=article_url,
        title=title,
    )


def _utc_timestamp() -> str:
    return _notification_timestamp(datetime.now(UTC))


@contextmanager
def _database(database_file: str | Path) -> Iterator[sqlite3.Connection]:
    path = Path(database_file)
    connection: sqlite3.Connection | None = None

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path)
        connection.execute("PRAGMA foreign_keys = ON")
        _validate_analysis_retry_schema(connection, path)
        _validate_article_read_failure_schema(connection, path)
        _validate_discovery_schema(connection, path)
        _validate_existing_notification_schema(connection, path)
        _validate_feedback_schema(connection, path)
        with connection:
            connection.execute(_CREATE_ARTICLES_TABLE)
            _migrate_articles_schema(connection)
        _validate_schema(connection, path)
        _validate_existing_event_schema(connection, path)
        _validate_existing_notification_schema(connection, path)
        with connection:
            connection.execute(_CREATE_EVENTS_TABLE)
            connection.execute(_CREATE_ARTICLE_EVENT_RESOLUTIONS_TABLE)
            connection.execute(_CREATE_ARTICLE_ANALYSIS_RETRIES_TABLE)
            connection.execute(_CREATE_ARTICLE_READ_FAILURES_TABLE)
            connection.execute(_CREATE_EVENT_ARTICLES_FTS)
            connection.execute(_CREATE_NOTIFICATION_OUTBOX_TABLE)
            connection.execute(_CREATE_NOTIFICATION_DELIVERIES_TABLE)
            connection.execute(_CREATE_DISCOVERY_SKIPS_TABLE)
            connection.execute(_CREATE_DISCOVERY_HTTP_VALIDATORS_TABLE)
            connection.execute(_CREATE_DISCOVERY_CANDIDATES_TABLE)
            connection.execute(_CREATE_TELEGRAM_UPDATE_OFFSETS_TABLE)
            connection.execute(_CREATE_FEEDBACK_OBSERVATIONS_TABLE)
            connection.execute(_CREATE_MESSAGE_FEEDBACK_STATE_TABLE)
        _validate_event_schema(connection, path)
        _validate_analysis_retry_schema(connection, path)
        _validate_article_read_failure_schema(connection, path)
        _validate_notification_schema(connection, path)
        _validate_discovery_schema(connection, path)
        _validate_feedback_schema(connection, path)
        yield connection
    except sqlite3.Error as exc:
        raise StorageError(f"Article database cannot be used: {path}") from exc
    except OSError as exc:
        raise StorageError(f"Article database path cannot be used: {path}") from exc
    finally:
        if connection is not None:
            connection.close()


def _migrate_articles_schema(connection: sqlite3.Connection) -> None:
    rows = connection.execute("PRAGMA table_info(articles)").fetchall()
    actual = tuple((row[1], row[2].upper(), row[3], row[5]) for row in rows)
    if actual != _LEGACY_COLUMNS:
        return

    connection.execute("ALTER TABLE articles ADD COLUMN translated_title TEXT")
    connection.execute("ALTER TABLE articles ADD COLUMN summary TEXT")


def _validate_schema(connection: sqlite3.Connection, path: Path) -> None:
    rows = connection.execute("PRAGMA table_info(articles)").fetchall()
    actual = tuple((row[1], row[2].upper(), row[3], row[5]) for row in rows)
    if actual != _EXPECTED_COLUMNS:
        raise StorageError(f"Article database has an incompatible schema: {path}")


def _validate_existing_event_schema(
    connection: sqlite3.Connection,
    path: Path,
) -> None:
    event_objects = (
        "events",
        "article_event_resolutions",
        "event_articles_fts",
    )
    existing_objects = {
        row[0]
        for row in connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table'
              AND name IN (?, ?, ?)
            """,
            event_objects,
        ).fetchall()
    }
    if existing_objects and existing_objects != set(event_objects):
        raise StorageError(f"Event database has an incomplete schema: {path}")

    _validate_optional_table(
        connection,
        path,
        "events",
        _EXPECTED_EVENTS_COLUMNS,
        _CREATE_EVENTS_TABLE,
    )
    _validate_optional_table(
        connection,
        path,
        "article_event_resolutions",
        _EXPECTED_RESOLUTION_COLUMNS,
        _CREATE_ARTICLE_EVENT_RESOLUTIONS_TABLE,
    )

    fts_row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        ("event_articles_fts",),
    ).fetchone()
    if fts_row is None:
        return
    columns = tuple(
        row[1]
        for row in connection.execute(
            "PRAGMA table_info(event_articles_fts)"
        ).fetchall()
    )
    if (
        columns != _EXPECTED_FTS_COLUMNS
        or _normalized_schema_sql(fts_row[0])
        != _normalized_schema_sql(_CREATE_EVENT_ARTICLES_FTS)
    ):
        raise StorageError(f"Event database has an incompatible schema: {path}")


def _validate_optional_table(
    connection: sqlite3.Connection,
    path: Path,
    table_name: str,
    expected: tuple[tuple[str, str, int, int], ...],
    expected_sql: str,
    *,
    schema_label: str = "Event",
) -> None:
    schema_row = connection.execute(
        "SELECT type, sql FROM sqlite_master WHERE name = ?",
        (table_name,),
    ).fetchone()
    if schema_row is None:
        return
    if schema_row[0] != "table":
        raise StorageError(
            f"{schema_label} database has an incompatible schema: {path}"
        )
    rows = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    actual = tuple((row[1], row[2].upper(), row[3], row[5]) for row in rows)
    if (
        actual != expected
        or _normalized_schema_sql(schema_row[1])
        != _normalized_schema_sql(expected_sql)
    ):
        raise StorageError(
            f"{schema_label} database has an incompatible schema: {path}"
        )


def _normalized_schema_sql(sql: object) -> str:
    if not isinstance(sql, str):
        return ""
    normalized = " ".join(sql.split()).casefold()
    return re.sub(r"\bif\s+not\s+exists\s+", "", normalized)


def _validate_event_schema(connection: sqlite3.Connection, path: Path) -> None:
    _validate_optional_table(
        connection,
        path,
        "events",
        _EXPECTED_EVENTS_COLUMNS,
        _CREATE_EVENTS_TABLE,
    )
    _validate_optional_table(
        connection,
        path,
        "article_event_resolutions",
        _EXPECTED_RESOLUTION_COLUMNS,
        _CREATE_ARTICLE_EVENT_RESOLUTIONS_TABLE,
    )
    _validate_existing_event_schema(connection, path)


def _validate_existing_notification_schema(
    connection: sqlite3.Connection,
    path: Path,
) -> None:
    notification_objects = (
        "notification_outbox",
        "notification_deliveries",
    )
    object_rows = connection.execute(
        """
        SELECT name, type
        FROM sqlite_master
        WHERE name IN (?, ?)
        """,
        notification_objects,
    ).fetchall()
    existing_objects = {row[0] for row in object_rows}
    if any(row[1] != "table" for row in object_rows):
        raise StorageError(f"Notification database has an incompatible schema: {path}")
    if existing_objects and existing_objects != set(notification_objects):
        raise StorageError(f"Notification database has an incomplete schema: {path}")

    _validate_optional_table(
        connection,
        path,
        "notification_outbox",
        _EXPECTED_NOTIFICATION_OUTBOX_COLUMNS,
        _CREATE_NOTIFICATION_OUTBOX_TABLE,
        schema_label="Notification",
    )
    _validate_optional_table(
        connection,
        path,
        "notification_deliveries",
        _EXPECTED_NOTIFICATION_DELIVERY_COLUMNS,
        _CREATE_NOTIFICATION_DELIVERIES_TABLE,
        schema_label="Notification",
    )


def _validate_notification_schema(
    connection: sqlite3.Connection,
    path: Path,
) -> None:
    _validate_existing_notification_schema(connection, path)


def _validate_feedback_schema(
    connection: sqlite3.Connection,
    path: Path,
) -> None:
    feedback_objects = (
        "telegram_update_offsets",
        "feedback_observations",
        "message_feedback_state",
    )
    object_rows = connection.execute(
        """
        SELECT name, type
        FROM sqlite_master
        WHERE name IN (?, ?, ?)
        """,
        feedback_objects,
    ).fetchall()
    existing_objects = {row[0] for row in object_rows}
    if any(row[1] != "table" for row in object_rows):
        raise StorageError(f"Feedback database has an incompatible schema: {path}")
    if existing_objects and existing_objects != set(feedback_objects):
        raise StorageError(f"Feedback database has an incomplete schema: {path}")

    _validate_optional_table(
        connection,
        path,
        "telegram_update_offsets",
        _EXPECTED_TELEGRAM_UPDATE_OFFSET_COLUMNS,
        _CREATE_TELEGRAM_UPDATE_OFFSETS_TABLE,
        schema_label="Feedback",
    )
    _validate_optional_table(
        connection,
        path,
        "feedback_observations",
        _EXPECTED_FEEDBACK_OBSERVATION_COLUMNS,
        _CREATE_FEEDBACK_OBSERVATIONS_TABLE,
        schema_label="Feedback",
    )
    _validate_optional_table(
        connection,
        path,
        "message_feedback_state",
        _EXPECTED_MESSAGE_FEEDBACK_STATE_COLUMNS,
        _CREATE_MESSAGE_FEEDBACK_STATE_TABLE,
        schema_label="Feedback",
    )


def _validate_analysis_retry_schema(
    connection: sqlite3.Connection,
    path: Path,
) -> None:
    _validate_optional_table(
        connection,
        path,
        "article_analysis_retries",
        _EXPECTED_ARTICLE_ANALYSIS_RETRY_COLUMNS,
        _CREATE_ARTICLE_ANALYSIS_RETRIES_TABLE,
        schema_label="Analysis retry",
    )


def _validate_article_read_failure_schema(
    connection: sqlite3.Connection,
    path: Path,
) -> None:
    _validate_optional_table(
        connection,
        path,
        "article_read_failures",
        _EXPECTED_ARTICLE_READ_FAILURE_COLUMNS,
        _CREATE_ARTICLE_READ_FAILURES_TABLE,
        schema_label="Article read failure",
    )


def _validate_discovery_schema(
    connection: sqlite3.Connection,
    path: Path,
) -> None:
    _validate_optional_table(
        connection,
        path,
        "discovery_skips",
        _EXPECTED_DISCOVERY_SKIP_COLUMNS,
        _CREATE_DISCOVERY_SKIPS_TABLE,
        schema_label="Discovery",
    )
    _validate_optional_table(
        connection,
        path,
        "discovery_http_validators",
        _EXPECTED_DISCOVERY_HTTP_VALIDATOR_COLUMNS,
        _CREATE_DISCOVERY_HTTP_VALIDATORS_TABLE,
        schema_label="Discovery",
    )
    _validate_optional_table(
        connection,
        path,
        "discovery_candidates",
        _EXPECTED_DISCOVERY_CANDIDATE_COLUMNS,
        _CREATE_DISCOVERY_CANDIDATES_TABLE,
        schema_label="Discovery",
    )
