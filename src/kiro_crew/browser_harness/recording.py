"""Turn replay: a per-turn recording of the agent's browser, kept on this machine.

One recording per (session, turn): the frames the live stream produced, burned with
the password mask, and a log of every action taken on the page -- the agent's ops
and the person's takeover input. The card's replay player reads it back through
the owner-only routes in ``dashboard/handlers/browser_recordings.py``.

Hard rules, each pinned by a test:

* **Never an unmasked password frame.** A frame is kept only when the password-box
  query of THAT frame answered (``mask_ok``); its boxes are painted over in the
  JPEG itself before it touches disk. Unknown or failed mask, an undecodable
  JPEG, no imaging library: the frame is dropped. Fail closed.
* **Never a typed secret.** Event args go through an allowlist; a ``type`` whose
  args mark it ``secret`` (or that hit a password field) keeps its length only,
  and the person's keystrokes during a takeover are never kept as text at all.
* **Only on change, at most 5 fps.** A frame identical to the last kept one is
  skipped, and frames closer than :data:`MIN_FRAME_INTERVAL_MS` collapse onto the
  latest, which is kept at the next chance (the next frame past the interval, the
  next action, or the turn's end).
* **Bounded.** Per-session and global byte caps (``browser.recording.*``); past
  either, the oldest finished turns go first. A running turn that alone exceeds a
  cap stops keeping frames (``truncated``) but keeps its action log.
* **Owner-only on disk.** ``<data home>/browser-recordings/<sha256(session)[:32]>/
  <turn_id>/``: directories 0700, files 0600, opened with ``O_NOFOLLOW``.
  Removed with the session (:meth:`RecordingStore.delete_session`).

Layout of a turn directory: ``meta.json`` (rewritten atomically), ``frames.jsonl``
and ``events.jsonl`` (appended), ``frames/<i>.jpg``. Everything blocking is in
:class:`RecordingStore` and runs off the event loop; :class:`LiveRecorder` is the
hub's cheap synchronous front that rate-gates frames and serialises writes per
session. Nothing here knows any op by name beyond the generic op-to-kind table.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import re
import shutil
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger(__name__)

#: Directory under the data home every recording lives in.
RECORDINGS_DIRNAME = "browser-recordings"
#: Format version written into ``meta.json``.
META_VERSION = 1
#: Frames closer together than this collapse onto the latest: 5 fps.
MIN_FRAME_INTERVAL_MS = 200
#: Events kept per turn; past it the log stops growing (the turn is ``truncated``).
MAX_EVENTS_PER_TURN = 5000
#: What a turn's meta.json and directory are counted as, so a turn with no frames still costs.
TURN_META_BYTES = 1024
#: Points kept on one recorded drag path.
MAX_PATH_POINTS = 64
#: Longest string kept for any one event arg; a URL gets more room.
MAX_ARG_CHARS = 200
MAX_URL_CHARS = 2048
MAX_LABEL_CHARS = 120
#: A person's press-and-release closer than this (frame px) is a click, not a drag.
DRAG_MIN_PX = 4
#: JPEG quality a masked frame is re-encoded at.
MASKED_JPEG_QUALITY = 70
#: Pixels every password box is grown by before it is painted.
MASK_PAD_PX = 4
#: Colour the password boxes are painted with.
MASK_FILL = (40, 40, 40)

#: What a turn id and a frame file may look like on the wire and on disk.
TURN_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
FRAME_NAME_RE = re.compile(r"([0-9]{1,7})\.jpg")

_DIR_MODE = 0o700
_FILE_MODE = 0o600
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_BINARY = getattr(os, "O_BINARY", 0)

#: The op names the browser tool reports, folded onto the replay's seven kinds.
#: Unknown names are ``other``: a new op records without a change here.
OP_KINDS: Mapping[str, str] = {
    "click": "click",
    "type": "type",
    "press_key": "key",
    "drag": "drag",
    "scroll": "scroll",
    "navigate": "navigate",
    "back": "navigate",
}
EVENT_KINDS = frozenset({"click", "type", "key", "drag", "scroll", "navigate", "other"})

#: Arg names kept on an event, with how each is kept. Anything else is dropped:
#: an allowlist, so a new arg never lands on disk until it is listed here.
_NUMBER_ARGS = frozenset({"x", "y", "to_x", "to_y", "dx", "dy", "clicks", "time", "timeout"})
_TEXT_ARGS = frozenset({"ref", "to_ref", "button", "key", "action", "target_id", "textGone"})
_FLAG_ARGS = frozenset({"submit", "doubleClick", "load"})


def session_dirname(session_key: str) -> str:
    """The directory a session's recordings live in: opaque, fixed width, no path."""
    return hashlib.sha256(session_key.encode("utf-8")).hexdigest()[:32]


def valid_turn_id(turn_id: object) -> bool:
    return isinstance(turn_id, str) and TURN_ID_RE.fullmatch(turn_id) is not None


def now_ms() -> int:
    return int(time.time() * 1000)


#: Schemes whose URL keeps its host and path; any other keeps its scheme only
#: (``javascript:``, ``data:`` and ``blob:`` carry their payload in the URL).
_PLACE_SCHEMES = frozenset({"http", "https", "ws", "wss", "file", "about", "chrome"})


