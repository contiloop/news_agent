"""Poll Telegram reactions and persist explicit news preference feedback."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import httpx

from news_agent.config import TelegramNotificationTarget
from news_agent.notifier import NotificationSendError, _telegram_bot_token
from news_agent.storage import (
    FeedbackObservation,
    StorageError,
    find_telegram_delivery_match,
    get_telegram_update_offset,
    store_feedback_observation,
    store_telegram_update_offset,
)

HEART = "\u2764\ufe0f"
HEART_NORMALIZED = "\u2764"
THUMBS_DOWN = "\U0001f44e"
WASTEBASKET = "\U0001f5d1\ufe0f"
WASTEBASKET_NORMALIZED = "\U0001f5d1"
SUPPORTED_REACTIONS = {
    HEART_NORMALIZED: (HEART, "more_like_this"),
    THUMBS_DOWN: (THUMBS_DOWN, "less_like_this"),
    WASTEBASKET_NORMALIZED: (WASTEBASKET, "strong_negative"),
}
ALLOWED_UPDATE_TYPES = ("message_reaction", "message_reaction_count")
TELEGRAM_UPDATE_LIMIT = 100

CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


class TelegramFeedbackError(RuntimeError):
    """Raised when Telegram reaction polling cannot complete safely."""


@dataclass(frozen=True)
class TelegramFeedbackPollResult:
    """Counts from one Telegram getUpdates poll for one target."""

    target_id: str
    processed_update_count: int
    recorded_count: int
    duplicate_count: int
    ignored_count: int
    unmatched_count: int
    next_update_id: int | None


@dataclass(frozen=True)
class _ReactionSelection:
    emoji: str
    signal: str


@dataclass(frozen=True)
class _ParsedReaction:
    update_id: int
    chat_id: str
    message_id: str
    selection: _ReactionSelection
    raw_old_reaction: str
    raw_new_reaction: str


def poll_telegram_feedback(
    target: TelegramNotificationTarget,
    database_file: str | Path,
    *,
    client: httpx.Client | None = None,
    environ: Mapping[str, str] | None = None,
    command_runner: CommandRunner | None = None,
) -> TelegramFeedbackPollResult:
    """Poll Telegram message reactions and record mapped news feedback."""

    if target.adapter != "telegram":
        raise TelegramFeedbackError("Feedback polling requires a Telegram target")

    token = _feedback_bot_token(
        target,
        environ=os.environ if environ is None else environ,
        command_runner=subprocess.run if command_runner is None else command_runner,
    )
    endpoint = _telegram_get_updates_endpoint(target, token)
    offset = get_telegram_update_offset(database_file, target.id)
    request = _get_updates_request(offset)

    with _client_scope(client) as active_client:
        updates = _get_updates(
            active_client,
            endpoint,
            request=request,
            timeout_seconds=target.timeout_seconds,
        )

    latest_update_id: int | None = None
    recorded_count = 0
    duplicate_count = 0
    ignored_count = 0
    unmatched_count = 0

    for update in updates:
        update_id = _update_id(update)
        if update_id is None:
            ignored_count += 1
            continue
        latest_update_id = (
            update_id
            if latest_update_id is None
            else max(latest_update_id, update_id)
        )

        parsed = _parse_reaction_update(update)
        if parsed is None:
            ignored_count += 1
            continue
        if parsed.chat_id != target.chat_id:
            ignored_count += 1
            continue

        match = find_telegram_delivery_match(
            database_file,
            target.id,
            parsed.message_id,
        )
        if match is None:
            unmatched_count += 1
            continue

        inserted = store_feedback_observation(
            database_file,
            FeedbackObservation(
                update_id=parsed.update_id,
                target_id=target.id,
                chat_id=parsed.chat_id,
                message_id=parsed.message_id,
                delivery_id=match.delivery_id,
                event_id=match.event_id,
                source_id=match.source_id,
                guid=match.guid,
                article_url=match.article_url,
                reaction_emoji=parsed.selection.emoji,
                signal=parsed.selection.signal,
                raw_old_reaction=parsed.raw_old_reaction,
                raw_new_reaction=parsed.raw_new_reaction,
            ),
        )
        if inserted:
            recorded_count += 1
        else:
            duplicate_count += 1

    next_update_id = None
    if latest_update_id is not None:
        next_update_id = latest_update_id + 1
        store_telegram_update_offset(database_file, target.id, next_update_id)

    return TelegramFeedbackPollResult(
        target_id=target.id,
        processed_update_count=len(updates),
        recorded_count=recorded_count,
        duplicate_count=duplicate_count,
        ignored_count=ignored_count,
        unmatched_count=unmatched_count,
        next_update_id=next_update_id,
    )


def _feedback_bot_token(
    target: TelegramNotificationTarget,
    *,
    environ: Mapping[str, str],
    command_runner: CommandRunner,
) -> str:
    try:
        return _telegram_bot_token(
            target,
            environ=environ,
            command_runner=command_runner,
        )
    except NotificationSendError as exc:
        raise TelegramFeedbackError(str(exc)) from exc


def _telegram_get_updates_endpoint(
    target: TelegramNotificationTarget,
    token: str,
) -> httpx.URL:
    base_url = httpx.URL(str(target.api_base_url))
    if base_url.scheme != "https":
        raise TelegramFeedbackError("Telegram API endpoint must use HTTPS")

    base_path = base_url.path.rstrip("/")
    token_path = quote(token, safe=":")
    return base_url.copy_with(
        path=f"{base_path}/bot{token_path}/getUpdates",
        query=None,
        fragment=None,
    )


def _get_updates_request(offset: int | None) -> dict[str, object]:
    request: dict[str, object] = {
        "limit": TELEGRAM_UPDATE_LIMIT,
        "timeout": 0,
        "allowed_updates": list(ALLOWED_UPDATE_TYPES),
    }
    if offset is not None:
        request["offset"] = offset
    return request


def _get_updates(
    client: httpx.Client,
    endpoint: httpx.URL,
    *,
    request: Mapping[str, object],
    timeout_seconds: float,
) -> tuple[dict[str, object], ...]:
    try:
        response = client.post(endpoint, json=dict(request), timeout=timeout_seconds)
    except httpx.RequestError as exc:
        raise TelegramFeedbackError("Telegram feedback request failed") from exc

    if not 200 <= response.status_code <= 299:
        raise TelegramFeedbackError("Telegram feedback request was rejected")

    try:
        payload = response.json()
    except ValueError as exc:
        raise TelegramFeedbackError(
            "Telegram feedback returned an invalid response"
        ) from exc
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise TelegramFeedbackError("Telegram feedback returned an invalid response")
    result = payload.get("result")
    if not isinstance(result, list):
        raise TelegramFeedbackError("Telegram feedback returned an invalid response")
    if not all(isinstance(item, dict) for item in result):
        raise TelegramFeedbackError("Telegram feedback returned an invalid response")
    return tuple(result)


def _update_id(update: Mapping[str, object]) -> int | None:
    value = update.get("update_id")
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _parse_reaction_update(update: Mapping[str, object]) -> _ParsedReaction | None:
    update_id = _update_id(update)
    reaction = update.get("message_reaction")
    if update_id is None or not isinstance(reaction, dict):
        return None

    chat = reaction.get("chat")
    if not isinstance(chat, dict):
        return None
    chat_id = chat.get("id")
    message_id = reaction.get("message_id")
    if (
        isinstance(chat_id, bool)
        or not isinstance(chat_id, (int, str))
        or isinstance(message_id, bool)
        or not isinstance(message_id, (int, str))
    ):
        return None

    old_reaction = reaction.get("old_reaction")
    new_reaction = reaction.get("new_reaction")
    if not isinstance(old_reaction, list) or not isinstance(new_reaction, list):
        return None

    selection = _reaction_selection(old_reaction, new_reaction)
    if selection is None:
        return None
    return _ParsedReaction(
        update_id=update_id,
        chat_id=str(chat_id),
        message_id=str(message_id),
        selection=selection,
        raw_old_reaction=_reaction_json(old_reaction),
        raw_new_reaction=_reaction_json(new_reaction),
    )


def _reaction_selection(
    old_reaction: list[object],
    new_reaction: list[object],
) -> _ReactionSelection | None:
    selected_new = _first_supported_reaction(new_reaction)
    if selected_new is not None:
        return selected_new
    selected_old = _first_supported_reaction(old_reaction)
    if selected_old is None:
        return None
    return _ReactionSelection(emoji=selected_old.emoji, signal="cleared")


def _first_supported_reaction(
    reactions: list[object],
) -> _ReactionSelection | None:
    for reaction in reactions:
        if not isinstance(reaction, dict):
            continue
        if reaction.get("type") != "emoji":
            continue
        emoji = reaction.get("emoji")
        if not isinstance(emoji, str):
            continue
        normalized = emoji.replace("\ufe0f", "")
        mapped = SUPPORTED_REACTIONS.get(normalized)
        if mapped is None:
            continue
        canonical_emoji, signal = mapped
        return _ReactionSelection(emoji=canonical_emoji, signal=signal)
    return None


def _reaction_json(reactions: list[object]) -> str:
    return json.dumps(reactions, ensure_ascii=False, separators=(",", ":"))


@contextmanager
def _client_scope(client: httpx.Client | None) -> Iterator[httpx.Client]:
    if client is not None:
        yield client
        return

    with httpx.Client() as owned_client:
        yield owned_client


__all__ = (
    "ALLOWED_UPDATE_TYPES",
    "SUPPORTED_REACTIONS",
    "TELEGRAM_UPDATE_LIMIT",
    "TelegramFeedbackError",
    "TelegramFeedbackPollResult",
    "poll_telegram_feedback",
)
