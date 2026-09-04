"""Configuration loading for one news discovery source."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    ValidationError,
    field_validator,
    model_validator,
)

NonEmptyText = Annotated[str, Field(min_length=1)]
PositiveTimeout = Annotated[float, Field(gt=0, allow_inf_nan=False)]
PositiveInteger = Annotated[int, Field(gt=0)]
EnvironmentVariableName = Annotated[
    str,
    Field(min_length=1, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$"),
]


class ConfigLoadError(ValueError):
    """Raised when an RSS source configuration cannot be loaded safely."""


class TelegramNotificationTarget(BaseModel):
    """One explicitly configured Telegram delivery destination."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    id: NonEmptyText
    adapter: Literal["telegram"]
    api_base_url: HttpUrl
    chat_id: NonEmptyText
    bot_token_env: EnvironmentVariableName | None = None
    bot_token_keychain_service: NonEmptyText | None = None
    bot_token_keychain_account: NonEmptyText | None = None
    timeout_seconds: PositiveTimeout

    @model_validator(mode="after")
    def validate_bot_token_source(self) -> Self:
        """Require one usable token source and a complete Keychain reference."""

        has_keychain_service = self.bot_token_keychain_service is not None
        has_keychain_account = self.bot_token_keychain_account is not None
        if has_keychain_service != has_keychain_account:
            raise ValueError(
                "bot_token_keychain_service and bot_token_keychain_account "
                "must be configured together"
            )
        has_environment_source = self.bot_token_env is not None
        if has_environment_source == has_keychain_service:
            raise ValueError(
                "telegram target must configure exactly one bot token source: "
                "bot_token_env or the Keychain service/account pair"
            )
        return self


