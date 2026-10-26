"""
iaedu_client -- minimal Python client for the IAEdu agent chat API.

The API accepts a multipart/form-data POST and answers with a stream of
JSON lines. This module sends the request, reads the stream and assembles
the final text.

Configuration comes from the environment; nothing secret lives in the code:

    IAEDU_API_KEY   required -- your personal API key
    IAEDU_BASE_URL  optional -- defaults to the public endpoint below

Requires Python 3.10+ and the ``requests`` package.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any, BinaryIO
from urllib.parse import urlsplit

import requests

__all__ = ["IaeduClient", "IaeduError", "DEFAULT_BASE_URL"]

DEFAULT_BASE_URL = "https://api.iaedu.pt/agent-chat/api/v1"


class IaeduError(RuntimeError):
    """Configuration problem or network failure."""


class IaeduClient:
    """
    Minimal client for the IAEdu streaming agent API.

    Example::

        client = IaeduClient()  # reads IAEDU_API_KEY and IAEDU_BASE_URL
        result = client.chat(
            agent_id="<agent-id>",
            channel_id="<channel-id>",
            message="Olá",
        )
        print(result["text"])
    """

    def __init__(self, api_key: str | None = None, base_url: str | None = None) -> None:
        # Private attribute: keeps the key out of vars(client) and casual dumps.
        self._api_key = api_key or os.environ.get("IAEDU_API_KEY")
        self.base_url = (
            base_url or os.environ.get("IAEDU_BASE_URL") or DEFAULT_BASE_URL
        ).rstrip("/")

        if not self._api_key:
            raise IaeduError(
                "Missing API key. Set the IAEDU_API_KEY environment variable "
                "or pass api_key= to the constructor."
            )
        # The key travels in a header: refuse to send it over plain HTTP.
        if not self.base_url.startswith("https://"):
            raise IaeduError(
                f"Refusing non-HTTPS base URL {self.base_url!r}: "
                "the API key would be sent in clear text."
            )

    def __repr__(self) -> str:
        # Never expose the key in logs or tracebacks.
        return f"IaeduClient(base_url={self.base_url!r}, api_key=<hidden>)"

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def chat(
        self,
        agent_id: str,
        channel_id: str,
        message: str,
        thread_id: str | None = None,
        user_info: str | dict[str, Any] | None = None,
        user_id: str | None = None,
        user_context: str | None = None,
        image: BinaryIO | None = None,
        timeout: int = 120,
    ) -> dict[str, Any]:
        """
        Send one message to an agent and consume the streamed answer.

        Parameters:
            agent_id      agent identifier (from the IAEdu web interface)
            channel_id    channel identifier (from the IAEdu web interface)
            message       user message text
            thread_id     conversation thread; a new one is generated if None.
                          Reuse it to continue the same conversation.
            user_info     JSON string or dict (serialised to JSON); default "{}"
            user_id       optional user identifier
            user_context  optional free-form key/value string
            image         optional binary file-like object (image attachment)
            timeout       read timeout in seconds (default 120)

        Returns a dict with:
            text          final text assembled from the stream
            raw_chunks    raw stream lines, for inspection
            status        HTTP status code
            latency_ms    total call latency
            rate_limited  True if throttling was detected (429 / headers)
            thread_id     thread used (pass it back to continue)
            model, provider, message_id, stop_reason, error
                          metadata reported by the stream, or None

        Raises IaeduError on missing ids, network failure, or any HTTP 3xx
        (redirects are never followed, so the key is never re-sent).
        """
        if not agent_id:
            raise IaeduError("agent_id is required.")
        if not channel_id:
            raise IaeduError("channel_id is required.")

        thread_id = thread_id or self.generate_thread_id()
        url = f"{self.base_url}/agent/{agent_id}/stream"

        if user_info is None:
            user_info_str = "{}"
        elif isinstance(user_info, str):
            user_info_str = user_info
        else:
            user_info_str = json.dumps(user_info, ensure_ascii=False)

        # Passing every field through ``files`` forces the whole body to be
        # multipart/form-data. (None, value) means "form field, no filename".
        parts: list[tuple[str, tuple[str | None, Any]]] = [
            ("channel_id", (None, channel_id)),
            ("thread_id", (None, thread_id)),
            ("user_info", (None, user_info_str)),
            ("message", (None, message)),
        ]
        if user_id is not None:
            parts.append(("user_id", (None, user_id)))
        if user_context is not None:
            parts.append(("user_context", (None, user_context)))
        if image is not None:
            parts.append(("image", ("image", image)))

        started = time.monotonic()
        try:
            response = requests.post(
                url,
                headers={
                    "x-api-key": self._api_key,
                    "Accept": "application/x-ndjson, text/event-stream, application/json, */*",
                },
                files=parts,
                stream=True,
                # Never follow redirects: requests would re-send the custom
                # x-api-key header to the new location, even another host or
                # plain HTTP.
                allow_redirects=False,
                timeout=(min(30, timeout), timeout),  # (connect, read)
            )
        except requests.RequestException as exc:
            raise IaeduError(f"Network error while contacting the API: {exc}") from exc

        if 300 <= response.status_code < 400:
            location = response.headers.get("Location", "")
            response.close()
            target = urlsplit(location)
            where = f"{target.scheme}://{target.netloc}" if target.netloc else "(no host)"
            raise IaeduError(
                f"Refusing to follow HTTP {response.status_code} redirect to {where}: "
                "the API key is never re-sent. Check IAEDU_BASE_URL."
            )

        rate_limited = self._is_rate_limited(response)

        try:
            # Read the body even on errors: it usually carries the detail.
            raw_chunks = self._read_stream(response)
        finally:
            response.close()

        text, meta = self._parse_stream(raw_chunks)
        latency_ms = int((time.monotonic() - started) * 1000)

        return {
            "text": text,
            "raw_chunks": raw_chunks,
            "status": response.status_code,
            "latency_ms": latency_ms,
            "rate_limited": rate_limited,
            "thread_id": thread_id,
            "model": meta.get("model"),
            "provider": meta.get("provider"),
            "message_id": meta.get("message_id"),
            "stop_reason": meta.get("stop_reason"),
            "error": meta.get("error"),
        }

    @staticmethod
    def generate_thread_id() -> str:
        """Return a new unique thread identifier."""
        return f"thr-{uuid.uuid4()}"

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    @staticmethod
    def _is_rate_limited(response: requests.Response) -> bool:
        """
        Detect throttling. The API's limits are not documented, so we look
        at the conventional signals: HTTP 429, a Retry-After header, or a
        remaining-quota header at zero.
        """
        if response.status_code == 429:
            return True

        headers = {k.lower(): v for k, v in response.headers.items()}
        if "retry-after" in headers:
            return True

        for key in ("x-ratelimit-remaining", "ratelimit-remaining"):
            value = headers.get(key)
            if value is None:
                continue
            try:
                if int(value) <= 0:
                    return True
            except ValueError:
                pass
        return False

    @staticmethod
    def _read_stream(response: requests.Response) -> list[str]:
        """
        Read the body line by line (covers JSON lines and SSE) and return the
        raw lines. Blank lines are separators and are dropped; other lines are
        kept untouched, because leading spaces can be meaningful in token
        streams.
        """
        chunks: list[str] = []
        try:
            for line in response.iter_lines(decode_unicode=True):
                if line is None or line.strip() == "":
                    continue
                chunks.append(line)
        except requests.RequestException as exc:
            # Stream cut mid-way: keep what arrived and record the error.
            chunks.append(f"[stream-error] {exc}")
        return chunks

    @staticmethod
    def _parse_stream(raw_chunks: list[str]) -> tuple[str, dict[str, Any]]:
        """
        Interpret the stream and return (text, metadata).

        Observed format: JSON lines, one object per line, with a "type" field:

            {"type": "start",   "content": "Processing"}          ignored
            {"type": "token",   "content": "O"}                   text delta
            {"type": "message", "content": {"content": "OK",
                 "response_metadata": {"model_name": "...", ...}}} final + meta
            {"type": "done",    "content": "...", "messageId": "..."}  meta
            {"type": "error",   "content": ...}                   meta["error"]

        The text is the concatenation of "token" deltas or, if there are none,
        the "message" content. Lines without a "type" envelope go through a
        defensive path (SSE "data:" lines, other JSON shapes, raw text), so
        the client keeps working if the format changes.
        """
        tokens: list[str] = []
        final_message: str | None = None
        fallback: list[str] = []
        meta: dict[str, Any] = {}
        saw_envelope = False

        for chunk in raw_chunks:
            if chunk.startswith("[stream-error]"):
                meta["error"] = chunk
                continue

            payload = chunk
            if payload.startswith("data:"):
                payload = payload[len("data:"):]
                if payload.startswith(" "):  # SSE: drop one space after "data:"
                    payload = payload[1:]
                if payload in ("[DONE]", "[END]", ""):
                    continue
            elif payload.startswith(("event:", "id:", "retry:", ":")):
                continue  # SSE control lines and comments

            obj: Any = None
            if payload.startswith(("{", "[")):
                try:
                    obj = json.loads(payload)
                except ValueError:
                    obj = None

            if isinstance(obj, dict) and "type" in obj:
                saw_envelope = True
                etype = obj.get("type")
                content = obj.get("content")
                if etype == "token":
                    if isinstance(content, str):
                        tokens.append(content)
                elif etype == "message":
                    if isinstance(content, dict):
                        if isinstance(content.get("content"), str):
                            final_message = content["content"]
                        rmeta = content.get("response_metadata") or {}
                        if rmeta.get("model_name"):
                            meta["model"] = rmeta["model_name"]
                        if rmeta.get("model_provider"):
                            meta["provider"] = rmeta["model_provider"]
                        if rmeta.get("stop_reason"):
                            meta["stop_reason"] = rmeta["stop_reason"]
                    elif isinstance(content, str):
                        final_message = content
                elif etype == "done":
                    if obj.get("messageId"):
                        meta["message_id"] = obj["messageId"]
                elif etype == "error":
                    meta["error"] = content
                continue

            if obj is not None:
                fallback.append(IaeduClient._extract_text_from_obj(obj))
            else:
                fallback.append(payload)

        if saw_envelope:
            text = "".join(tokens) if tokens else (final_message or "")
        else:
            text = "".join(fallback)
        return text, meta

    @staticmethod
    def _extract_text_from_obj(obj: Any) -> str:
        """Best-effort text extraction from an unknown JSON shape."""
        if isinstance(obj, str):
            return obj

        if isinstance(obj, dict):
            for key in ("text", "content", "message", "delta", "token", "output", "answer"):
                value = obj.get(key)
                if isinstance(value, str):
                    return value
                if isinstance(value, dict):
                    inner = value.get("content") or value.get("text")
                    if isinstance(inner, str):
                        return inner

            choices = obj.get("choices")
            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                first = choices[0]
                for key in ("delta", "message"):
                    sub = first.get(key)
                    if isinstance(sub, dict):
                        inner = sub.get("content") or sub.get("text")
                        if isinstance(inner, str):
                            return inner
                if isinstance(first.get("text"), str):
                    return first["text"]

        # Nothing recognised: keep the information rather than dropping it.
        return json.dumps(obj, ensure_ascii=False)