def clean_url(url: object) -> str:
    """A URL fit to keep: scheme, host, port and path. ``""`` for junk.

    The user and password, the query, the fragment and ``;`` path parameters are
    dropped: a sign-in redirect carries its code or token there (``?code=``,
    ``#access_token=``, ``;jsessionid=``). A scheme outside :data:`_PLACE_SCHEMES`
    keeps only ``scheme:``.
    """
    if not isinstance(url, str) or not url:
        return ""
    try:
        parts = urlsplit(url.strip())
        scheme = parts.scheme.lower()
        if scheme not in _PLACE_SCHEMES:
            return f"{scheme}:" if scheme.isalnum() else ""
        host = parts.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        netloc = f"{host}:{parts.port}" if parts.port else host
        path = "/".join(seg.split(";", 1)[0] for seg in parts.path.split("/"))
        kept = urlunsplit((scheme, netloc, path, "", ""))
    except ValueError:
        return ""
    return kept[:MAX_URL_CHARS]


def sanitize_args(op: str, args: Mapping[str, Any] | None, *, secret: bool = False) -> dict:
    """The op's args as they may be kept: allowlisted, bounded, secrets as a length.

    ``type`` keeps its text only when it is not a secret (*secret*, or the args'
    own ``secret`` flag); a secret keeps ``chars`` and ``secret: true``. With
    *secret*, a ``press_key`` of one character keeps no key either (a password
    spelled one key at a time). ``select_option`` values and ``wait_for`` text
    are kept bounded.
    """
    out: dict[str, Any] = {}
    if not isinstance(args, Mapping):
        return out
    for name, value in args.items():
        if not isinstance(name, str):
            continue
        if name in _NUMBER_ARGS:
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                out[name] = round(float(value), 2)
        elif name in _TEXT_ARGS:
            if name == "key" and secret and isinstance(value, str) and len(value) == 1:
                continue
            if isinstance(value, str):
                out[name] = value[:MAX_ARG_CHARS]
        elif name in _FLAG_ARGS:
            if isinstance(value, bool):
                out[name] = value
        elif name == "modifiers":
            if isinstance(value, int) and not isinstance(value, bool):
                out[name] = value
            elif isinstance(value, list):
                out[name] = [str(v)[:16] for v in value[:4]]
        elif name == "url":
            kept = clean_url(value)
            if kept:
                out[name] = kept
        elif name in ("values", "value") and op != "type":
            raw = value if isinstance(value, list) else [value]
            out["values"] = [str(v)[:MAX_ARG_CHARS] for v in raw[:16] if v is not None]
    text = args.get("text")
    if isinstance(text, str):
        if op == "type" and (secret or bool(args.get("secret"))):
            out["chars"] = len(text)
            out["secret"] = True
        elif op == "type":
            out["chars"] = len(text)
            out["text"] = text[:MAX_ARG_CHARS]
        else:
            out["text"] = text[:MAX_ARG_CHARS]
    return out


def burn_mask(jpeg: bytes, width: int, height: int, boxes: list[list[int]]) -> bytes | None:
    """The JPEG with every box painted over, re-encoded; ``None`` when that fails.

    No boxes: the frame is kept byte-for-byte. Any failure (no Pillow, a frame that
    does not decode, a size that disagrees with the header): ``None``, and the
    caller drops the frame.
    """
    if not boxes:
        return jpeg
    try:
        from PIL import Image, ImageDraw
    except Exception:  # noqa: BLE001 - no imaging library: a masked frame cannot be made
        return None
    try:
        with Image.open(io.BytesIO(jpeg)) as img:
            img.load()
            if img.size != (width, height):
                return None
            rgb = img.convert("RGB")
        draw = ImageDraw.Draw(rgb)
        for x, y, w, h in boxes:
            draw.rectangle([x, y, x + w - 1, y + h - 1], fill=MASK_FILL)
        out = io.BytesIO()
        rgb.save(out, format="JPEG", quality=MASKED_JPEG_QUALITY)
        return out.getvalue()
    except Exception:  # noqa: BLE001 - a frame we cannot mask is a frame we do not keep
        logger.debug("recording: mask burn failed", exc_info=True)
        return None


def pad_boxes(boxes: list[list[int]], width: int, height: int) -> list[list[int]]:
    """Each box grown by :data:`MASK_PAD_PX` on every side, clipped to the frame.

    The boxes are read just after the frame is captured, so a field that moved by a
    few pixels in between (a scroll, a layout shift) is still covered.
    """
    out: list[list[int]] = []
    for x, y, w, h in boxes:
        left, top = max(0, x - MASK_PAD_PX), max(0, y - MASK_PAD_PX)
        right, bottom = min(width, x + w + MASK_PAD_PX), min(height, y + h + MASK_PAD_PX)
        out.append([left, top, right - left, bottom - top])
    return out


def valid_boxes(boxes: object, width: int, height: int) -> list[list[int]] | None:
    """Boxes inside the frame as ints, or ``None`` when any box is malformed."""
    if not isinstance(boxes, list):
        return None
    out: list[list[int]] = []
    for box in boxes:
        if not (isinstance(box, list) and len(box) == 4):
            return None
        if not all(isinstance(v, int) and not isinstance(v, bool) for v in box):
            return None
        x, y, w, h = box
        if w <= 0 or h <= 0 or x < 0 or y < 0 or x + w > width or y + h > height:
            return None
        out.append([x, y, w, h])
    return out


@dataclass(frozen=True)
class RecordingConfig:
    enabled: bool = True
    max_session_bytes: int = 200 * 1024 * 1024
    max_total_bytes: int = 1024 * 1024 * 1024

    @classmethod
    def from_config(cls, cfg: Any) -> "RecordingConfig":
        rec = getattr(getattr(cfg, "browser", None), "recording", None)
        if rec is None:
            return cls()
        mb = 1024 * 1024
        try:
            return cls(
                enabled=bool(getattr(rec, "enabled", True)),
                max_session_bytes=max(1, int(getattr(rec, "max_session_mb", 200))) * mb,
                max_total_bytes=max(1, int(getattr(rec, "max_total_mb", 1024))) * mb,
            )
        except (TypeError, ValueError):
            return cls()


