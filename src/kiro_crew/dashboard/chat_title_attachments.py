from __future__ import annotations

from typing import Any

# Only a small amount of user text can influence a 200-character title prompt.
# Allow enough bounded source for every dashboard attachment to precede it, then
# cap the retained text separately after generated references are removed.
_TITLE_TEXT_LIMIT = 16_384
_TITLE_MAX_ATTACHMENT_FILES = 20
_TITLE_MAX_ATTACHMENT_PATH_LENGTH = 4_096
# Total budget for ALL substituted attachment labels in one message, and the cap
# for any single label. Bounded so a message carrying 20 deep paths cannot push
# the real user text out of the prompt window.
_TITLE_MAX_ATTACHMENT_LABEL_BUDGET = 80
_TITLE_MAX_ATTACHMENT_LABEL_LENGTH = _TITLE_MAX_ATTACHMENT_LABEL_BUDGET // 2
_TITLE_SOURCE_SCAN_LIMIT = _TITLE_TEXT_LIMIT + _TITLE_MAX_ATTACHMENT_FILES * (
    _TITLE_MAX_ATTACHMENT_PATH_LENGTH + 32
)


def _strip_markdown_images(content: str, *, drop_trailing_partial: bool = False) -> str:
    """Remove dashboard-generated image blocks in one forward pass.

    Dashboard image references use the fixed ``![image](path)`` form on their
    own lines. Requiring that shape preserves escaped and code-quoted Markdown
    written by the user while balanced-parenthesis tracking handles filenames
    such as ``screenshot(1).jpg`` without regex backtracking.
    """
    prefix = "![image]("
    chunks: list[str] = []
    cursor = 0
    while True:
        image_start = content.find(prefix, cursor)
        if image_start < 0:
            chunks.append(content[cursor:])
            break

        if image_start > 0 and content[image_start - 1] != "\n":
            chunks.append(content[cursor : image_start + 1])
            cursor = image_start + 1
            continue

        index = image_start + len(prefix)
        depth = 1
        while index < len(content) and depth and content[index] not in "\r\n":
            char = content[index]
            if char == "\\" and index + 1 < len(content):
                index += 2
                continue
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            index += 1

        if depth or (index < len(content) and content[index] not in "\r\n"):
            if drop_trailing_partial and index == len(content):
                chunks.append(content[cursor:image_start])
                break
            chunks.append(content[cursor : image_start + 1])
            cursor = image_start + 1
            continue

        chunks.append(content[cursor:image_start])
        chunks.append(" ")
        cursor = index

    return "".join(chunks)


def _attachment_labels(paths: tuple[str, ...]) -> dict[str, str]:
    """Map each attachment path to its title label: the trailing path segment.

    Titles keep the name rather than the full path: the path is noise and can
    leak a directory layout, but the name is usually the whole topic. A label is
    widened leftwards while it collides, because three files all named
    ``report.pdf`` would otherwise read as "report.pdf and report.pdf and
    report.pdf". Mirrors the disambiguation the composer applies to chips.
    """
    normalized = {p: p.replace("\\", "/").rstrip("/") for p in paths if p}
    labels: dict[str, str] = {}
    for original, norm in normalized.items():
        segments = [s for s in norm.split("/") if s]
        if not segments:
            labels[original] = ""
            continue
        depth = 1
        while depth < len(segments):
            mine = "/".join(segments[-depth:])
            clash = any(
                other != norm and "/".join([s for s in other.split("/") if s][-depth:]) == mine
                for other in normalized.values()
            )
            if not clash:
                break
            depth += 1
        labels[original] = "/".join(segments[-depth:])[:_TITLE_MAX_ATTACHMENT_LABEL_LENGTH]
    return labels


