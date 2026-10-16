"""A minimal Fly.io Sprites API client: lifecycle, exec and the TCP proxy.

Only what a session environment needs (see https://docs.fly.io/sprites/api):

* ``GET/POST/DELETE /v1/sprites[/{name}]`` for the sprite's lifecycle;
* the exec WebSocket (``/v1/sprites/{name}/exec``, non-TTY) to run one command
  with stdin and collect its output and exit code. Binary frames carry a one-byte
  stream id: 0 stdin, 1 stdout, 2 stderr, 3 exit code, 4 stdin EOF;
* the proxy WebSocket (``/v1/sprites/{name}/proxy``), which after a JSON
  ``{"host", "port"}`` init and a ``{"status": "connected"}`` answer carries raw
  TCP bytes, to dial the guest worker's loopback port.

The token is sent only as the ``Authorization`` header to the configured API
host and is never logged, framed or included in an error. A command line travels
in the exec URL's query string, so callers must keep secrets on stdin.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any, Optional
from urllib.parse import quote, urlencode

import aiohttp

from kiro_crew.session_environment import SessionEnvironmentUnavailable

logger = logging.getLogger(__name__)

API_URL = "https://api.sprites.dev"
_HTTP_TIMEOUT = 60.0
_EXEC_TIMEOUT = 120.0
_EXEC_OUTPUT_MAX = 8 * 1024 * 1024
_PROXY_INIT_TIMEOUT = 15.0
_WS_MAX_MSG = 16 * 1024 * 1024
_STDIN, _STDOUT, _STDERR, _EXIT, _STDIN_EOF = 0, 1, 2, 3, 4
_STDIN_CHUNK = 256 * 1024


class SpritesError(SessionEnvironmentUnavailable):
    """A Sprites API call failed. The message never carries the token."""


def _ws_base(base_url: str) -> str:
    if base_url.startswith("https://"):
        return "wss://" + base_url[len("https://") :]
    if base_url.startswith("http://"):
        return "ws://" + base_url[len("http://") :]
    raise ValueError("the Sprites API URL must be http(s)")


class SpritesClient:
    """One organization's Sprites API, authenticated by its token."""

    def __init__(self, token: str, *, base_url: str = API_URL) -> None:
        if not token:
            raise SpritesError("no Sprites API token")
        self._token = token
        self.base_url = base_url.rstrip("/")

    def __repr__(self) -> str:  # never the token
        return f"SpritesClient({self.base_url!r})"

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    def _url(self, path: str) -> str:
        return self.base_url + path

    @staticmethod
    def _sprite_path(name: str) -> str:
        return "/v1/sprites/" + quote(name, safe="")

    async def _request(
        self, method: str, path: str, *, body: Any = None, params: Any = None
    ) -> tuple[int, Any]:
        timeout = aiohttp.ClientTimeout(total=_HTTP_TIMEOUT)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.request(
                    method,
                    self._url(path),
                    json=body,
                    params=params,
                    headers=self._headers(),
                    allow_redirects=False,
                ) as resp:
                    text = await resp.text()
                    try:
                        data = json.loads(text) if text else None
                    except ValueError:
                        data = None
                    return resp.status, data
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise SpritesError(f"could not reach the Sprites API ({type(exc).__name__})") from exc

    @staticmethod
    def _failed(what: str, status: int, data: Any) -> SpritesError:
        if status == 401:
            return SpritesError("the Sprites API token was refused; save a valid one")
        detail = ""
        if isinstance(data, dict):
            detail = str(data.get("error") or data.get("message") or "")[:200]
        return SpritesError(
            f"Sprites {what} failed (HTTP {status}){': ' + detail if detail else ''}"
        )

    async def list_sprites(self, *, prefix: str = "", max_results: int = 1) -> dict[str, Any]:
        params: dict[str, str] = {"max_results": str(max_results)}
        if prefix:
            params["prefix"] = prefix
        status, data = await self._request("GET", "/v1/sprites", params=params)
        if status != 200 or not isinstance(data, dict):
            raise self._failed("list", status, data)
        return data

    async def get_sprite(self, name: str) -> Optional[dict[str, Any]]:
        status, data = await self._request("GET", self._sprite_path(name))
        if status == 404:
            return None
        if status != 200 or not isinstance(data, dict):
            raise self._failed("lookup", status, data)
        return data

    async def create_sprite(self, name: str) -> dict[str, Any]:
        # URL auth stays "sprite" (the default): the sprite's HTTP endpoint is
        # never made public by a session.
        status, data = await self._request(
            "POST", "/v1/sprites", body={"name": name, "url_settings": {"auth": "sprite"}}
        )
        if status not in (200, 201) or not isinstance(data, dict):
            raise self._failed("create", status, data)
        return data

    async def delete_sprite(self, name: str) -> bool:
        """True when deleted, False when it was already gone."""
        status, data = await self._request("DELETE", self._sprite_path(name))
        if status == 404:
            return False
        if status not in (200, 204):
            raise self._failed("delete", status, data)
        return True

    # -- WebSockets ----------------------------------------------------------------
    async def _ws(self, path: str, params: list[tuple[str, str]]) -> tuple[Any, Any]:
        session = aiohttp.ClientSession()
        url = _ws_base(self.base_url) + path + ("?" + urlencode(params) if params else "")
        try:
            ws = await session.ws_connect(
                url, headers=self._headers(), max_msg_size=_WS_MAX_MSG, heartbeat=15.0
            )
        except aiohttp.WSServerHandshakeError as exc:
            await session.close()
            if exc.status == 401:
                raise SpritesError("the Sprites API token was refused; save a valid one") from exc
            raise SpritesError(
                f"the Sprites API refused the connection (HTTP {exc.status})"
            ) from exc
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            await session.close()
            raise SpritesError(f"could not reach the Sprites API ({type(exc).__name__})") from exc
        return session, ws

    async def exec(
        self,
        name: str,
        argv: list[str],
        stdin: bytes = b"",
        *,
        timeout: float = _EXEC_TIMEOUT,
        max_output: int = _EXEC_OUTPUT_MAX,
    ) -> tuple[int, bytes, bytes]:
        """Run ``argv`` in the sprite; ``(exit code, stdout, stderr)``."""
        if not argv:
            raise ValueError("a command is required")
        params = [("cmd", arg) for arg in argv] + [("path", argv[0]), ("stdin", "true")]
        session, ws = await self._ws(self._sprite_path(name) + "/exec", params)
        out, err = bytearray(), bytearray()
        code: Optional[int] = None

        async def feed() -> None:
            for start in range(0, len(stdin), _STDIN_CHUNK):
                await ws.send_bytes(bytes([_STDIN]) + stdin[start : start + _STDIN_CHUNK])
            await ws.send_bytes(bytes([_STDIN_EOF]))

        async def collect() -> None:
            nonlocal code
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.BINARY and msg.data:
                    stream, payload = msg.data[0], msg.data[1:]
                    if stream == _STDOUT:
                        out.extend(payload)
                    elif stream == _STDERR:
                        err.extend(payload)
                    elif stream == _EXIT:
                        code = payload[0] if payload else 0
                        return
                    if len(out) > max_output or len(err) > max_output:
                        raise SpritesError("the sprite command returned too much data")
                elif msg.type == aiohttp.WSMsgType.TEXT:
                    with contextlib.suppress(ValueError, TypeError, AttributeError):
                        info = json.loads(msg.data)
                        if info.get("type") == "exit":
                            code = int(info.get("exit_code") or 0)
                            return
                elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                    return

        try:
            feeder = asyncio.ensure_future(feed())
            try:
                await asyncio.wait_for(collect(), timeout)
            finally:
                feeder.cancel()
                with contextlib.suppress(BaseException):
                    await feeder
        except asyncio.TimeoutError as exc:
            raise SpritesError("the sprite command did not finish in time") from exc
        finally:
            with contextlib.suppress(Exception):
                await ws.close()
            await session.close()
        if code is None:
            raise SpritesError("the sprite closed the command before it reported an exit code")
        return code, bytes(out), bytes(err)

    async def open_tty(
        self, name: str, argv: list[str], *, cols: int, rows: int, workdir: str = ""
    ) -> tuple[Any, Any]:
        """An interactive TTY exec: ``(session, ws)``, raw bytes both ways.

        In TTY mode binary frames carry raw terminal bytes with no stream-id
        prefix; the server sends a JSON ``{"type": "exit"}`` text frame at the
        end, and accepts ``{"type": "resize", "cols", "rows"}``. The caller owns
        closing both.
        """
        params = [("cmd", arg) for arg in argv] + [
            ("path", argv[0]),
            ("tty", "true"),
            ("rows", str(max(1, int(rows)))),
            ("cols", str(max(1, int(cols)))),
            ("stdin", "true"),
        ]
        if workdir:
            params.append(("dir", workdir))
        return await self._ws(self._sprite_path(name) + "/exec", params)

    async def proxy(self, name: str, port: int) -> tuple[asyncio.StreamReader, "_WsWriter"]:
        """A raw byte stream to ``127.0.0.1:port`` inside the sprite."""
        session, ws = await self._ws(self._sprite_path(name) + "/proxy", [])
        try:
            # The guest worker binds 127.0.0.1; "localhost" may resolve to ::1 first.
            await ws.send_str(json.dumps({"host": "127.0.0.1", "port": int(port)}))
            reply = await asyncio.wait_for(ws.receive(), _PROXY_INIT_TIMEOUT)
            status = ""
            if reply.type == aiohttp.WSMsgType.TEXT:
                with contextlib.suppress(ValueError, AttributeError):
                    status = str(json.loads(reply.data).get("status") or "")
            if status != "connected":
                raise SpritesError(f"the sprite proxy did not connect to port {int(port)}")
        except BaseException:
            with contextlib.suppress(Exception):
                await ws.close()
            await session.close()
            raise
        return _ws_stream(session, ws)