@dataclass
class TurnInfo:
    """One turn as the index knows it; ``meta.json`` is its persisted form."""

    session_key: str
    turn_id: str
    started_ts: int
    ended_ts: int | None = None
    width: int = 0
    height: int = 0
    start_url: str = ""
    frame_count: int = 0
    event_count: int = 0
    bytes: int = 0
    truncated: bool = False
    last_ts: int = 0

    def meta(self) -> dict[str, Any]:
        return {
            "version": META_VERSION,
            "session_key": self.session_key,
            "turn_id": self.turn_id,
            "started_ts": self.started_ts,
            "ended_ts": self.ended_ts,
            "width": self.width,
            "height": self.height,
            "start_url": self.start_url,
            "frame_count": self.frame_count,
            "event_count": self.event_count,
            "bytes": self.bytes,
            "truncated": self.truncated,
            "last_ts": self.last_ts,
        }

    @classmethod
    def from_meta(cls, data: Mapping[str, Any]) -> "TurnInfo | None":
        try:
            info = cls(
                session_key=str(data["session_key"]),
                turn_id=str(data["turn_id"]),
                started_ts=int(data["started_ts"]),
                ended_ts=None if data.get("ended_ts") is None else int(data["ended_ts"]),
                width=int(data.get("width") or 0),
                height=int(data.get("height") or 0),
                start_url=str(data.get("start_url") or ""),
                frame_count=int(data.get("frame_count") or 0),
                event_count=int(data.get("event_count") or 0),
                bytes=max(int(data.get("bytes") or 0), TURN_META_BYTES),
                truncated=bool(data.get("truncated")),
                last_ts=int(data.get("last_ts") or 0),
            )
        except (KeyError, TypeError, ValueError):
            return None
        return info if valid_turn_id(info.turn_id) else None


