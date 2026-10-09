from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Final

DIFF_FORMAT_VERSION: Final[str] = "1.0"


class DiffError(Exception):
    pass


@dataclass(frozen=True)
class _Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: tuple[str, ...]


@dataclass(frozen=True)
class ParsedDiff:
    path: str
    hunks: tuple[_Hunk, ...]


def _text_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalized_path(raw: str) -> str:
    if raw.startswith("/"):
        raise DiffError("absolute path is rejected")
    parts = raw.split("/")
    if ".." in parts:
        raise DiffError("path traversal is rejected")
    if "" in parts:
        raise DiffError("malformed path")
    return raw


def _parse_header_path(line: str, prefix: str, marker: str) -> str:
    rest = line[len(prefix) :]
    if not rest.startswith(marker):
        raise DiffError("header must use the a/ and b/ markers")
    return _normalized_path(rest[len(marker) :])


def _pair(token: str) -> tuple[int, int]:
    token = token[1:]
    if "," in token:
        start_s, count_s = token.split(",", 1)
    else:
        start_s, count_s = token, "1"
    try:
        return int(start_s), int(count_s)
    except ValueError as exc:
        raise DiffError("malformed hunk range") from exc


def _parse_hunk_header(line: str) -> tuple[int, int, int, int]:
    if not line.startswith("@@ -") or "@@" not in line[3:]:
        raise DiffError("malformed hunk header")
    ranges, _, _ = line[len("@@ ") :].partition(" @@")
    old_part, _, new_part = ranges.partition(" ")
    if not old_part.startswith("-") or not new_part.startswith("+"):
        raise DiffError("malformed hunk header")
    old_start, old_count = _pair(old_part)
    new_start, new_count = _pair(new_part)
    return old_start, old_count, new_start, new_count


def parse_diff(diff_text: str) -> ParsedDiff:
    lines = diff_text.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    if len(lines) < 2:
        raise DiffError("diff too short")
    for line in lines:
        for ch in line:
            if ord(ch) < 32 and ch != "\t":
                raise DiffError("control character in diff is rejected")
    header_count = sum(1 for line in lines if line.startswith("--- "))
    if header_count > 1:
        raise DiffError("multi-file diff is rejected")
    for forbidden in ("new file mode", "deleted file mode", "old mode", "new mode"):
        if any(line.startswith(forbidden) for line in lines):
            raise DiffError("file creation, deletion, or mode change is rejected")
    if any(line.startswith("rename from") or line.startswith("rename to") for line in lines):
        raise DiffError("rename is rejected")
    if any(line.startswith("Binary files") or line == "GIT binary patch" for line in lines):
        raise DiffError("binary diff is rejected")
    if not lines[0].startswith("--- "):
        raise DiffError("missing --- header")
    if not lines[1].startswith("+++ "):
        raise DiffError("missing +++ header")
    old_path = _parse_header_path(lines[0], "--- ", "a/")
    new_path = _parse_header_path(lines[1], "+++ ", "b/")
    if old_path != new_path:
        raise DiffError("--- and +++ paths must match")
    hunks: list[_Hunk] = []
    index = 2
    while index < len(lines):
        line = lines[index]
        if not line.startswith("@@ "):
            raise DiffError("expected a hunk header")
        old_start, old_count, new_start, new_count = _parse_hunk_header(line)
        index += 1
        body: list[str] = []
        while index < len(lines) and not lines[index].startswith("@@ "):
            candidate = lines[index]
            if candidate.startswith("--- ") or candidate.startswith("+++ "):
                raise DiffError("multi-file diff is rejected")
            if candidate[:1] not in (" ", "+", "-"):
                raise DiffError("malformed hunk line")
            body.append(candidate)
            index += 1
        _validate_hunk_counts(old_count, new_count, body)
        hunks.append(
            _Hunk(
                old_start=old_start,
                old_count=old_count,
                new_start=new_start,
                new_count=new_count,
                lines=tuple(body),
            )
        )
    if not hunks:
        raise DiffError("diff has no hunks")
    _reject_overlaps(hunks)
    return ParsedDiff(path=old_path, hunks=tuple(hunks))


