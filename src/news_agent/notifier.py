"""Telegram delivery for article notifications and plain-text operational alerts."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from urllib.parse import quote

import httpx

from news_agent.config import TelegramNotificationTarget

TELEGRAM_TEXT_LIMIT = 4096
KEYCHAIN_READ_TIMEOUT_SECONDS = 5.0


class NotificationSendError(RuntimeError):
    """A sanitized notification failure with an explicit retry decision."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool,
        retry_after_seconds: int | None = None,
        receipt_ids: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds
        self.receipt_ids = receipt_ids


class TelegramNotifier:
    """Send one complete notification through a configured Telegram bot."""

    def __init__(
        self,
        target: TelegramNotificationTarget,
        *,
        client: httpx.Client | None = None,
        environ: Mapping[str, str] | None = None,
        command_runner: (
            Callable[..., subprocess.CompletedProcess[str]] | None
        ) = None,
    ) -> None:
        self._target = target
        self._client = client
        self._environ = os.environ if environ is None else environ
        self._command_runner = (
            subprocess.run if command_runner is None else command_runner
        )

    def send(
        self,
        translated_title: str,
        summary: str,
        article_url: str,
        *,
        heartbeat: Callable[[], bool] | None = None,
    ) -> tuple[str, ...]:
        """Send every lossless text chunk and return its Telegram message ID."""

        message, link_suffix = _notification_text(
            translated_title,
            summary,
            article_url,
        )
        return self._send_message(
            message,
            protected_tail=link_suffix,
            heartbeat=heartbeat,
        )

    def send_text(
        self,
        text: str,
        *,
        heartbeat: Callable[[], bool] | None = None,
    ) -> tuple[str, ...]:
        """Send nonempty plain text unchanged, split into lossless chunks."""

        if not isinstance(text, str) or not text.strip():
            raise NotificationSendError(
                "Telegram notification payload is invalid",
                retryable=False,
            )
        return self._send_message(text, heartbeat=heartbeat)

    def _send_message(
        self,
        message: str,
        *,
        protected_tail: str = "",
        heartbeat: Callable[[], bool] | None = None,
    ) -> tuple[str, ...]:
        token = _telegram_bot_token(
            self._target,
            environ=self._environ,
            command_runner=self._command_runner,
        )

        endpoint = _telegram_endpoint(self._target, token)
        receipt_ids: list[str] = []
        failure: NotificationSendError | None = None

        with _client_scope(self._client) as client:
            for chunk in _split_text(message, protected_tail=protected_tail):
                _require_live_heartbeat(heartbeat, receipt_ids)
                try:
                    receipt_id = _send_chunk(
                        client,
                        endpoint,
                        chat_id=self._target.chat_id,
                        text=chunk,
                        timeout_seconds=self._target.timeout_seconds,
                    )
                except NotificationSendError as exc:
                    failure = exc
                    break
                receipt_ids.append(receipt_id)
                _require_live_heartbeat(heartbeat, receipt_ids)

        if failure is not None:
            raise NotificationSendError(
                str(failure),
                retryable=failure.retryable,
                retry_after_seconds=failure.retry_after_seconds,
                receipt_ids=tuple(receipt_ids),
            )
        return tuple(receipt_ids)