class RecordingStore:
    """The blocking half: the on-disk recordings, their index and the byte caps.

    Thread-safe (one lock); every method may be called from a worker thread. The
    root is resolved on first use from *root*, a callable, so the data home a test
    sets is the one used.
    """

    def __init__(self, root: Callable[[], Path], config: RecordingConfig | None = None) -> None:
        self._root_fn = root
        self.config = config or RecordingConfig()
        self._lock = threading.RLock()
        self._root: Path | None = None
        #: session dir -> turn id -> info. Loaded from disk on first use.
        self._index: dict[str, dict[str, TurnInfo]] | None = None
        #: (session dir, turn id) of the turns running in this process.
        self._live: set[tuple[str, str]] = set()

    # ── paths ──────────────────────────────────────────────────────────────

    def root(self) -> Path:
        """``<data home>/browser-recordings``, resolved per call (the home can move).

        A different home than last time (a test's, or an override set late) starts
        from an empty index: nothing read under one home is answered for another.
        """
        with self._lock:
            root = Path(self._root_fn()) / RECORDINGS_DIRNAME
            if root != self._root:
                self._root = root
                self._index = None
                self._live = set()
            return root

    def _ensure_dir(self, path: Path) -> None:
        path.mkdir(mode=_DIR_MODE, parents=True, exist_ok=True)
        if path.is_symlink():
            raise OSError(f"refusing a symlinked recording directory: {path}")
        try:
            os.chmod(path, _DIR_MODE)
        except OSError:
            logger.debug("recording: chmod %s failed", path, exc_info=True)

    def _turn_dir(self, sdir: str, turn_id: str) -> Path:
        return self.root() / sdir / turn_id

    @staticmethod
    def _write_new(path: Path, data: bytes) -> None:
        fd = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_BINARY, _FILE_MODE
        )
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)

    @staticmethod
    def _append(path: Path, line: dict[str, Any]) -> int:
        data = (json.dumps(line, separators=(",", ":"), ensure_ascii=False) + "\n").encode()
        fd = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | _O_NOFOLLOW | _O_BINARY, _FILE_MODE
        )
        with os.fdopen(fd, "ab") as fh:
            fh.write(data)
        return len(data)

    def _write_meta(self, sdir: str, info: TurnInfo) -> None:
        tdir = self._turn_dir(sdir, info.turn_id)
        tmp = tdir / f".meta.{os.getpid()}.{threading.get_ident()}.tmp"
        data = json.dumps(info.meta(), separators=(",", ":")).encode()
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        self._write_new(tmp, data)
        os.replace(tmp, tdir / "meta.json")

    @staticmethod
    def _read_bytes(path: Path) -> bytes | None:
        try:
            fd = os.open(path, os.O_RDONLY | _O_NOFOLLOW | _O_BINARY)
        except OSError:
            return None
        with os.fdopen(fd, "rb") as fh:
            return fh.read()

    # ── index ──────────────────────────────────────────────────────────────

    def _loaded(self) -> dict[str, dict[str, TurnInfo]]:
        root = self.root()  # first: a moved home drops the index it would return
        if self._index is not None:
            return self._index
        index: dict[str, dict[str, TurnInfo]] = {}
        try:
            sessions = [p for p in root.iterdir() if p.is_dir() and not p.is_symlink()]
        except OSError:
            sessions = []
        for sdir in sessions:
            try:
                tdirs = [p for p in sdir.iterdir() if not p.is_symlink()]
            except OSError:
                continue
            for tdir in tdirs:
                raw = self._read_bytes(tdir / "meta.json") if tdir.is_dir() else None
                if raw is None:
                    continue
                try:
                    info = TurnInfo.from_meta(json.loads(raw.decode("utf-8")))
                except ValueError:  # UnicodeDecodeError is a ValueError
                    info = None
                if info is None or info.turn_id != tdir.name:
                    continue
                if info.ended_ts is None:
                    # A turn that was running when the gateway went away.
                    info.ended_ts = info.last_ts or info.started_ts
                index.setdefault(sdir.name, {})[info.turn_id] = info
        self._index = index
        return index

    def _session_bytes(self, sdir: str) -> int:
        return sum(t.bytes for t in self._loaded().get(sdir, {}).values())

    def _total_bytes(self) -> int:
        return sum(t.bytes for turns in self._loaded().values() for t in turns.values())

    def _drop_turn(self, sdir: str, turn_id: str) -> None:
        turns = self._loaded().get(sdir, {})
        turns.pop(turn_id, None)
        self._live.discard((sdir, turn_id))
        shutil.rmtree(self._turn_dir(sdir, turn_id), ignore_errors=True)
        if not turns:
            self._loaded().pop(sdir, None)
            try:
                (self.root() / sdir).rmdir()
            except OSError:
                pass

    def _evict(self, sdir: str, extra: int = 0) -> None:
        """Oldest finished turns first, until *extra* more bytes fit both caps.

        The session's cap first, then the global one. Running turns are never
        evicted; when only they are left, the caller's write is refused instead.
        """
        cfg = self.config
        while self._session_bytes(sdir) + extra > cfg.max_session_bytes:
            victims = [
                t
                for t in self._loaded().get(sdir, {}).values()
                if (sdir, t.turn_id) not in self._live
            ]
            if not victims:
                break
            oldest = min(victims, key=lambda t: (t.started_ts, t.turn_id))
            self._drop_turn(sdir, oldest.turn_id)
        while self._total_bytes() + extra > cfg.max_total_bytes:
            anywhere: list[tuple[str, TurnInfo]] = [
                (s, t)
                for s, turns in self._loaded().items()
                for t in turns.values()
                if (s, t.turn_id) not in self._live
            ]
            if not anywhere:
                break
            where, oldest = min(anywhere, key=lambda st: (st[1].started_ts, st[1].turn_id))
            self._drop_turn(where, oldest.turn_id)

    def _over_cap(self, sdir: str, extra: int) -> bool:
        cfg = self.config
        return (
            self._session_bytes(sdir) + extra > cfg.max_session_bytes
            or self._total_bytes() + extra > cfg.max_total_bytes
        )

    # ── writes (worker thread) ─────────────────────────────────────────────

    def start_turn(self, session_key: str, turn_id: str, ts: int, url: str = "") -> bool:
        """Open (or reopen) a turn's recording. ``False`` when disabled or invalid."""
        if not self.config.enabled or not valid_turn_id(turn_id) or not session_key:
            return False
        sdir = session_dirname(session_key)
        with self._lock:
            turns = self._loaded().setdefault(sdir, {})
            info = turns.get(turn_id)
            self._ensure_dir(self.root())
            self._ensure_dir(self.root() / sdir)
            self._ensure_dir(self._turn_dir(sdir, turn_id))
            self._ensure_dir(self._turn_dir(sdir, turn_id) / "frames")
            if info is None:
                info = TurnInfo(session_key=session_key, turn_id=turn_id, started_ts=ts)
                info.bytes = TURN_META_BYTES  # its meta.json counts against the caps
                turns[turn_id] = info
            info.ended_ts = None
            if url and not info.start_url:
                info.start_url = clean_url(url)
            self._live.add((sdir, turn_id))
            self._write_meta(sdir, info)
            self._evict(sdir)
            return True

    def _live_info(self, session_key: str, turn_id: str) -> tuple[str, TurnInfo] | None:
        sdir = session_dirname(session_key)
        if (sdir, turn_id) not in self._live:
            return None
        info = self._loaded().get(sdir, {}).get(turn_id)
        return (sdir, info) if info is not None else None

    def end_turn(self, session_key: str, turn_id: str, ts: int) -> None:
        with self._lock:
            found = self._live_info(session_key, turn_id)
            if found is None:
                return
            sdir, info = found
            info.ended_ts = max(ts, info.last_ts, info.started_ts)
            self._live.discard((sdir, turn_id))
            self._write_meta(sdir, info)
            self._evict(sdir)

    def note_url(self, session_key: str, turn_id: str, url: str) -> None:
        with self._lock:
            found = self._live_info(session_key, turn_id)
            if found is None or found[1].start_url:
                return
            kept = clean_url(url)
            if kept and kept != "about:blank":
                found[1].start_url = kept
                self._write_meta(found[0], found[1])

    def add_frame(
        self,
        session_key: str,
        turn_id: str,
        jpeg: bytes,
        width: int,
        height: int,
        boxes: list[list[int]],
        mask_ok: bool,
        ts: int,
    ) -> bool:
        """Mask and keep one frame. ``False`` (nothing written) whenever it cannot be safe."""
        if not mask_ok or width <= 0 or height <= 0:
            return False
        rects = valid_boxes(boxes, width, height)
        if rects is None:
            return False
        masked = burn_mask(jpeg, width, height, pad_boxes(rects, width, height))
        if masked is None:
            return False
        with self._lock:
            found = self._live_info(session_key, turn_id)
            if found is None:
                return False
            sdir, info = found
            if info.truncated:
                return False
            line = {"i": info.frame_count, "ts": ts, "w": width, "h": height}
            self._evict(sdir, len(masked))
            if self._over_cap(sdir, len(masked)):
                info.truncated = True
                self._write_meta(sdir, info)
                return False
            tdir = self._turn_dir(sdir, turn_id)
            try:
                self._write_new(tdir / "frames" / f"{info.frame_count}.jpg", masked)
                info.bytes += len(masked) + self._append(tdir / "frames.jsonl", line)
            except OSError:
                logger.debug("recording: frame write failed", exc_info=True)
                return False
            info.frame_count += 1
            info.width, info.height = width, height
            info.last_ts = max(info.last_ts, ts)
            self._write_meta(sdir, info)
            return True

    def add_event(self, session_key: str, turn_id: str, event: dict[str, Any]) -> bool:
        with self._lock:
            found = self._live_info(session_key, turn_id)
            if found is None:
                return False
            sdir, info = found
            if info.event_count >= MAX_EVENTS_PER_TURN:
                info.truncated = True
                self._write_meta(sdir, info)
                return False
            line = {"id": info.event_count, **event}
            if not info.start_url and isinstance(event.get("url"), str):
                info.start_url = event["url"]
            try:
                info.bytes += self._append(self._turn_dir(sdir, turn_id) / "events.jsonl", line)
            except OSError:
                logger.debug("recording: event write failed", exc_info=True)
                return False
            info.event_count += 1
            info.last_ts = max(info.last_ts, int(event.get("ts") or 0))
            self._write_meta(sdir, info)
            return True

    def delete_session(self, session_key: str) -> bool:
        """Remove every recording of *session_key*. Returns whether any existed."""
        if not session_key:
            return False
        sdir = session_dirname(session_key)
        with self._lock:
            index = self._loaded()
            existed = sdir in index
            index.pop(sdir, None)
            self._live = {(s, t) for s, t in self._live if s != sdir}
            path = self.root() / sdir
            if path.is_symlink():
                path.unlink()
                return True
            if path.exists():
                existed = True
                shutil.rmtree(path, ignore_errors=True)
            return existed

    # ── reads (route thread) ───────────────────────────────────────────────

    def list_turns(self, session_key: str) -> list[dict[str, Any]]:
        sdir = session_dirname(session_key)
        with self._lock:
            turns = list(self._loaded().get(sdir, {}).values())
            live = {t for s, t in self._live if s == sdir}
        turns.sort(key=lambda t: (t.started_ts, t.turn_id))
        return [
            {
                "turn_id": t.turn_id,
                "started_ts": t.started_ts,
                "ended_ts": None if t.turn_id in live else t.ended_ts,
                "live": t.turn_id in live,
                "frame_count": t.frame_count,
                "event_count": t.event_count,
                "bytes": t.bytes,
                "start_url": t.start_url,
                "truncated": t.truncated,
            }
            for t in turns
        ]

    def _jsonl(self, path: Path, limit: int) -> list[dict[str, Any]]:
        raw = self._read_bytes(path)
        if raw is None:
            return []
        out: list[dict[str, Any]] = []
        for line in raw.decode("utf-8", errors="replace").splitlines()[:limit]:
            try:
                item = json.loads(line)
            except ValueError:
                continue  # a torn last line from a write in flight
            if isinstance(item, dict):
                out.append(item)
        return out

    def turn_detail(self, session_key: str, turn_id: str) -> dict[str, Any] | None:
        if not valid_turn_id(turn_id):
            return None
        sdir = session_dirname(session_key)
        with self._lock:
            info = self._loaded().get(sdir, {}).get(turn_id)
            live = (sdir, turn_id) in self._live
            if info is None:
                return None
            snapshot = TurnInfo(**{**info.__dict__})
        tdir = self._turn_dir(sdir, turn_id)
        frames = [
            {"i": int(f["i"]), "ts": int(f["ts"]), "w": int(f["w"]), "h": int(f["h"])}
            for f in self._jsonl(tdir / "frames.jsonl", snapshot.frame_count)
            if {"i", "ts", "w", "h"} <= set(f)
        ]
        events = self._jsonl(tdir / "events.jsonl", snapshot.event_count)
        return {
            "turn_id": turn_id,
            "started_ts": snapshot.started_ts,
            "ended_ts": None if live else snapshot.ended_ts,
            "live": live,
            "width": snapshot.width,
            "height": snapshot.height,
            "start_url": snapshot.start_url,
            "truncated": snapshot.truncated,
            "frames": frames,
            "events": events,
        }

    def read_frame(self, session_key: str, turn_id: str, index: int) -> bytes | None:
        if not valid_turn_id(turn_id) or index < 0:
            return None
        sdir = session_dirname(session_key)
        with self._lock:
            info = self._loaded().get(sdir, {}).get(turn_id)
            if info is None or index >= info.frame_count:
                return None
        root = self.root().resolve()
        path = self._turn_dir(sdir, turn_id) / "frames" / f"{index}.jpg"
        try:
            if root not in path.resolve().parents:
                return None
        except OSError:
            return None
        return self._read_bytes(path)


