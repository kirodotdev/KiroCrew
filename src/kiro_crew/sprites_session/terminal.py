"""The dashboard terminal's bridge to a shell in a session's sprite.

The terminal panel runs this module as its PTY child (the argv comes from
``session_browse.remote_terminal_argv``). It opens a Sprites exec WebSocket in
TTY mode and copies bytes between it and its own stdin/stdout, so the panel
needs nothing Sprites-specific.

The Sprites API token is read here, from the gateway vault under the profile's
secret name. It is never in argv (visible to every local user in ``ps``), an
environment variable, or a file, and it is sent only as the ``Authorization``
header to the API. Only names the gateway minted are accepted as the sprite.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import signal
import struct
import sys
from typing import Any, Optional

from kiro_crew import platform_compat

#: The sprite names this gateway mints (see ``launch.NAME_PREFIX``).
SPRITE_NAME_RE = re.compile(r"kirocrew-[0-9a-f]{16}")
_SECRET_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_CHUNK = 64 * 1024


def bridge_argv(sprite: str, token_secret: str, workdir: str) -> list[str]:
    """argv for the terminal panel's PTY child; validated, so it cannot carry flags."""
    if not SPRITE_NAME_RE.fullmatch(sprite or ""):
        raise ValueError("not a sprite this gateway created")
    if not _SECRET_NAME_RE.fullmatch(token_secret or ""):
        raise ValueError("not a secret name")
    argv = platform_compat.isolated_python_argv(
        "-P",
        "-m",
        "kiro_crew.sprites_session.terminal",
        "--sprite",
        sprite,
        "--token-secret",
        token_secret,
    )
    if workdir:
        argv += ["--dir", workdir]
    return argv


def _window_size(fd: int) -> tuple[int, int]:
    """``(cols, rows)`` of the PTY on ``fd``, or 80x24 when it is not a terminal."""
    if platform_compat.IS_POSIX and os.isatty(fd):
        import fcntl
        import termios

        with contextlib.suppress(OSError):
            rows, cols, _x, _y = struct.unpack(
                "HHHH", fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8)
            )
            if rows and cols:
                return cols, rows
    return 80, 24


@contextlib.contextmanager
def _raw(fd: int) -> Any:
    """Raw mode on a terminal stdin, so the remote shell does echo and line editing."""
    if not (platform_compat.IS_POSIX and os.isatty(fd)):
        yield
        return
    import termios
    import tty

    saved = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        yield
    finally:
        # TCSANOW, not TCSADRAIN: draining waits for the panel to read output it
        # may never read once the shell has exited, and the bridge would hang.
        termios.tcsetattr(fd, termios.TCSANOW, saved)


async def run(
    sprite: str,
    token: str,
    workdir: str,
    *,
    api_url: Optional[str] = None,
    stdin_fd: int = 0,
    stdout_fd: int = 1,
) -> int:
    """Relay ``stdin_fd``/``stdout_fd`` (the PTY) to a TTY exec until either side ends."""
    from kiro_crew.sprites_session.client import API_URL, SpritesClient

    client = SpritesClient(token, base_url=api_url or API_URL)
    cols, rows = _window_size(stdin_fd)
    session, ws = await client.open_tty(
        sprite, ["bash", "-l"], cols=cols, rows=rows, workdir=workdir
    )
    loop = asyncio.get_running_loop()
    stdin_done = asyncio.Event()
    code = 0

    def on_stdin() -> None:
        try:
            data = os.read(stdin_fd, _CHUNK)
        except OSError:
            data = b""
        if not data:
            loop.remove_reader(stdin_fd)
            stdin_done.set()
            return
        asyncio.ensure_future(ws.send_bytes(data))

    def on_resize() -> None:
        new_cols, new_rows = _window_size(stdin_fd)
        asyncio.ensure_future(
            ws.send_str(json.dumps({"type": "resize", "cols": new_cols, "rows": new_rows}))
        )

    import aiohttp

    try:
        loop.add_reader(stdin_fd, on_stdin)
        if platform_compat.IS_POSIX:
            loop.add_signal_handler(signal.SIGWINCH, on_resize)
        receiver = asyncio.ensure_future(_receive(ws, aiohttp, stdout_fd))
        waiter = asyncio.ensure_future(stdin_done.wait())
        done, _pending = await asyncio.wait({receiver, waiter}, return_when=asyncio.FIRST_COMPLETED)
        if receiver in done:
            code = receiver.result()
        else:
            receiver.cancel()  # the panel closed: end the remote shell's input too
        waiter.cancel()
    finally:
        with contextlib.suppress(Exception):
            loop.remove_reader(stdin_fd)
        if platform_compat.IS_POSIX:
            with contextlib.suppress(Exception):
                loop.remove_signal_handler(signal.SIGWINCH)
        with contextlib.suppress(Exception):
            await ws.close()
        await session.close()
    return code


async def _receive(ws: Any, aiohttp: Any, stdout_fd: int) -> int:
    code = 0
    async for msg in ws:
        if msg.type == aiohttp.WSMsgType.BINARY:
            os.write(stdout_fd, msg.data)
        elif msg.type == aiohttp.WSMsgType.TEXT:
            with contextlib.suppress(ValueError, TypeError, AttributeError):
                info = json.loads(msg.data)
                if info.get("type") == "exit":
                    code = int(info.get("exit_code") or 0)
                    break
        elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
            break
    return code


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="kiro_crew.sprites_session.terminal")
    parser.add_argument("--sprite", required=True)
    parser.add_argument("--token-secret", required=True)
    parser.add_argument("--dir", default="")
    # Tests point it at a loopback fake; anything else is the real API only.
    parser.add_argument("--api-url", default="", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not SPRITE_NAME_RE.fullmatch(args.sprite) or not _SECRET_NAME_RE.fullmatch(
        args.token_secret
    ):
        print("sprites terminal: invalid sprite or secret name", file=sys.stderr)
        return 2
    if args.api_url and not re.fullmatch(r"http://127\.0\.0\.1:\d+", args.api_url):
        print("sprites terminal: only a loopback test API may override the URL", file=sys.stderr)
        return 2
    from kiro_crew.sprites_session.launch import _vault_token

    token = _vault_token(args.token_secret)
    if not token:
        print(
            f"sprites terminal: no Sprites API token is saved as {args.token_secret}",
            file=sys.stderr,
        )
        return 1
    from kiro_crew.session_environment import SessionEnvironmentUnavailable

    try:
        with _raw(0):
            return asyncio.run(run(args.sprite, token, args.dir, api_url=args.api_url or None))
    except SessionEnvironmentUnavailable as exc:
        print(f"\r\nsprites terminal: {exc}\r\n", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