class NotificationConfig(BaseModel):
    """Delivery policy and currently enabled notification targets."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    lease_seconds: PositiveInteger
    max_attempts: PositiveInteger
    retry_base_seconds: PositiveTimeout
    retry_max_seconds: PositiveTimeout
    targets: Annotated[tuple[TelegramNotificationTarget, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_delivery_policy(self) -> Self:
        """Reject ambiguous targets and a retry cap below the first delay."""

        target_ids = [target.id for target in self.targets]
        if len(target_ids) != len(set(target_ids)):
            raise ValueError("notification target ids must be unique")
        if self.retry_max_seconds < self.retry_base_seconds:
            raise ValueError(
                "notification retry_max_seconds must be at least retry_base_seconds"
            )
        if any(
            target.timeout_seconds >= self.lease_seconds
            for target in self.targets
        ):
            raise ValueError(
                "notification lease_seconds must exceed every target timeout_seconds"
            )
        return self


class RssSourceConfig(BaseModel):
    """Validated settings for RSS or News Sitemap discovery."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
    )

    source_id: NonEmptyText
    feed_url: HttpUrl
    news_sitemap_url: HttpUrl | None = None
    news_sitemap_keyword: NonEmptyText | None = None
    news_sitemap_excluded_keyword: NonEmptyText | None = None
    browser_section_urls: tuple[HttpUrl, ...] = ()
    browser_article_allowed_hosts: tuple[NonEmptyText, ...] = ()
    browser_article_allowed_path_prefixes: tuple[NonEmptyText, ...] = ()
    browser_article_excluded_path_prefixes: tuple[NonEmptyText, ...] = ()
    browser_candidate_collect_limit: PositiveInteger = 60
    browser_candidate_prompt_limit: PositiveInteger = 20
    browser_candidate_selection_limit: PositiveInteger = 5
    browser_candidate_read_limit: PositiveInteger = 3
    browser_candidate_rejected_cooldown_hours: PositiveTimeout = 24.0
    browser_candidate_selection_profile: NonEmptyText | None = None
    user_agent: NonEmptyText
    timeout_seconds: PositiveTimeout
    codex_timeout_seconds: PositiveTimeout
    database_file: Path
    memory_directory: Path = Path("../data/memory")
    memory_context_max_characters: PositiveInteger = 4000
    browser_mode: Literal["cua", "dedicated_chrome"] = "cua"
    browser_launch_mode: Literal[
        "headless",
        "hidden_cdp",
        "regular_cdp",
    ] = "headless"
    browser_bundle_id: NonEmptyText | None = None
    browser_executable: Path | None = None
    browser_profile_directory: Path | None = None
    article_body_selector: NonEmptyText = "p"
    article_access_denied_selector: NonEmptyText | None = None
    article_access_denied_phrases: tuple[NonEmptyText, ...] = ()
    article_read_retry_max_attempts: PositiveInteger = 3
    article_read_retry_base_seconds: PositiveTimeout = 900
    article_read_retry_max_seconds: PositiveTimeout = 21600
    notifications: NotificationConfig | None = None

    @field_validator(
        "browser_executable",
        "browser_profile_directory",
        "memory_directory",
        mode="before",
    )
    @classmethod
    def validate_browser_path_text(cls, value: object) -> object:
        """Reject path settings that TOML would otherwise coerce from blank text."""

        if isinstance(value, str) and not value.strip():
            raise ValueError("browser paths cannot be empty")
        return value

    @model_validator(mode="after")
    def validate_browser_settings(self) -> Self:
        """Require exactly the settings used by the selected browser backend."""

        has_bundle_id = self.browser_bundle_id is not None
        has_executable = self.browser_executable is not None
        has_profile_directory = self.browser_profile_directory is not None
        if self.browser_mode == "cua":
            if self.browser_launch_mode != "headless":
                raise ValueError(
                    "non-headless browser_launch_mode requires dedicated_chrome "
                    "browser mode"
                )
            if not has_bundle_id or has_executable or has_profile_directory:
                raise ValueError(
                    "cua browser mode requires browser_bundle_id and forbids "
                    "dedicated Chrome settings"
                )
        elif (
            has_bundle_id
            or not has_executable
            or not has_profile_directory
        ):
            raise ValueError(
                "dedicated_chrome browser mode requires browser_executable and "
                "browser_profile_directory and forbids browser_bundle_id"
            )
        return self

    @model_validator(mode="after")
    def validate_news_sitemap_settings(self) -> Self:
        """Require an explicit filter whenever News Sitemap discovery is enabled."""

        has_sitemap = self.news_sitemap_url is not None
        has_required_keyword = self.news_sitemap_keyword is not None
        has_excluded_keyword = self.news_sitemap_excluded_keyword is not None
        has_filter = has_required_keyword or has_excluded_keyword
        has_exactly_one_filter = has_required_keyword != has_excluded_keyword
        if not has_sitemap and has_filter:
            raise ValueError(
                "news_sitemap filters require news_sitemap_url"
            )
        if has_sitemap and not has_exactly_one_filter:
            raise ValueError(
                "news_sitemap_url requires exactly one filter: "
                "news_sitemap_keyword or news_sitemap_excluded_keyword"
            )
        return self

    @field_validator(
        "browser_article_allowed_hosts",
        "browser_article_allowed_path_prefixes",
        "browser_article_excluded_path_prefixes",
    )
    @classmethod
    def validate_browser_candidate_filter_values(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        """Normalize browser-discovery filters and reject ambiguous entries."""

        normalized = tuple(value.strip() for value in values)
        if len(normalized) != len(set(normalized)):
            raise ValueError("browser discovery filter values must be unique")
        return normalized

    @field_validator(
        "browser_article_allowed_path_prefixes",
        "browser_article_excluded_path_prefixes",
    )
    @classmethod
    def validate_browser_article_path_prefixes(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        """Keep article URL path filters explicit and path-like."""

        for value in values:
            if not value.startswith("/"):
                raise ValueError("browser article path prefixes must start with /")
            if "\\" in value:
                raise ValueError("browser article path prefixes cannot contain \\")
        return values

    @field_validator("article_access_denied_phrases")
    @classmethod
    def validate_article_access_denied_phrases(
        cls,
        values: tuple[str, ...],
    ) -> tuple[str, ...]:
        """Normalize access-denied phrases and reject ambiguous duplicates."""

        normalized = tuple(value.strip() for value in values)
        if any(not value for value in normalized):
            raise ValueError("article access denied phrases cannot be empty")
        folded = tuple(value.casefold() for value in normalized)
        if len(folded) != len(set(folded)):
            raise ValueError("article access denied phrases must be unique")
        return normalized

    @model_validator(mode="after")
    def validate_article_read_settings(self) -> Self:
        """Require complete access rules and a bounded retry schedule."""

        has_access_selector = self.article_access_denied_selector is not None
        has_access_phrases = bool(self.article_access_denied_phrases)
        if has_access_selector != has_access_phrases:
            raise ValueError(
                "article_access_denied_selector and "
                "article_access_denied_phrases must be configured together"
            )
        if has_access_selector and self.browser_mode != "dedicated_chrome":
            raise ValueError(
                "article access denied detection requires dedicated_chrome "
                "browser mode"
            )
        if self.article_read_retry_max_seconds < self.article_read_retry_base_seconds:
            raise ValueError(
                "article_read_retry_max_seconds must be at least "
                "article_read_retry_base_seconds"
            )
        return self

    @model_validator(mode="after")
    def validate_browser_section_settings(self) -> Self:
        """Require bounded, dedicated-browser settings for section discovery."""

        has_sections = bool(self.browser_section_urls)
        has_section_filters = any(
            (
                self.browser_article_allowed_hosts,
                self.browser_article_allowed_path_prefixes,
                self.browser_article_excluded_path_prefixes,
                self.browser_candidate_selection_profile is not None,
            )
        )
        if not has_sections:
            if has_section_filters:
                raise ValueError(
                    "browser discovery filters require browser_section_urls"
                )
            return self

        if self.news_sitemap_url is not None:
            raise ValueError(
                "browser_section_urls cannot be combined with news_sitemap_url"
            )
        if self.browser_mode != "dedicated_chrome":
            raise ValueError(
                "browser_section_urls requires dedicated_chrome browser mode"
            )
        if not self.browser_article_allowed_hosts:
            raise ValueError(
                "browser_section_urls requires browser_article_allowed_hosts"
            )
        if not self.browser_article_allowed_path_prefixes:
            raise ValueError(
                "browser_section_urls requires "
                "browser_article_allowed_path_prefixes"
            )
        if self.browser_candidate_selection_profile is None:
            raise ValueError(
                "browser_section_urls requires "
                "browser_candidate_selection_profile"
            )
        if self.browser_candidate_prompt_limit > self.browser_candidate_collect_limit:
            raise ValueError(
                "browser_candidate_prompt_limit cannot exceed "
                "browser_candidate_collect_limit"
            )
        if self.browser_candidate_selection_limit > self.browser_candidate_prompt_limit:
            raise ValueError(
                "browser_candidate_selection_limit cannot exceed "
                "browser_candidate_prompt_limit"
            )
        if self.browser_candidate_read_limit > self.browser_candidate_selection_limit:
            raise ValueError(
                "browser_candidate_read_limit cannot exceed "
                "browser_candidate_selection_limit"
            )
        return self


def load_config(path: str | Path) -> RssSourceConfig:
    """Load TOML settings and safely resolve its configured filesystem paths."""

    config_path = Path(path).expanduser().resolve()

    try:
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigLoadError(f"Config file not found: {config_path}") from exc
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigLoadError(f"Config file cannot be read: {config_path}") from exc

    try:
        config = RssSourceConfig.model_validate(raw)
    except ValidationError:
        # ValidationError renders rejected input values. Configuration input may
        # contain credentials, so never attach its text or exception chain to a
        # user-facing error.
        raise ConfigLoadError(f"Config file is invalid: {config_path}") from None

    database_file = config.database_file.expanduser()
    if not database_file.is_absolute():
        database_file = (config_path.parent / database_file).resolve()
    memory_directory = _resolve_relative_path(
        config.memory_directory,
        base_directory=config_path.parent,
    )

    try:
        browser_executable = _resolve_optional_relative_path(
            config.browser_executable,
            base_directory=config_path.parent,
        )
        browser_profile_directory = _resolve_optional_relative_path(
            config.browser_profile_directory,
            base_directory=config_path.parent,
        )
        _validate_resolved_browser_paths(
            browser_executable,
            browser_profile_directory,
        )
    except (OSError, RuntimeError, ValueError):
        raise ConfigLoadError(f"Config file is invalid: {config_path}") from None

    return config.model_copy(
        update={
            "database_file": database_file,
            "memory_directory": memory_directory,
            "browser_executable": browser_executable,
            "browser_profile_directory": browser_profile_directory,
        }
    )


def _resolve_optional_relative_path(
    path: Path | None,
    *,
    base_directory: Path,
) -> Path | None:
    if path is None:
        return None
    return _resolve_relative_path(path, base_directory=base_directory)


def _resolve_relative_path(path: Path, *, base_directory: Path) -> Path:
    expanded = path.expanduser()
    if not expanded.is_absolute():
        expanded = base_directory / expanded
    return expanded.resolve()


def _validate_resolved_browser_paths(
    executable: Path | None,
    profile_directory: Path | None,
) -> None:
    if executable is None or profile_directory is None:
        return
    if executable == profile_directory:
        raise ValueError(
            "browser executable and profile directory must be different paths"
        )

    home_directory = Path.home().resolve()
    personal_chrome_directory = (
        home_directory / "Library" / "Application Support" / "Google" / "Chrome"
    ).resolve()
    is_filesystem_root = profile_directory.parent == profile_directory
    is_personal_chrome_directory = (
        profile_directory == personal_chrome_directory
        or personal_chrome_directory in profile_directory.parents
    )
    if (
        is_filesystem_root
        or profile_directory == home_directory
        or is_personal_chrome_directory
    ):
        raise ValueError("browser profile directory is not safely isolated")