# ── the hub's front ────────────────────────────────────────────────────────


@dataclass
class _PendingFrame:
    turn_id: str
    jpeg: bytes
    width: int
    height: int
    boxes: list[list[int]]
    ts: int


@dataclass
class _UserGesture:
    """The person's pointer press in progress, in frame pixels."""

    button: str
    path: list[dict[str, int]] = field(default_factory=list)
    ts: int = 0


@dataclass
class _SessionRec:
    turn_id: str = ""
    last_kept_ms: int = -(10**12)
    last_digest: bytes = b""
    pending: _PendingFrame | None = None
    jobs: deque[Callable[[], Any]] = field(default_factory=deque)
    worker: asyncio.Task[Any] | None = None
    gesture: _UserGesture | None = None
    typed_chars: int = 0
    typed_ts: int = 0
    typed_url: str = ""
    started_ms: int = 0
    #: Size of the latest frame the stream offered: the space a point drawn now is in.
    frame_w: int = 0
    frame_h: int = 0


class LiveRecorder:
    """The hub's synchronous front: rate gate, change gate, per-session write queue.

    Every public method is cheap and never raises; the work lands on a worker task
    per session that runs the store's blocking calls in a thread, one at a time,
    so frames and events reach disk in the order they happened.
    """

    def __init__(self, store: RecordingStore, *, clock_ms: Callable[[], int] = now_ms) -> None:
        self.store = store
        self._clock_ms = clock_ms
        #: Per-slot writer state (not the session registry: no crumbs live here).
        self._recs: dict[str, _SessionRec] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.store.config.enabled)

    def configure(self, cfg: Any) -> None:
        self.store.config = RecordingConfig.from_config(cfg)

    def _rec(self, session_key: str) -> _SessionRec:
        rec = self._recs.get(session_key)
        if rec is None:
            rec = _SessionRec()
            self._recs[session_key] = rec
        return rec

    def _submit(self, session_key: str, job: Callable[[], Any]) -> None:
        rec = self._rec(session_key)
        rec.jobs.append(job)
        if rec.worker is not None and not rec.worker.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No loop (a sync caller in a test): run in place.
            while rec.jobs:
                self._run(rec.jobs.popleft())
            return
        rec.worker = loop.create_task(self._drain(rec))

    @staticmethod
    def _run(job: Callable[[], Any]) -> None:
        try:
            job()
        except Exception:  # noqa: BLE001 - a recording failure never reaches the browser
            logger.debug("recording job failed", exc_info=True)

    async def _drain(self, rec: _SessionRec) -> None:
        while rec.jobs:
            job = rec.jobs.popleft()
            try:
                await asyncio.to_thread(job)
            except Exception:  # noqa: BLE001 - as above
                logger.debug("recording job failed", exc_info=True)

    async def flush(self, session_key: str | None = None) -> None:
        """Wait for the queued writes (tests, and the session delete)."""
        keys = [session_key] if session_key is not None else list(self._recs)
        for key in keys:
            rec = self._recs.get(key)
            if rec is None:
                continue
            while rec.worker is not None and not rec.worker.done():
                await asyncio.wait({rec.worker})
            if rec.jobs:
                await self._drain(rec)

    # ── turn ───────────────────────────────────────────────────────────────

    def turn_started(self, session_key: str, turn_id: str, url: str = "") -> None:
        if not self.enabled:
            return
        rec = self._rec(session_key)
        if rec.turn_id and rec.turn_id != turn_id:
            self._close(session_key, rec)
        rec.turn_id = turn_id
        rec.last_kept_ms = -(10**12)
        rec.last_digest = b""
        ts = self._clock_ms()
        rec.started_ms = ts
        # A size from the last turn is not this one's: a point before the first
        # frame falls back to the shown frame's own size.
        rec.frame_w = rec.frame_h = 0
        self._submit(session_key, lambda: self.store.start_turn(session_key, turn_id, ts, url))

    def turn_ended(self, session_key: str) -> None:
        rec = self._recs.get(session_key)
        if rec is None or not rec.turn_id:
            return
        self._close(session_key, rec)

    def _close(self, session_key: str, rec: _SessionRec) -> None:
        self._flush_pending(session_key, rec)
        self._flush_typing(session_key, rec)
        rec.gesture = None
        turn_id, rec.turn_id = rec.turn_id, ""
        ts = self._clock_ms()
        self._submit(session_key, lambda: self.store.end_turn(session_key, turn_id, ts))

    def note_url(self, session_key: str, url: str) -> None:
        rec = self._recs.get(session_key)
        if rec is None or not rec.turn_id or not url:
            return
        turn_id = rec.turn_id
        self._submit(session_key, lambda: self.store.note_url(session_key, turn_id, url))

    # ── frames ─────────────────────────────────────────────────────────────

    def turn_open(self, session_key: str) -> bool:
        """A turn of *session_key* is being recorded (so a hidden card keeps casting)."""
        rec = self._recs.get(session_key)
        return rec is not None and bool(rec.turn_id) and self.enabled

    def wants_frame(self, session_key: str) -> bool:
        """Whether a frame offered now would be kept (a turn is open, the 5 fps gate is).

        The stream asks before it spends a fresh password-box query on a frame.
        """
        rec = self._recs.get(session_key)
        if rec is None or not rec.turn_id or not self.enabled:
            return False
        return self._clock_ms() - rec.last_kept_ms >= MIN_FRAME_INTERVAL_MS

    def note_size(self, session_key: str, width: int, height: int) -> None:
        """The size of the frame streaming now: the space a point placed now is in."""
        rec = self._recs.get(session_key)
        if rec is not None and rec.turn_id and width > 0 and height > 0:
            rec.frame_w, rec.frame_h = width, height

    def frame(
        self,
        session_key: str,
        jpeg: bytes,
        width: int,
        height: int,
        boxes: list[list[int]],
        mask_ok: bool,
        *,
        age_s: float = 0.0,
    ) -> None:
        """One stream frame. Dropped unless the mask is known; kept on change, <= 5 fps.

        *age_s* backdates a frame the stream held and offers late (``flush_held``);
        it never lands before the last kept frame.
        """
        rec = self._recs.get(session_key)
        if rec is None or not rec.turn_id or not self.enabled:
            return
        if not mask_ok:
            # Fail closed, and drop a held frame too: what follows an unknown mask
            # must not be represented by a picture from before it.
            rec.pending = None
            return
        digest = hashlib.sha1(jpeg + repr(boxes).encode()).digest()  # noqa: S324 - change detection
        if digest == rec.last_digest:
            return
        ts = self._clock_ms()
        if age_s > 0:
            # Never before the last kept frame, and never before this turn began: a
            # frame held across a turn boundary belongs to no turn and is dropped.
            if ts - int(age_s * 1000) < rec.started_ms:
                return
            ts = max(rec.last_kept_ms + MIN_FRAME_INTERVAL_MS, ts - int(age_s * 1000))
        frame = _PendingFrame(rec.turn_id, jpeg, width, height, list(boxes), ts)
        if ts - rec.last_kept_ms < MIN_FRAME_INTERVAL_MS:
            rec.pending = frame
            return
        rec.pending = None
        self._keep(session_key, rec, frame, digest)

    def _keep(
        self, session_key: str, rec: _SessionRec, frame: _PendingFrame, digest: bytes | None = None
    ) -> None:
        rec.last_kept_ms = frame.ts
        rec.last_digest = (
            digest
            or hashlib.sha1(  # noqa: S324 - change detection
                frame.jpeg + repr(frame.boxes).encode()
            ).digest()
        )
        self._submit(
            session_key,
            lambda: self.store.add_frame(
                session_key,
                frame.turn_id,
                frame.jpeg,
                frame.width,
                frame.height,
                frame.boxes,
                True,
                frame.ts,
            ),
        )

    def _flush_pending(self, session_key: str, rec: _SessionRec) -> None:
        frame, rec.pending = rec.pending, None
        if frame is not None and frame.turn_id == rec.turn_id:
            self._keep(session_key, rec, frame)

    # ── events ─────────────────────────────────────────────────────────────

    def event(self, session_key: str, event: dict[str, Any]) -> None:
        """One action, already shaped (:func:`agent_event` / the user helpers)."""
        rec = self._recs.get(session_key)
        if rec is None or not rec.turn_id or not self.enabled:
            return
        self._flush_pending(session_key, rec)
        if event.get("actor") == "agent":
            self._flush_typing(session_key, rec)
        if (event.get("point") or event.get("path")) and rec.frame_w > 0:
            # A point is in the pixels of the frame size streaming now; the stream's
            # size can change before the frame the replay shows (a settle frame).
            event = {**event, "fw": rec.frame_w, "fh": rec.frame_h}
        turn_id = rec.turn_id
        self._submit(session_key, lambda: self.store.add_event(session_key, turn_id, event))

    def user_input(self, session_key: str, event: Any, url: str = "") -> None:
        """The person's takeover input (a parsed ``takeover.InputEvent``), as actions.

        A press-move-release becomes one ``click`` or ``drag``; a wheel is a
        ``scroll``; keys that type text are counted, never kept, and become one
        ``type`` event of ``chars`` when the typing stops (another action, the
        turn's end). A named key with no text (Enter, Tab) is a ``key`` event.
        """
        rec = self._recs.get(session_key)
        if rec is None or not rec.turn_id or not self.enabled:
            return
        ts = self._clock_ms()
        kind = getattr(event, "type", "")
        if kind == "key":
            if getattr(event, "action", "press") == "up":
                return
            text = getattr(event, "text", "") or ""
            key = getattr(event, "key", "") or ""
            if text or len(key) == 1:
                if not rec.typed_chars:
                    rec.typed_ts, rec.typed_url = ts, url
                rec.typed_chars += max(1, len(text))
                return
            self._flush_typing(session_key, rec)
            self.event(session_key, user_event("key", ts, url, args={"key": key[:40]}))
            return
        x, y = int(getattr(event, "x", 0)), int(getattr(event, "y", 0))
        point = {"x": x, "y": y}
        if kind == "wheel":
            self._flush_typing(session_key, rec)
            args = {"dx": getattr(event, "dx", 0), "dy": getattr(event, "dy", 0)}
            self.event(session_key, user_event("scroll", ts, url, point=point, args=args))
            return
        action = getattr(event, "action", "")
        button = getattr(event, "button", "none")
        if action == "click":
            self._flush_typing(session_key, rec)
            self.event(session_key, user_event("click", ts, url, point=point))
        elif action == "down":
            self._flush_typing(session_key, rec)
            rec.gesture = _UserGesture(button=button, path=[point], ts=ts)
        elif action == "move" and rec.gesture is not None and button != "none":
            if len(rec.gesture.path) < MAX_PATH_POINTS:
                rec.gesture.path.append(point)
            else:
                rec.gesture.path[-1] = point
        elif action == "up" and rec.gesture is not None:
            gesture, rec.gesture = rec.gesture, None
            gesture.path.append(point)
            start = gesture.path[0]
            if abs(x - start["x"]) + abs(y - start["y"]) < DRAG_MIN_PX:
                self.event(session_key, user_event("click", gesture.ts, url, point=start))
            else:
                self.event(
                    session_key,
                    user_event("drag", gesture.ts, url, point=start, path=gesture.path),
                )

    def _flush_typing(self, session_key: str, rec: _SessionRec) -> None:
        chars, rec.typed_chars = rec.typed_chars, 0
        if not chars or not rec.turn_id:
            return
        event = user_event(
            "type", rec.typed_ts, rec.typed_url, args={"chars": chars, "secret": True}
        )
        turn_id = rec.turn_id
        self._submit(session_key, lambda: self.store.add_event(session_key, turn_id, event))

    # ── deletion ───────────────────────────────────────────────────────────

    async def delete_session(self, session_key: str) -> bool:
        """Forget the session and remove its recordings, after its queued writes."""
        rec = self._recs.pop(session_key, None)
        if rec is not None:
            rec.turn_id = ""
            rec.pending = None
            rec.jobs.clear()
            if rec.worker is not None and not rec.worker.done():
                await asyncio.wait({rec.worker})
        return await asyncio.to_thread(self.store.delete_session, session_key)