def _strip_attached_file_tokens(
    content: str,
    attached_files: tuple[str, ...] = (),
    *,
    drop_trailing_partial: bool = False,
    labels: dict[str, str] | None = None,
    budget: list[int] | None = None,
    label_prefix: str = "",
) -> str:
    """Replace dashboard-generated ``[attached_file N] path`` references.

    The marker and its full path are replaced by the attachment's disambiguated
    NAME, not dropped. Dropping them left an attachment-only message with no
    content at all: "compare [attached_file 1] /a/x.txt and [attached_file 2]
    /b/y.txt" collapsed to "compare   and", and an attachment-only message
    collapsed to a single space. The titling model correctly answered SKIP for
    those, so every such chat fell back to a truncated default name. The name
    preserves the topic while still keeping the full path out of the title.

    ``labels`` maps path -> replacement name (see ``_attachment_labels``); pass
    ``None`` to keep the historical drop-to-space behaviour. ``budget`` is a
    single-element list carrying the remaining label allowance, so a message with
    many attachments substitutes the first few and collapses the rest rather than
    crowding out the user's own words. ``label_prefix`` is written before each
    substituted name, so a reader elsewhere (the cross-surface echo) can tell an
    attachment from a word the user typed.

    Current dashboard messages store paths in token-index order, making each
    lookup constant-time. The whitespace-delimited fallback preserves support
    for older messages without metadata.
    """
    remaining = budget if budget is not None else [_TITLE_MAX_ATTACHMENT_LABEL_BUDGET]
    prefix = "[attached_file "
    chunks: list[str] = []
    cursor = 0
    while True:
        token_start = content.find(prefix, cursor)
        if token_start < 0:
            chunks.append(content[cursor:])
            break

        if token_start > 0 and not content[token_start - 1].isspace():
            chunks.append(content[cursor : token_start + 1])
            cursor = token_start + 1
            continue

        index = token_start + len(prefix)
        digits_start = index
        # ASCII only: ``isdigit`` admits ``²`` and the like, which ``int`` rejects.
        while index < len(content) and content[index] in "0123456789":
            index += 1
        digit_count = index - digits_start
        if not 1 <= digit_count <= 2 or not content.startswith("] ", index):
            chunks.append(content[cursor : token_start + 1])
            cursor = token_start + 1
            continue

        token_index = int(content[digits_start:index])
        path_start = index + 2
        expected_path = (
            attached_files[token_index - 1] if 1 <= token_index <= len(attached_files) else ""
        )
        path_end = path_start
        if expected_path and content.startswith(expected_path, path_start):
            candidate_end = path_start + len(expected_path)
            if candidate_end == len(content) or content[candidate_end].isspace():
                path_end = candidate_end
        elif (
            drop_trailing_partial
            and expected_path
            and expected_path.startswith(content[path_start:])
        ):
            path_end = len(content)

        if path_end == path_start:
            while path_end < len(content) and not content[path_end].isspace():
                path_end += 1
        if path_end == path_start:
            chunks.append(content[cursor : token_start + 1])
            cursor = token_start + 1
            continue

        chunks.append(content[cursor:token_start])
        # Substitute the attachment's name, not a bare space. `labels=None`
        # preserves the historical drop for callers that only want the text.
        if labels is None:
            label = ""
        else:
            label = labels.get(expected_path, "")
            if not label:
                # Older message with no metadata: derive the label from whatever
                # the whitespace scan captured.
                scanned = content[path_start:path_end].strip()
                label = scanned.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
                label = label[:_TITLE_MAX_ATTACHMENT_LABEL_LENGTH]
            if len(label) > remaining[0]:
                # Budget spent — collapse the rest so the user's own text keeps
                # its place in the transcript line.
                label = ""
            else:
                remaining[0] -= len(label)
        chunks.append(f" {label_prefix}{label} " if label else " ")
        cursor = path_end

    return "".join(chunks)


def _message_attachment_paths(message: dict[str, Any]) -> tuple[str, ...]:
    """Return bounded, index-preserving paths from dashboard message metadata."""
    meta = message.get("meta")
    if not isinstance(meta, dict):
        return ()
    files = meta.get("files")
    if not isinstance(files, list):
        return ()
    return tuple(
        path if isinstance(path, str) and 0 < len(path) <= _TITLE_MAX_ATTACHMENT_PATH_LENGTH else ""
        for path in files[:_TITLE_MAX_ATTACHMENT_FILES]
    )
