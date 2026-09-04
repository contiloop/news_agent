"""HTTP fetching and parsing for the current RSS discovery increment."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Annotated, Any

import feedparser
import httpx
from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from news_agent.config import RssSourceConfig


NonEmptyText = Annotated[str, Field(min_length=1)]


class RssFetchError(RuntimeError):
    """Raised when the configured RSS feed cannot be fetched."""


class RssParseError(ValueError):
    """Raised when an RSS response does not contain the required item values."""


class DiscoveredItem(BaseModel):
    """Minimum article candidate emitted by the current RSS increment."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    guid: NonEmptyText
    title: NonEmptyText
    url: HttpUrl
    published_at: datetime | None


@contextmanager
def _client_scope(client: httpx.Client | None) -> Iterator[httpx.Client]:
    if client is not None:
        yield client
        return

    with httpx.Client(follow_redirects=True) as owned_client:
        yield owned_client


def fetch_feed(config: RssSourceConfig, *, client: httpx.Client | None = None) -> bytes:
    """Fetch the configured RSS document with its declared User-Agent and timeout."""

    try:
        with _client_scope(client) as active_client:
            response = active_client.get(
                str(config.feed_url),
                headers={"User-Agent": config.user_agent},
                timeout=config.timeout_seconds,
            )
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise RssFetchError(f"RSS fetch failed for source {config.source_id}") from exc

    return response.content


def parse_feed(document: bytes | str) -> tuple[DiscoveredItem, ...]:
    """Parse one RSS document without guessing missing article identities."""

    parsed = feedparser.parse(document)
    if parsed.bozo:
        raise RssParseError(f"RSS document is malformed: {parsed.bozo_exception}")
    if not parsed.get("version"):
        raise RssParseError("Response is not a recognized RSS or Atom document")

    items: list[DiscoveredItem] = []
    for index, entry in enumerate(parsed.entries):
        guid = _entry_text(entry, "id")
        title = _entry_text(entry, "title")
        url = _entry_text(entry, "link")
        published_at = _entry_published_at(entry)

        missing = [
            name
            for name, value in (("guid", guid), ("title", title), ("link", url))
            if not value
        ]
        if missing:
            fields = ", ".join(missing)
            raise RssParseError(f"RSS item {index} is missing required field(s): {fields}")

        try:
            items.append(
                DiscoveredItem(
                    guid=guid,
                    title=title,
                    url=url,
                    published_at=published_at,
                )
            )
        except ValueError as exc:
            raise RssParseError(f"RSS item {index} contains an invalid value") from exc

    return tuple(items)


def fetch_and_parse(
    config: RssSourceConfig,
    *,
    client: httpx.Client | None = None,
) -> tuple[DiscoveredItem, ...]:
    """Fetch and parse one configured RSS source."""

    return parse_feed(fetch_feed(config, client=client))


def _entry_text(entry: Any, key: str) -> str:
    value = entry.get(key, "")
    return value.strip() if isinstance(value, str) else ""


def _entry_published_at(entry: Any) -> datetime | None:
    parsed = entry.get("published_parsed")
    if parsed is None:
        return None

    try:
        return datetime(*parsed[:6], tzinfo=UTC)
    except (TypeError, ValueError):
        return None