#: Where a URL starts inside free text: a scheme and a colon with no space after it.
_URL_IN_TEXT_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]{1,15}:(?=\S)")


def _label(text: str) -> str:
    """A label fit to keep: from the first URL on, only what :func:`clean_url` keeps.

    A step label spells the URL it opened ("Open <url>"), cut at the step cap, so
    the URL may be partial and may run on past a space. Everything from the first
    ``scheme:`` to the end of the label is replaced by the cleaned URL, and a ``?``
    or ``#`` anywhere left cuts the label there.
    """
    raw = str(text or "")
    match = _URL_IN_TEXT_RE.search(raw)
    if match is not None:
        raw = raw[: match.start()] + clean_url(raw[match.start() :]).strip()
    for mark in ("?", "#"):
        raw = raw.split(mark, 1)[0]
    return raw.strip()[:MAX_LABEL_CHARS]


def user_event(
    op: str,
    ts: int,
    url: str,
    *,
    label: str = "",
    point: dict[str, int] | None = None,
    path: list[dict[str, int]] | None = None,
    args: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "ts": ts,
        "op": op if op in EVENT_KINDS else "other",
        "label": _label(label),
        "args": dict(args or {}),
        "point": point,
        "path": path,
        "url": clean_url(url) or None,
        "status": "ok",
        "actor": "user",
    }


