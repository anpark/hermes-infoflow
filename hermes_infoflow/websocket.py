"""WebSocket transport for receiving Infoflow inbound messages.

This module is intentionally parallel to :mod:`webhook`: it owns only the
Infoflow receive transport and converts accepted frames into the same
``IncomingMessage`` objects consumed by the adapter/bot layers.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import ssl
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

import aiohttp

from .parser import parse_websocket_payload_text
from .utils import gw_log

if TYPE_CHECKING:
    from .itypes import IncomingMessage
    from .serverapi import ServerAPI


logger = logging.getLogger(__name__)

INFOFLOW_WS_GATEWAY = "infoflow-open-gateway.baidu.com"
ENDPOINT_TIMEOUT_SECONDS = 15
FALLBACK_HEARTBEAT_SECONDS = 120
FALLBACK_RECONNECT_SECONDS = 120
FALLBACK_RECONNECT_ATTEMPTS = -1
GROUP_INBOUND_TTL_SECONDS = 5 * 60
GROUP_INBOUND_MAX_SIZE = 2048

FRAME_CONTROL = 0
FRAME_DATA = 1
FRAME_REQUEST = 2
FRAME_RESPONSE = 3


@dataclass
class Header:
    key: str = ""
    value: str = ""


@dataclass
class Frame:
    seq_id: int = 0
    log_id: str = ""
    service: int = 0
    method: int = FRAME_DATA
    headers: list[Header] = field(default_factory=list)
    payload: bytes = b""


class FrameCodecError(ValueError):
    """Raised when an Infoflow websocket frame cannot be decoded."""


class FrameCodec:
    """Minimal protobuf codec for the Infoflow ``Frame`` message."""

    @staticmethod
    def _encode_varint(value: int) -> bytes:
        if value < 0:
            raise FrameCodecError("negative varint")
        out = bytearray()
        while value > 0x7F:
            out.append((value & 0x7F) | 0x80)
            value >>= 7
        out.append(value)
        return bytes(out)

    @staticmethod
    def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
        shift = 0
        value = 0
        while offset < len(data):
            b = data[offset]
            offset += 1
            value |= (b & 0x7F) << shift
            if not (b & 0x80):
                return value, offset
            shift += 7
            if shift > 70:
                raise FrameCodecError("varint too long")
        raise FrameCodecError("truncated varint")

    @classmethod
    def _field_key(cls, field_number: int, wire_type: int) -> bytes:
        return cls._encode_varint((field_number << 3) | wire_type)

    @classmethod
    def _field_varint(cls, field_number: int, value: int) -> bytes:
        return cls._field_key(field_number, 0) + cls._encode_varint(value)

    @classmethod
    def _field_bytes(cls, field_number: int, value: bytes) -> bytes:
        return (
            cls._field_key(field_number, 2)
            + cls._encode_varint(len(value))
            + value
        )

    @classmethod
    def _encode_header(cls, header: Header) -> bytes:
        out = bytearray()
        if header.key:
            out.extend(cls._field_bytes(1, header.key.encode("utf-8")))
        if header.value:
            out.extend(cls._field_bytes(2, header.value.encode("utf-8")))
        return bytes(out)

    @classmethod
    def encode(cls, frame: Frame) -> bytes:
        out = bytearray()
        out.extend(cls._field_varint(1, int(frame.seq_id or 0)))
        if frame.log_id:
            out.extend(cls._field_bytes(2, frame.log_id.encode("utf-8")))
        out.extend(cls._field_varint(3, int(frame.service or 0)))
        out.extend(cls._field_varint(4, int(frame.method or 0)))
        for header in frame.headers:
            out.extend(cls._field_bytes(5, cls._encode_header(header)))
        if frame.payload:
            out.extend(cls._field_bytes(6, frame.payload))
        return bytes(out)

    @classmethod
    def _skip_field(cls, data: bytes, offset: int, wire_type: int) -> int:
        if wire_type == 0:
            _, offset = cls._read_varint(data, offset)
            return offset
        if wire_type == 2:
            length, offset = cls._read_varint(data, offset)
            end = offset + length
            if end > len(data):
                raise FrameCodecError("truncated length-delimited field")
            return end
        raise FrameCodecError(f"unsupported protobuf wire type {wire_type}")

    @classmethod
    def _decode_header(cls, data: bytes) -> Header:
        header = Header()
        offset = 0
        while offset < len(data):
            key, offset = cls._read_varint(data, offset)
            field_number = key >> 3
            wire_type = key & 0x07
            if wire_type != 2:
                offset = cls._skip_field(data, offset, wire_type)
                continue
            length, offset = cls._read_varint(data, offset)
            end = offset + length
            if end > len(data):
                raise FrameCodecError("truncated header field")
            raw = data[offset:end]
            offset = end
            if field_number == 1:
                header.key = raw.decode("utf-8", errors="replace")
            elif field_number == 2:
                header.value = raw.decode("utf-8", errors="replace")
        return header

    @classmethod
    def decode(cls, data: bytes) -> Frame:
        frame = Frame()
        offset = 0
        while offset < len(data):
            key, offset = cls._read_varint(data, offset)
            field_number = key >> 3
            wire_type = key & 0x07
            if field_number in {1, 3, 4} and wire_type == 0:
                value, offset = cls._read_varint(data, offset)
                if field_number == 1:
                    frame.seq_id = int(value)
                elif field_number == 3:
                    frame.service = int(value)
                else:
                    frame.method = int(value)
                continue
            if wire_type == 2:
                length, offset = cls._read_varint(data, offset)
                end = offset + length
                if end > len(data):
                    raise FrameCodecError("truncated frame field")
                raw = data[offset:end]
                offset = end
                if field_number == 2:
                    frame.log_id = raw.decode("utf-8", errors="replace")
                elif field_number == 5:
                    frame.headers.append(cls._decode_header(raw))
                elif field_number == 6:
                    frame.payload = raw
                continue
            offset = cls._skip_field(data, offset, wire_type)
        return frame

    @classmethod
    def json_frame(
        cls,
        *,
        seq_id: int,
        method: int,
        payload: dict[str, Any] | None = None,
        log_id: str = "",
        service: int = 0,
        headers: list[Header] | None = None,
    ) -> Frame:
        raw_payload = b""
        if payload is not None:
            raw_payload = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        return Frame(
            seq_id=seq_id,
            log_id=log_id,
            service=service,
            method=method,
            headers=list(headers or []),
            payload=raw_payload,
        )


class _EndpointError(RuntimeError):
    def __init__(self, message: str, *, permanent: bool = False) -> None:
        super().__init__(message)
        self.permanent = permanent


class _GroupInboundDeduper:
    def __init__(self) -> None:
        self._seen: dict[str, tuple[str, float]] = {}

    def should_skip(self, key: str, incoming_kind: str) -> bool:
        if not key:
            return False
        now = time.time()
        self._prune(now)
        existing = self._seen.get(key)
        if existing is None:
            self._seen[key] = (incoming_kind, now)
            return False
        existing_kind, _seen_at = existing
        if incoming_kind == "forward":
            self._seen[key] = (existing_kind, now)
            return True
        if existing_kind == "mention":
            self._seen[key] = (existing_kind, now)
            return True
        self._seen[key] = ("mention", now)
        return False

    def _prune(self, now: float) -> None:
        expired = [
            key for key, (_kind, seen_at) in self._seen.items()
            if now - seen_at > GROUP_INBOUND_TTL_SECONDS
        ]
        for key in expired:
            self._seen.pop(key, None)
        if len(self._seen) <= GROUP_INBOUND_MAX_SIZE:
            return
        overflow = len(self._seen) - GROUP_INBOUND_MAX_SIZE
        oldest = sorted(self._seen.items(), key=lambda item: item[1][1])[:overflow]
        for key, _value in oldest:
            self._seen.pop(key, None)


class WebSocketReceiver:
    """Receive Infoflow messages over the official websocket gateway."""

    def __init__(
        self,
        *,
        serverapi: ServerAPI,
        sent_message_ids: set[str],
        settings: dict[str, Any],
        on_message: Callable[[IncomingMessage], Awaitable[None]],
        task_set: set[asyncio.Task[Any]] | None = None,
    ) -> None:
        self._serverapi = serverapi
        self._sent_message_ids = sent_message_ids
        self._settings = settings
        self._on_message = on_message
        self._task_set = task_set
        self._session: aiohttp.ClientSession | None = None
        self._own_session = False
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._main_task: asyncio.Task[Any] | None = None
        self._heartbeat_task: asyncio.Task[Any] | None = None
        self._stopped = True
        self._seq_id = 0
        self._connection_generation = 0
        self._connection_id = ""
        self._client_config: dict[str, Any] = {}
        self._group_deduper = _GroupInboundDeduper()
        self._ssl_context = self._make_ssl_context()

    @property
    def is_running(self) -> bool:
        return not self._stopped and self._main_task is not None

    async def start(self) -> None:
        """Connect and start the reconnect/read loop (idempotent)."""
        if self.is_running:
            return
        self._stopped = False
        self._session = self._serverapi.http_session
        if self._session is None:
            self._session = aiohttp.ClientSession()
            self._own_session = True
        try:
            await self._connect_once()
        except Exception:
            if self._own_session and self._session is not None:
                with contextlib.suppress(Exception):
                    await self._session.close()
            self._session = None
            self._own_session = False
            self._stopped = True
            raise
        self._main_task = asyncio.create_task(self._listen_with_reconnect())
        gw_log().info("[ws:connect] initial connection established")

    async def stop(self) -> None:
        """Stop receiving and prevent any later reconnect."""
        if self._stopped:
            return
        self._stopped = True
        gw_log().info("[ws:disconnect] stopping websocket receiver")
        await self._stop_heartbeat()
        task = self._main_task
        self._main_task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self._close_ws()
        if self._own_session and self._session is not None:
            with contextlib.suppress(Exception):
                await self._session.close()
        self._session = None
        self._own_session = False

    async def _listen_with_reconnect(self) -> None:
        attempts = 0
        while not self._stopped:
            stop_reconnect = False
            try:
                if self._ws is None or self._ws.closed:
                    await self._connect_once()
                await self._read_loop(self._connection_generation)
                attempts = 0
            except asyncio.CancelledError:
                raise
            except _EndpointError as exc:
                gw_log().error("[ws:error] websocket endpoint error: %s", exc)
                if exc.permanent:
                    stop_reconnect = True
            except Exception as exc:
                if not self._stopped:
                    gw_log().warning(
                        "[ws:error] websocket read loop failed: %s",
                        exc,
                        exc_info=True,
                    )
            await self._stop_heartbeat()
            await self._close_ws()
            if self._stopped or stop_reconnect:
                break
            max_attempts = self._max_reconnect_attempts()
            if max_attempts != -1 and attempts >= max_attempts:
                gw_log().error("[ws:error] websocket reached reconnect limit=%s", max_attempts)
                break
            attempts += 1
            delay = self._reconnect_delay_seconds()
            gw_log().warning(
                "[ws:disconnect] websocket disconnected; reconnecting in %.1fs attempt=%s",
                delay,
                attempts,
            )
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise
            if self._stopped:
                break
        await self._stop_heartbeat()
        await self._close_ws()
        self._stopped = True

    async def _connect_once(self) -> None:
        if self._session is None:
            raise RuntimeError("websocket receiver session is not initialized")
        endpoint = await self._fetch_ws_endpoint(self._session)
        raw_url = str(endpoint.get("url") or "")
        if not raw_url:
            raise _EndpointError("endpoint response missing websocket url", permanent=True)
        self._client_config = (
            endpoint.get("client_config")
            if isinstance(endpoint.get("client_config"), dict)
            else {}
        )
        parsed = urlparse(raw_url)
        self._connection_id = (parse_qs(parsed.query).get("connection_id") or [""])[0]
        self._connection_generation += 1
        generation = self._connection_generation
        gw_log().info(
            "[ws:connect] connecting gateway=%s connection_id=%s",
            INFOFLOW_WS_GATEWAY,
            self._connection_id or "?",
        )
        ssl_arg: ssl.SSLContext | bool | None = (
            self._ssl_context if parsed.scheme == "wss" else None
        )
        self._ws = await self._session.ws_connect(
            raw_url,
            heartbeat=None,
            compress=0,
            ssl=ssl_arg,
            timeout=30,
        )
        if generation == self._connection_generation and not self._stopped:
            self._start_heartbeat()
            gw_log().info(
                "[ws:connect] websocket connected connection_id=%s",
                self._connection_id or "?",
            )

    async def _fetch_ws_endpoint(
        self,
        session: aiohttp.ClientSession,
    ) -> dict[str, Any]:
        url = f"https://{INFOFLOW_WS_GATEWAY}/open/ws/endpoint"
        body = {
            "app_key": str(self._settings.get("app_key") or ""),
            "app_secret": str(self._settings.get("app_secret") or ""),
        }
        timeout = aiohttp.ClientTimeout(total=ENDPOINT_TIMEOUT_SECONDS)
        gw_log().info("[ws:connect] phase1 POST %s", url)
        try:
            async with session.post(
                url,
                json=body,
                timeout=timeout,
                ssl=self._ssl_context,
            ) as resp:
                text = await resp.text()
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise _EndpointError(
                        f"endpoint returned non-json response status={resp.status}",
                        permanent=400 <= resp.status < 500,
                    ) from exc
                if resp.status != 200:
                    raise _EndpointError(
                        f"endpoint HTTP {resp.status}: {payload}",
                        permanent=resp.status in {400, 401, 403, 404},
                    )
                if payload.get("code") != 0 or not isinstance(payload.get("data"), dict):
                    raise _EndpointError(
                        f"endpoint rejected request: {payload.get('msg') or payload}",
                        permanent=True,
                    )
                return dict(payload["data"])
        except asyncio.TimeoutError as exc:
            raise _EndpointError(
                f"endpoint timeout after {ENDPOINT_TIMEOUT_SECONDS}s"
            ) from exc
        except aiohttp.ClientError as exc:
            raise _EndpointError(f"endpoint request failed: {exc}") from exc

    async def _read_loop(self, generation: int) -> None:
        ws = self._ws
        if ws is None:
            return
        async for message in ws:
            if self._stopped or generation != self._connection_generation:
                return
            if message.type == aiohttp.WSMsgType.BINARY:
                await self._handle_frame_bytes(bytes(message.data))
            elif message.type == aiohttp.WSMsgType.TEXT:
                await self._handle_frame_bytes(str(message.data).encode("utf-8"))
            elif message.type in {
                aiohttp.WSMsgType.CLOSE,
                aiohttp.WSMsgType.CLOSED,
                aiohttp.WSMsgType.CLOSING,
            }:
                return
            elif message.type == aiohttp.WSMsgType.ERROR:
                err = ws.exception()
                raise RuntimeError(f"websocket error: {err}") from err

    async def _handle_frame_bytes(self, data: bytes) -> None:
        try:
            frame = FrameCodec.decode(data)
        except Exception as exc:
            gw_log().warning("[ws:frame] decode failed len=%d: %s", len(data), exc)
            return
        gw_log().info(
            "[ws:frame] method=%s seq=%s payloadLen=%d",
            frame.method,
            frame.seq_id,
            len(frame.payload or b""),
        )
        if frame.method == FRAME_CONTROL:
            await self._handle_control_frame(frame)
            return
        if frame.method != FRAME_DATA:
            return

        raw_text = frame.payload.decode("utf-8", errors="replace") if frame.payload else ""
        try:
            should_ack = await self._handle_data_payload(raw_text)
        except Exception as exc:
            gw_log().error(
                "[ws:inbound] handler error seq=%s: %s",
                frame.seq_id,
                exc,
                exc_info=True,
            )
            should_ack = True
        if should_ack:
            await self._send_ack(frame)

    async def _handle_data_payload(self, raw_text: str) -> bool:
        parsed = parse_websocket_payload_text(
            raw_text,
            account=self._serverapi.parser_account,
            sent_message_ids=self._sent_message_ids,
        )
        if parsed.kind == "invalid":
            gw_log().warning(
                "[ws:frame] payload parse failed: %s",
                parsed.diagnostic_reason or "invalid",
            )
            return False
        if parsed.kind == "ignored" or parsed.inbound is None:
            gw_log().info(
                "[ws:inbound] payload ignored reason=%s",
                parsed.diagnostic_reason or "ignored",
            )
            return True
        if (
            parsed.inbound.chat_type == "group"
            and parsed.transport_dedup_key
            and self._group_deduper.should_skip(
                parsed.transport_dedup_key,
                parsed.transport_seen_kind,
            )
        ):
            gw_log().info(
                "[ws:inbound] duplicate group frame skipped key=%s kind=%s",
                parsed.transport_dedup_key,
                parsed.transport_seen_kind,
            )
            return True
        msg = self._serverapi.to_incoming(parsed.inbound)
        self._schedule_message(msg)
        return True

    def _schedule_message(self, msg: IncomingMessage) -> None:
        task = asyncio.ensure_future(self._on_message(msg))
        if self._task_set is not None:
            self._task_set.add(task)
            task.add_done_callback(self._task_set.discard)

    async def _handle_control_frame(self, frame: Frame) -> None:
        control_type = ""
        for header in frame.headers:
            if header.key == "type":
                control_type = header.value
                break
        if not control_type and frame.payload:
            with contextlib.suppress(Exception):
                payload = json.loads(frame.payload.decode("utf-8", errors="replace"))
                if isinstance(payload, dict):
                    control_type = str(payload.get("type") or "")
        if control_type == "pong":
            gw_log().debug("[ws:heartbeat] pong seq=%s", frame.seq_id)
        elif control_type == "ping":
            gw_log().debug("[ws:heartbeat] ping seq=%s", frame.seq_id)
            await self._send_frame(
                FrameCodec.json_frame(
                    seq_id=self._next_seq_id(),
                    log_id=frame.log_id,
                    service=frame.service,
                    method=FRAME_CONTROL,
                    headers=[Header(key="type", value="pong")],
                    payload={},
                )
            )

    def _start_heartbeat(self) -> None:
        if self._heartbeat_task is not None and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def _stop_heartbeat(self) -> None:
        task = self._heartbeat_task
        self._heartbeat_task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _heartbeat_loop(self) -> None:
        while not self._stopped:
            await self._send_ping()
            await asyncio.sleep(self._heartbeat_interval_seconds())

    async def _send_ping(self) -> None:
        if self._ws is None or self._ws.closed:
            return
        frame = FrameCodec.json_frame(
            seq_id=self._next_seq_id(),
            method=FRAME_CONTROL,
            headers=[Header(key="type", value="ping")],
            payload={},
        )
        await self._send_frame(frame)

    async def _send_ack(self, original: Frame) -> None:
        if self._ws is None or self._ws.closed:
            return
        frame = FrameCodec.json_frame(
            seq_id=self._next_seq_id(),
            log_id=original.log_id,
            service=0,
            method=FRAME_DATA,
            headers=[Header(key="type", value="event_ack")],
            payload={"ackSeqId": original.seq_id, "code": 0, "message": "ok"},
        )
        await self._send_frame(frame)
        gw_log().info("[ws:frame] ACK sent seq=%s ackSeq=%s", frame.seq_id, original.seq_id)

    async def _send_frame(self, frame: Frame) -> None:
        if self._ws is None or self._ws.closed:
            return
        await self._ws.send_bytes(FrameCodec.encode(frame))

    def _next_seq_id(self) -> int:
        self._seq_id = (self._seq_id + 1) % 9_007_199_254_740_991
        if self._seq_id <= 0:
            self._seq_id = 1
        return self._seq_id

    def _heartbeat_interval_seconds(self) -> float:
        raw = self._client_config.get("ping_interval")
        with contextlib.suppress(TypeError, ValueError):
            value = float(raw)
            if value > 0:
                return value
        return FALLBACK_HEARTBEAT_SECONDS

    def _reconnect_delay_seconds(self) -> float:
        raw_interval = self._client_config.get("reconnect_interval")
        raw_nonce = self._client_config.get("reconnect_nonce")
        with contextlib.suppress(TypeError, ValueError):
            interval = float(raw_interval)
            nonce = max(0.0, float(raw_nonce or 0))
            if interval > 0:
                return interval + random.random() * nonce
        return FALLBACK_RECONNECT_SECONDS

    def _max_reconnect_attempts(self) -> int:
        raw = self._client_config.get("reconnect_count")
        with contextlib.suppress(TypeError, ValueError):
            return int(raw)
        return FALLBACK_RECONNECT_ATTEMPTS

    async def _close_ws(self) -> None:
        ws = self._ws
        self._ws = None
        if ws is not None and not ws.closed:
            with contextlib.suppress(Exception):
                await ws.close()

    @staticmethod
    def _make_ssl_context() -> ssl.SSLContext:
        context = ssl.create_default_context()
        legacy = getattr(ssl, "OP_LEGACY_SERVER_CONNECT", 0)
        if legacy:
            context.options |= legacy
        return context


__all__ = [
    "ENDPOINT_TIMEOUT_SECONDS",
    "FALLBACK_HEARTBEAT_SECONDS",
    "FALLBACK_RECONNECT_ATTEMPTS",
    "FALLBACK_RECONNECT_SECONDS",
    "Frame",
    "FrameCodec",
    "FrameCodecError",
    "Header",
    "INFOFLOW_WS_GATEWAY",
    "WebSocketReceiver",
]
