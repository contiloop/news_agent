"""HTTP fetching and strict parsing for Google News sitemap discovery."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from xml.etree import ElementTree

import httpx
from pydantic import HttpUrl, TypeAdapter

from news_agent.config import RssSourceConfig
from news_agent.rss import DiscoveredItem

SITEMAP_NAMESPACE = "http://www.sitemaps.org/schemas/sitemap/0.9"
NEWS_NAMESPACE = "http://www.google.com/schemas/sitemap-news/0.9"

_URLSET_TAG = f"{{{SITEMAP_NAMESPACE}}}urlset"
_URL_TAG = f"{{{SITEMAP_NAMESPACE}}}url"
_LOC_TAG = f"{{{SITEMAP_NAMESPACE}}}loc"
_NEWS_TAG = f"{{{NEWS_NAMESPACE}}}news"
_TITLE_TAG = f"{{{NEWS_NAMESPACE}}}title"
_PUBLICATION_DATE_TAG = f"{{{NEWS_NAMESPACE}}}publication_date"
_KEYWORDS_TAG = f"{{{NEWS_NAMESPACE}}}keywords"
_HTTP_URL_ADAPTER = TypeAdapter(HttpUrl)

SitemapFetchStatus = Literal["modified", "not_modified"]


class SitemapFetchError(RuntimeError):
    """Raised when the configured Google News sitemap cannot be fetched."""


class SitemapParseError(ValueError):
    """Raised when a Google News sitemap is malformed or has invalid entries."""


@dataclass(frozen=True)
class SitemapFetchResult:
    """Immutable outcome of a conditional news sitemap request."""

    status: SitemapFetchStatus
    document: bytes | None
    etag: str | None
    last_modified: str | None


class _StrictTreeBuilder(ElementTree.TreeBuilder):
    """Tree builder that refuses DTDs and their entity declarations."""

    def doctype(
        self,
        name: str,
        pubid: str | None,
        system: str | None,
    ) -> None:
        del name, pubid, system
        raise ElementTree.ParseError("DTD declarations are not allowed")


@contextmanager
def _client_scope(client: httpx.Client | None) -> Iterator[httpx.Client]:
    if client is not None:
        yield client
        return

    with httpx.Client(follow_redirects=True) as owned_client:
        yield owned_client


def fetch_sitemap(
    config: RssSourceConfig,
    client: httpx.Client | None = None,
) -> bytes:
    """Fetch the configured news sitemap with its declared HTTP settings."""

    response = _fetch_sitemap_response(
        config,
        headers={"User-Agent": config.user_agent},
        accepted_status_codes=(200,),
        client=client,
    )
    return response.content


def fetch_sitemap_conditionally(
    config: RssSourceConfig,
    *,
    etag: str | None = None,
    last_modified: str | None = None,
    client: httpx.Client | None = None,
) -> SitemapFetchResult:
    """Fetch a sitemap, returning validators without parsing a 304 response."""

    normalized_etag = _normalize_validator(etag, name="etag")
    normalized_last_modified = _normalize_validator(
        last_modified,
        name="last_modified",
    )
    headers = {"User-Agent": config.user_agent}
    if normalized_etag is not None:
        headers["If-None-Match"] = normalized_etag
    if normalized_last_modified is not None:
        headers["If-Modified-Since"] = normalized_last_modified

    response = _fetch_sitemap_response(
        config,
        headers=headers,
        accepted_status_codes=(200, 304),
        client=client,
    )
    response_etag = _normalize_validator(response.headers.get("ETag"), name="ETag")
    response_last_modified = _normalize_validator(
        response.headers.get("Last-Modified"),
        name="Last-Modified",
    )

    if response.status_code == 304:
        return SitemapFetchResult(
            status="not_modified",
            document=None,
            etag=response_etag or normalized_etag,
            last_modified=response_last_modified or normalized_last_modified,
        )

    return SitemapFetchResult(
        status="modified",
        document=response.content,
        etag=response_etag,
        last_modified=response_last_modified,
    )


def _fetch_sitemap_response(
    config: RssSourceConfig,
    *,
    headers: dict[str, str],
    accepted_status_codes: tuple[int, ...],
    client: httpx.Client | None,
) -> httpx.Response:
    sitemap_url = config.news_sitemap_url
    if sitemap_url is None:
        raise SitemapFetchError(
            f"News sitemap URL is not configured for source {config.source_id}"
        )

    try:
        with _client_scope(client) as active_client:
            response = active_client.get(
                str(sitemap_url),
                headers=headers,
                timeout=config.timeout_seconds,
            )
            if response.status_code not in accepted_status_codes:
                response.raise_for_status()
                raise httpx.HTTPStatusError(
                    "Unexpected successful HTTP status for news sitemap",
                    request=response.request,
                    response=response,
                )
    except httpx.HTTPError as exc:
        raise SitemapFetchError(
            f"News sitemap fetch failed for source {config.source_id}"
        ) from exc

    return response


def _normalize_validator(value: str | None, *, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string or None")

    normalized = value.strip()
    return normalized or None


def parse_sitemap(
    document: bytes | str,
    *,
    required_keyword: str | None = None,
    excluded_keyword: str | None = None,
) -> tuple[DiscoveredItem, ...]:
    """Parse a Google News sitemap, optionally filtering one exact keyword token."""

    normalized_required_keyword = _normalize_filter_keyword(
        required_keyword,
        name="required_keyword",
    )
    normalized_excluded_keyword = _normalize_filter_keyword(
        excluded_keyword,
        name="excluded_keyword",
    )
    if (
        normalized_required_keyword is not None
        and normalized_excluded_keyword is not None
    ):
        raise SitemapParseError(
            "required_keyword and excluded_keyword cannot both be configured"
        )

    try:
        parser = ElementTree.XMLParser(target=_StrictTreeBuilder())
        root = ElementTree.fromstring(document, parser=parser)
    except (ElementTree.ParseError, LookupError, TypeError, ValueError) as exc:
        raise SitemapParseError(f"News sitemap document is malformed: {exc}") from exc

    if root.tag != _URLSET_TAG:
        raise SitemapParseError(
            "Response is not a recognized Google News sitemap document"
        )

    items: list[DiscoveredItem] = []
    for index, url_element in enumerate(root.findall(_URL_TAG)):
        loc_elements = url_element.findall(_LOC_TAG)
        news_elements = url_element.findall(_NEWS_TAG)
        news_element = news_elements[0] if len(news_elements) == 1 else None
        title_elements = (
            news_element.findall(_TITLE_TAG) if news_element is not None else []
        )
        publication_date_elements = (
            news_element.findall(_PUBLICATION_DATE_TAG)
            if news_element is not None
            else []
        )

        duplicates = [
            field
            for field, elements in (
                ("loc", loc_elements),
                ("news:news", news_elements),
                ("news:title", title_elements),
                ("news:publication_date", publication_date_elements),
            )
            if len(elements) > 1
        ]
        if duplicates:
            fields = ", ".join(duplicates)
            raise SitemapParseError(
                f"News sitemap item {index} has duplicate field(s): {fields}"
            )

        loc = _element_text(loc_elements[0] if loc_elements else None)
        title = _element_text(title_elements[0] if title_elements else None)
        publication_date = _element_text(
            publication_date_elements[0] if publication_date_elements else None
        )

        missing = [
            field
            for field, value in (
                ("loc", loc),
                ("news:title", title),
                ("news:publication_date", publication_date),
            )
            if not value
        ]
        if missing:
            fields = ", ".join(missing)
            raise SitemapParseError(
                f"News sitemap item {index} is missing required field(s): {fields}"
            )

        try:
            canonical_url = str(_HTTP_URL_ADAPTER.validate_python(loc))
            published_at = datetime.fromisoformat(publication_date)
            if published_at.tzinfo is None or published_at.utcoffset() is None:
                raise ValueError("publication date has no UTC offset")

            item = DiscoveredItem(
                guid=canonical_url,
                title=title,
                url=canonical_url,
                published_at=published_at.astimezone(UTC),
            )
        except ValueError as exc:
            raise SitemapParseError(
                f"News sitemap item {index} contains an invalid value"
            ) from exc

        if normalized_required_keyword is not None and not _has_keyword(
            news_element, normalized_required_keyword
        ):
            continue
        if normalized_excluded_keyword is not None and _has_keyword(
            news_element, normalized_excluded_keyword
        ):
            continue

        items.append(item)

    return tuple(items)


def fetch_and_parse_sitemap(
    config: RssSourceConfig,
    client: httpx.Client | None = None,
) -> tuple[DiscoveredItem, ...]:
    """Fetch and parse the configured Google News sitemap source."""

    return parse_sitemap(
        fetch_sitemap(config, client),
        required_keyword=config.news_sitemap_keyword,
        excluded_keyword=config.news_sitemap_excluded_keyword,
    )


def _normalize_filter_keyword(keyword: str | None, *, name: str) -> str | None:
    if keyword is None:
        return None

    normalized = keyword.strip().casefold()
    if not normalized:
        raise SitemapParseError(f"{name} must not be blank")
    return normalized


def _element_text(element: ElementTree.Element | None) -> str:
    if element is None or not isinstance(element.text, str):
        return ""
    return element.text.strip()


def _has_keyword(
    news_element: ElementTree.Element | None,
    required_keyword: str,
) -> bool:
    if news_element is None:
        return False

    for keywords_element in news_element.findall(_KEYWORDS_TAG):
        text = keywords_element.text
        if not isinstance(text, str):
            continue
        tokens = (token.strip().casefold() for token in text.split(","))
        if required_keyword in tokens:
            return True
    return False