def _frame_point(x: object, y: object, scale: float) -> dict[str, int] | None:
    if isinstance(x, bool) or isinstance(y, bool):
        return None
    if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
        return None
    return {"x": int(round(float(x) * scale)), "y": int(round(float(y) * scale))}


def agent_event(
    op: str,
    args: Mapping[str, Any] | None,
    result: Mapping[str, Any] | None,
    *,
    ts: int,
    label: str,
    url: str,
    scale: float,
) -> dict[str, Any]:
    """One agent op as a replay event. Generic: reads the op's own result shape.

    Points come from the result (``x``/``y``, or ``from``/``to`` for a drag), in
    viewport CSS pixels, and are multiplied by *scale* (frame px per CSS px) onto
    the recorded frames. A refusal or failure is ``status: error``.
    """
    result = result if isinstance(result, Mapping) else {}
    ok = bool(result.get("ok"))
    # Text that did not land is kept as a length: a refusal or an interruption may
    # have stopped it short of the password check that would have refused it.
    secret = result.get("code") == "password_field" or not ok
    kind = OP_KINDS.get(op, "other")
    scale = scale if scale > 0 else 1.0
    point: dict[str, int] | None = None
    path: list[dict[str, int]] | None = None
    if ok:
        frm, to = result.get("from"), result.get("to")
        if isinstance(frm, list) and isinstance(to, list) and len(frm) == 2 and len(to) == 2:
            a, b = _frame_point(frm[0], frm[1], scale), _frame_point(to[0], to[1], scale)
            if a is not None and b is not None:
                point, path = a, [a, b]
        else:
            point = _frame_point(result.get("x"), result.get("y"), scale)
    where = url
    if kind == "navigate" and isinstance(args, Mapping) and isinstance(args.get("url"), str):
        where = args["url"]
    return {
        "ts": int(ts),
        "op": kind,
        "label": _label(label or op.replace("_", " ").capitalize()),
        "args": {"name": op[:32], **sanitize_args(op, args, secret=secret)},
        "point": point,
        "path": path,
        "url": clean_url(where) or None,
        "status": "ok" if ok else "error",
        "actor": "agent",
    }


_STORE: RecordingStore | None = None
_RECORDER: LiveRecorder | None = None


def get_recorder() -> LiveRecorder:
    """The gateway's one recorder, rooted in the data home on first use."""
    global _STORE, _RECORDER
    if _RECORDER is None:
        from kiro_crew.config.paths import data_home

        _STORE = RecordingStore(data_home)
        _RECORDER = LiveRecorder(_STORE)
    return _RECORDER


def reset_recorder() -> None:
    """Drop the singleton (tests)."""
    global _STORE, _RECORDER
    _STORE = None
    _RECORDER = None


__all__ = [
    "EVENT_KINDS",
    "LiveRecorder",
    "MIN_FRAME_INTERVAL_MS",
    "OP_KINDS",
    "RECORDINGS_DIRNAME",
    "RecordingConfig",
    "RecordingStore",
    "TURN_ID_RE",
    "agent_event",
    "burn_mask",
    "clean_url",
    "get_recorder",
    "reset_recorder",
    "sanitize_args",
    "session_dirname",
    "user_event",
    "valid_turn_id",
]
