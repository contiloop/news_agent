"""Shared article candidate models for non-feed discovery sources."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    field_serializer,
    field_validator,
)

from news_agent.rss import DiscoveredItem

NonEmptyText = Annotated[str, Field(min_length=1)]


class ArticleCandidate(BaseModel):
    """One visible article-like link collected before an article body is read."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    url: HttpUrl
    title: NonEmptyText
    section_url: HttpUrl
    section_title: NonEmptyText
    visible_text: NonEmptyText
    published_at: datetime | None = None

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

    def to_prompt_dict(self, *, index: int) -> dict[str, object]:
        """Return the compact JSON shape supplied to candidate selection."""

        payload = self.model_dump(mode="json")
        return {
            "index": index,
            "url": payload["url"],
            "title": payload["title"],
            "section_url": payload["section_url"],
            "section_title": payload["section_title"],
            "visible_text": payload["visible_text"],
            "published_at": payload["published_at"],
        }

    def to_discovered_item(self) -> DiscoveredItem:
        """Promote a selected candidate into the existing article pipeline."""

        return DiscoveredItem(
            guid=str(self.url),
            title=self.title,
            url=self.url,
            published_at=self.published_at,
        )


__all__ = ("ArticleCandidate",)