class _WsWriter:
    """StreamWriter-shaped view of a proxy WebSocket: ``write``/``close``/``is_closing``."""

    def __init__(self, session: Any, ws: Any, reader_task: "asyncio.Future[None]") -> None:
        self._session = session
        self._ws = ws
        self._reader_task = reader_task
        self._queue: asyncio.Queue[Optional[bytes]] = asyncio.Queue()
        self._closing = False
        self._sender = asyncio.ensure_future(self._send_loop())

    async def _send_loop(self) -> None:
        try:
            while (data := await self._queue.get()) is not None:
                await self._ws.send_bytes(data)
        except Exception:  # noqa: BLE001 -- a dead socket ends the stream
            pass
        finally:
            self._closing = True
            self._reader_task.cancel()
            with contextlib.suppress(Exception):
                await self._ws.close()
            with contextlib.suppress(Exception):
                await self._session.close()

    def write(self, data: bytes) -> None:
        if not self._closing and data:
            self._queue.put_nowait(bytes(data))

    async def drain(self) -> None:
        return None

    def is_closing(self) -> bool:
        return self._closing or self._ws.closed

    def close(self) -> None:
        if not self._closing:
            self._closing = True
            self._queue.put_nowait(None)

    async def wait_closed(self) -> None:
        with contextlib.suppress(BaseException):
            await self._sender


def _ws_stream(session: Any, ws: Any) -> tuple[asyncio.StreamReader, _WsWriter]:
    reader = asyncio.StreamReader(limit=_WS_MAX_MSG)

    async def pump() -> None:
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.BINARY:
                    reader.feed_data(msg.data)
                elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                    break
        except Exception:  # noqa: BLE001 -- the reader sees EOF either way
            pass
        finally:
            reader.feed_eof()

    task = asyncio.ensure_future(pump())
    writer = _WsWriter(session, ws, task)
    task.add_done_callback(lambda _t: writer.close())
    return reader, writer
