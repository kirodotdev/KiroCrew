"""Small terminal-UI helpers for the cloud wizard — colors, steps, prompts.

Kept dependency-free (stdlib only) and TTY-aware: colors and the live spinner
degrade to plain text when stdout is not a terminal or ``NO_COLOR`` is set, so
the same code works interactively and under ``curl | bash`` / CI.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from typing import Optional

_ENABLE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(code: str) -> str:
    return code if _ENABLE_COLOR else ""


BOLD = _c("\033[1m")
DIM = _c("\033[2m")
RESET = _c("\033[0m")
RED = _c("\033[31m")
GREEN = _c("\033[32m")
YELLOW = _c("\033[33m")
BLUE = _c("\033[34m")
MAGENTA = _c("\033[35m")
CYAN = _c("\033[36m")


BANNER = f"""{MAGENTA}{BOLD}
   _  ___            ___                   ___ _             _
  | |/ (_)_ _ ___   / __|_ _ _____ __ __  / __| |___ _  _ __| |
  | ' <| | '_/ _ \\ | (__| '_/ -_) V  V / | (__| / _ \\ || / _` |
  |_|\\_\\_|_| \\___/  \\___|_| \\___|\\_/\\_/   \\___|_\\___/\\_,_\\__,_|
{RESET}
  {DIM}Run your personal AI agent on your own AWS - in minutes.{RESET}
"""


class Steps:
    """Numbered step headers: ``[3/6] Choose a size``."""

    def __init__(self, total: int) -> None:
        self.total = total
        self.n = 0

    def step(self, title: str) -> None:
        self.n += 1
        print(flush=True)
        print(f"  {BLUE}{BOLD}[{self.n}/{self.total}]{RESET} {BOLD}{title}{RESET}", flush=True)
        print(f"  {DIM}{'─' * 52}{RESET}", flush=True)


def info(msg: str) -> None:
    print(f"  {DIM}→{RESET} {msg}", flush=True)


def ok(msg: str) -> None:
    print(f"  {GREEN}✓{RESET} {msg}", flush=True)


def warn(msg: str) -> None:
    print(f"  {YELLOW}⚠{RESET} {msg}", flush=True)


def fail(msg: str) -> None:
    print(f"  {RED}✗{RESET} {msg}", flush=True)


def detail(msg: str) -> None:
    print(f"    {DIM}{msg}{RESET}", flush=True)


def note(msg: str) -> None:
    print(f"  {msg}", flush=True)


def prompt(text: str, default: str = "") -> str:
    """Read a line with an optional default (shown in brackets)."""
    suffix = f" {DIM}[{default}]{RESET}" if default else ""
    try:
        val = input(f"  {CYAN}?{RESET} {text}{suffix}: ").strip()
    except EOFError:
        print()
        return default
    except KeyboardInterrupt:
        print()
        raise
    return val or default


def confirm(text: str, default: bool = True) -> bool:
    """Y/n confirmation. Default shown capitalized; end of input is always no."""
    d = "Y/n" if default else "y/N"
    try:
        val = input(f"  {CYAN}?{RESET} {text} {DIM}[{d}]{RESET} ").strip().lower()
    except EOFError:
        print()
        return False
    except KeyboardInterrupt:
        print()
        raise
    if not val:
        return default
    return val in ("y", "yes")


def choose(title: str, options: list[tuple[str, str]], default_index: int = 0) -> int:
    """Single-choice radio menu. ``options`` = list of (label, sublabel).

    Returns the chosen 0-based index. Non-interactive input returns the default.
    """
    if _supports_cursor_menu():
        return _choose_cursor(title, options, default_index)
    return _choose_prompt(title, options, default_index)


def _choose_prompt(title: str, options: list[tuple[str, str]], default_index: int = 0) -> int:
    """Numbered fallback picker for non-TTY input."""
    print(f"  {BOLD}{title}{RESET}")
    for i, (label, sub) in enumerate(options):
        marker = f"{GREEN}◉{RESET}" if i == default_index else f"{DIM}○{RESET}"
        star = f" {GREEN}(recommended){RESET}" if i == default_index else ""
        print(f"    {marker} {BOLD}{label}{RESET}{star}")
        if sub:
            print(f"       {DIM}{sub}{RESET}")
    # Piped answers cannot be asked again: re-reading would take the line meant
    # for the next question, so a bad piped answer falls back to the default.
    piped = not sys.stdin.isatty()
    while True:
        raw = prompt(f"Choose 1-{len(options)}", str(default_index + 1))
        if not raw:
            return default_index
        try:
            idx = int(raw) - 1
        except ValueError:
            idx = -1
            hint = "Enter a number."
        else:
            hint = f"Enter a number between 1 and {len(options)}."
        if 0 <= idx < len(options):
            return idx
        if piped:
            warn(
                f"{raw!r} is not a choice; using {default_index + 1} ({options[default_index][0]})."
            )
            return default_index
        warn(hint)


def _supports_cursor_menu() -> bool:
    """True when stdin/stdout can support a raw-mode cursor-key menu.

    POSIX-only: the cursor picker uses termios/tty, which don't exist on
    Windows — there (install.ps1 client) the numbered fallback is used.
    """
    return os.name == "posix" and sys.stdin.isatty() and sys.stdout.isatty()


def _choose_cursor(title: str, options: list[tuple[str, str]], default_index: int = 0) -> int:
    """TTY picker supporting arrow keys, Vim keys, digits, and Enter."""
    import termios
    import tty

    selected = max(0, min(default_index, len(options) - 1))
    rendered_lines = 0

    def render() -> None:
        nonlocal rendered_lines
        lines = _choice_lines(title, options, selected)
        if rendered_lines:
            sys.stdout.write(f"\033[{rendered_lines}A")
        for line in lines:
            sys.stdout.write("\033[2K\r")
            sys.stdout.write(line)
            sys.stdout.write("\n")
        sys.stdout.flush()
        rendered_lines = len(lines)

    fd = sys.stdin.fileno()
    old_attrs = termios.tcgetattr(fd)
    typed = ""
    try:
        tty.setcbreak(fd)
        render()
        while True:
            key = _read_key()
            selected, done, typed = _apply_choice_key(key, selected, len(options), typed)
            if done:
                return selected
            render()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)


def _choice_lines(title: str, options: list[tuple[str, str]], selected: int) -> list[str]:
    """Render the cursor picker as stable lines for redraw."""
    lines = [
        f"  {BOLD}{title}{RESET}",
        f"    {DIM}Use ↑/↓ and Enter, or press 1-{len(options)}.{RESET}",
    ]
    for i, (label, sub) in enumerate(options):
        marker = f"{GREEN}◉{RESET}" if i == selected else f"{DIM}○{RESET}"
        star = f" {GREEN}(recommended){RESET}" if i == selected else ""
        lines.append(f"    {marker} {BOLD}{label}{RESET}{star}")
        if sub:
            lines.append(f"       {DIM}{sub}{RESET}")
    return lines


def _read_key() -> str:
    """Read one terminal key, including common ANSI escape sequences."""
    ch = sys.stdin.read(1)
    if ch == "\x1b":
        return ch + sys.stdin.read(2)
    return ch


def _apply_choice_key(
    key: str, selected: int, count: int, typed: str = ""
) -> tuple[int, bool, str]:
    """Apply one keypress to a cursor menu. Returns (selected, done, typed).

    ``typed`` is the digits pressed so far. A number that one more digit could
    still extend (``1`` in a 13-item menu) moves the cursor and waits, so ``1``
    then ``3`` picks item 13 and ``1`` then Enter picks item 1.
    """
    if count <= 0:
        return 0, True, ""
    if key in ("\r", "\n"):
        return selected, True, ""
    if key in ("\x1b[A", "k", "K"):
        return (selected - 1) % count, False, ""
    if key in ("\x1b[B", "j", "J"):
        return (selected + 1) % count, False, ""
    if key.isdecimal():
        number = int(typed + key)
        if 1 <= number <= count:
            if number * 10 > count:
                return number - 1, True, ""
            return number - 1, False, typed + key
    return selected, False, ""


class Spinner:
    """A tiny live spinner for a long step. Context-manager; no-op when not a TTY."""

    _FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, msg: str) -> None:
        self.msg = msg
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def __enter__(self) -> "Spinner":
        if _ENABLE_COLOR:
            self._thread = threading.Thread(target=self._spin, daemon=True)
            self._thread.start()
        else:
            info(self.msg)
        return self

    def _spin(self) -> None:
        i = 0
        while not self._stop.is_set():
            frame = self._FRAMES[i % len(self._FRAMES)]
            sys.stdout.write(f"\r  {DIM}{frame}{RESET} {self.msg}")
            sys.stdout.flush()
            i += 1
            time.sleep(0.1)
        sys.stdout.write("\r" + " " * (len(self.msg) + 6) + "\r")
        sys.stdout.flush()

    def update(self, msg: str) -> None:
        self.msg = msg
        if not _ENABLE_COLOR:
            info(msg)

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)