def _validate_hunk_counts(old_count: int, new_count: int, body: list[str]) -> None:
    old_lines = sum(1 for line in body if line[:1] in (" ", "-"))
    new_lines = sum(1 for line in body if line[:1] in (" ", "+"))
    if old_lines != old_count or new_lines != new_count:
        raise DiffError("hunk range count does not match hunk body")


def _reject_overlaps(hunks: list[_Hunk]) -> None:
    seen_starts: set[int] = set()
    last_end = 0
    for hunk in sorted(hunks, key=lambda h: (h.old_start, h.new_start)):
        if hunk.old_start in seen_starts:
            raise DiffError("duplicate hunk at the same old_start is rejected")
        seen_starts.add(hunk.old_start)
        if hunk.old_count > 0 and hunk.old_start <= last_end:
            raise DiffError("overlapping hunks are rejected")
        if hunk.old_count > 0:
            last_end = hunk.old_start + hunk.old_count - 1


def _apply_hunks(base_lines: list[str], hunks: tuple[_Hunk, ...], reverse: bool) -> list[str]:
    result: list[str] = []
    cursor = 0
    for hunk in sorted(hunks, key=lambda h: h.old_start):
        start = (hunk.new_start if reverse else hunk.old_start) - 1
        if start < cursor:
            raise DiffError("hunk application out of order")
        result.extend(base_lines[cursor:start])
        cursor = start
        for entry in hunk.lines:
            tag, text = entry[:1], entry[1:]
            take_tag, emit_tag = ("+", "-") if reverse else ("-", "+")
            if tag == " ":
                if cursor >= len(base_lines) or base_lines[cursor] != text:
                    raise DiffError("context line does not match base")
                result.append(text)
                cursor += 1
            elif tag == take_tag:
                if cursor >= len(base_lines) or base_lines[cursor] != text:
                    raise DiffError("removed line does not match base")
                cursor += 1
            elif tag == emit_tag:
                result.append(text)
            else:
                raise DiffError("unexpected hunk line tag")
    result.extend(base_lines[cursor:])
    return result


def _split(text: str) -> tuple[list[str], bool]:
    trailing_newline = text.endswith("\n")
    body = text[:-1] if trailing_newline else text
    return (body.split("\n") if body != "" else []), trailing_newline


def _join(lines: list[str], trailing_newline: bool) -> str:
    joined = "\n".join(lines)
    if trailing_newline:
        joined += "\n"
    return joined


def _normalize_expected(expected_path: str) -> str:
    if expected_path.startswith("/"):
        raise DiffError("expected target path must be workspace-relative")
    parts = [part for part in expected_path.split("/") if part not in ("", ".")]
    if ".." in parts:
        raise DiffError("expected target path must not contain traversal")
    if not parts:
        raise DiffError("expected target path is empty")
    return "/".join(parts)


def apply_diff(
    base_text: str,
    diff_text: str,
    base_digest: str,
    expected_path: str,
    max_output_bytes: int | None = None,
) -> str:
    normalized = _normalize_expected(expected_path)
    if _text_digest(base_text) != base_digest:
        raise DiffError("base digest does not match")
    parsed = parse_diff(diff_text)
    if parsed.path != normalized:
        raise DiffError("diff target path does not match the expected target")
    base_lines, trailing = _split(base_text)
    applied = _apply_hunks(base_lines, parsed.hunks, reverse=False)
    result = _join(applied, trailing)
    if max_output_bytes is not None and len(result.encode("utf-8")) > max_output_bytes:
        raise DiffError("applied result exceeds output bound")
    return result


def reverse_apply_diff(modified_text: str, diff_text: str, expected_path: str) -> str:
    normalized = _normalize_expected(expected_path)
    parsed = parse_diff(diff_text)
    if parsed.path != normalized:
        raise DiffError("diff target path does not match the expected target")
    modified_lines, trailing = _split(modified_text)
    restored = _apply_hunks(modified_lines, parsed.hunks, reverse=True)
    return _join(restored, trailing)