def _telegram_bot_token(
    target: TelegramNotificationTarget,
    *,
    environ: Mapping[str, str],
    command_runner: Callable[..., subprocess.CompletedProcess[str]],
) -> str:
    environment_name = target.bot_token_env
    if environment_name is not None:
        environment_token = environ.get(environment_name)
        if isinstance(environment_token, str) and environment_token.strip():
            return environment_token.strip()

    keychain_service = target.bot_token_keychain_service
    keychain_account = target.bot_token_keychain_account
    if keychain_service is None or keychain_account is None:
        raise NotificationSendError(
            "Telegram bot token is unavailable",
            retryable=False,
        )

    keychain_token: object | None = None
    try:
        completed = command_runner(
            [
                "/usr/bin/security",
                "find-generic-password",
                "-s",
                keychain_service,
                "-a",
                keychain_account,
                "-w",
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=KEYCHAIN_READ_TIMEOUT_SECONDS,
            check=False,
            shell=False,
        )
        if completed.returncode == 0:
            keychain_token = completed.stdout
    except (OSError, subprocess.SubprocessError, UnicodeError):
        keychain_token = None

    if not isinstance(keychain_token, str) or not keychain_token.strip():
        raise NotificationSendError(
            "Telegram bot token could not be read from Keychain",
            retryable=True,
        )
    return keychain_token.strip()


def _require_live_heartbeat(
    heartbeat: Callable[[], bool] | None,
    receipt_ids: list[str],
) -> None:
    if heartbeat is not None and not heartbeat():
        raise NotificationSendError(
            "Notification delivery lease was lost",
            retryable=False,
            receipt_ids=tuple(receipt_ids),
        )


@contextmanager
def _client_scope(client: httpx.Client | None) -> Iterator[httpx.Client]:
    if client is not None:
        yield client
        return

    with httpx.Client() as owned_client:
        yield owned_client


def _notification_text(
    translated_title: str,
    summary: str,
    article_url: str,
) -> tuple[str, str]:
    if (
        not isinstance(translated_title, str)
        or not translated_title.strip()
        or not isinstance(summary, str)
        or not summary.strip()
        or not isinstance(article_url, str)
        or not article_url.strip()
    ):
        raise NotificationSendError(
            "Telegram notification payload is invalid",
            retryable=False,
        )
    selected_url = article_url.strip()
    try:
        parsed_url = httpx.URL(selected_url)
    except httpx.InvalidURL:
        raise NotificationSendError(
            "Telegram notification payload is invalid",
            retryable=False,
        ) from None
    if parsed_url.scheme not in {"http", "https"} or parsed_url.host is None:
        raise NotificationSendError(
            "Telegram notification payload is invalid",
            retryable=False,
        )
    link_suffix = f"\n\n원문: {selected_url}"
    if len(link_suffix) > TELEGRAM_TEXT_LIMIT:
        raise NotificationSendError(
            "Telegram notification payload is invalid",
            retryable=False,
        )
    return f"{translated_title}\n\n{summary}{link_suffix}", link_suffix


def _split_text(text: str, *, protected_tail: str = "") -> tuple[str, ...]:
    body = text
    if protected_tail:
        if not text.endswith(protected_tail):
            raise ValueError("protected tail must end the notification text")
        body = text[: -len(protected_tail)]

    chunks = [
        body[offset : offset + TELEGRAM_TEXT_LIMIT]
        for offset in range(0, len(body), TELEGRAM_TEXT_LIMIT)
    ]
    if not protected_tail:
        return tuple(chunks)
    if chunks and len(chunks[-1]) + len(protected_tail) <= TELEGRAM_TEXT_LIMIT:
        chunks[-1] += protected_tail
    else:
        chunks.append(protected_tail)
    return tuple(chunks)


def _telegram_endpoint(
    target: TelegramNotificationTarget,
    token: str,
) -> httpx.URL:
    base_url = httpx.URL(str(target.api_base_url))
    if base_url.scheme != "https":
        raise NotificationSendError(
            "Telegram API endpoint must use HTTPS",
            retryable=False,
        )

    base_path = base_url.path.rstrip("/")
    token_path = quote(token, safe=":")
    return base_url.copy_with(
        path=f"{base_path}/bot{token_path}/sendMessage",
        query=None,
        fragment=None,
    )


def _send_chunk(
    client: httpx.Client,
    endpoint: httpx.URL,
    *,
    chat_id: str,
    text: str,
    timeout_seconds: float,
) -> str:
    request_failure = False
    try:
        response = client.post(
            endpoint,
            json={"chat_id": chat_id, "text": text},
            timeout=timeout_seconds,
        )
    except httpx.RequestError:
        request_failure = True

    if request_failure:
        raise NotificationSendError(
            "Telegram notification request failed",
            retryable=True,
        )

    if response.status_code == 429:
        raise NotificationSendError(
            "Telegram notification was rate limited",
            retryable=True,
            retry_after_seconds=_retry_after_seconds(response),
        )
    if 500 <= response.status_code <= 599:
        raise NotificationSendError(
            "Telegram notification service failed",
            retryable=True,
        )
    if 400 <= response.status_code <= 499:
        raise NotificationSendError(
            "Telegram notification was rejected",
            retryable=False,
        )
    if not 200 <= response.status_code <= 299:
        raise NotificationSendError(
            "Telegram notification returned an unexpected response",
            retryable=False,
        )

    payload = _response_json(response)
    if payload is None:
        raise NotificationSendError(
            "Telegram notification returned an invalid response",
            retryable=False,
        )

    if payload.get("ok") is not True:
        error_code = payload.get("error_code")
        if error_code == 429:
            raise NotificationSendError(
                "Telegram notification was rate limited",
                retryable=True,
                retry_after_seconds=_payload_retry_after_seconds(payload),
            )
        if (
            isinstance(error_code, int)
            and not isinstance(error_code, bool)
            and 500 <= error_code <= 599
        ):
            raise NotificationSendError(
                "Telegram notification service failed",
                retryable=True,
            )
        raise NotificationSendError(
            "Telegram notification was rejected",
            retryable=False,
        )

    result = payload.get("result")
    if not isinstance(result, dict):
        raise NotificationSendError(
            "Telegram notification returned an invalid response",
            retryable=False,
        )
    message_id = result.get("message_id")
    if (
        not isinstance(message_id, int)
        or isinstance(message_id, bool)
        or message_id < 1
    ):
        raise NotificationSendError(
            "Telegram notification returned an invalid response",
            retryable=False,
        )
    return str(message_id)


def _response_json(response: httpx.Response) -> dict[str, object] | None:
    try:
        payload = response.json()
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _retry_after_seconds(response: httpx.Response) -> int | None:
    payload = _response_json(response)
    payload_delay = (
        None if payload is None else _payload_retry_after_seconds(payload)
    )
    header_delay = _positive_integer(response.headers.get("Retry-After"))
    delays = tuple(
        delay for delay in (payload_delay, header_delay) if delay is not None
    )
    return max(delays) if delays else None


def _payload_retry_after_seconds(payload: Mapping[str, object]) -> int | None:
    parameters = payload.get("parameters")
    if not isinstance(parameters, dict):
        return None
    retry_after = parameters.get("retry_after")
    if (
        not isinstance(retry_after, int)
        or isinstance(retry_after, bool)
        or retry_after < 1
    ):
        return None
    return retry_after


def _positive_integer(value: str | None) -> int | None:
    if value is None or not value.isascii() or not value.isdecimal():
        return None
    parsed = int(value)
    return parsed if parsed >= 1 else None


__all__ = (
    "KEYCHAIN_READ_TIMEOUT_SECONDS",
    "TELEGRAM_TEXT_LIMIT",
    "NotificationSendError",
    "TelegramNotifier",
)
