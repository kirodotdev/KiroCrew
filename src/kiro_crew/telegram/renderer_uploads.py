"""Telegram's outbound image upload: local image references a sealed answer sends.

Authorizes the provider's cwd as the extraction root, pulls local image references out
of a sealed segment off-loop (fail-soft, with each refusal named in the answer), and
ships the pictures as their own message after the text, restoring the references when
the upload fails. Each function here whose first parameter is ``self`` is bound as the
``TelegramRenderer`` attribute of the same name in :mod:`kiro_crew.telegram.renderer`,
which keeps the renderer's state, so ``self.<name>(...)`` calls and instance and class
patches reach it as they reach a method defined in the class body. They read
``_chat_id``, ``_client``, ``_session_key``, ``_thread_id``, ``_upload_root``,
``_scratch_root``, ``_uploads_allowed`` and ``capabilities``, and write ``_upload_root``
and ``_scratch_root``. New upload work goes here.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from kiro_crew.messaging.outbound_files import (
    ExtractLimits,
    OutboundFile,
    Rejection,
    extract_local_refs_off_loop,
)
from kiro_crew.sel import sel
from kiro_crew.telegram.client import (
    TELEGRAM_MAX_MEDIA_GROUP,
    TELEGRAM_MAX_PHOTO_BYTES,
    TELEGRAM_MAX_TOTAL_UPLOAD_BYTES,
)
from kiro_crew.telegram.renderer_display import _display_safe, _utf16_chunks, _utf16_len

if TYPE_CHECKING:
    from kiro_crew.telegram.renderer import TelegramRenderer

logger = logging.getLogger("kiro_crew.telegram.renderer")


#: Budgets handed to the shared extractor, so an oversize image is refused BY
#: THE READ and keeps its markup rather than being uploaded and 413'd, or
#: stripped out of the text and then dropped.
_UPLOAD_LIMITS = ExtractLimits(
    max_files=TELEGRAM_MAX_MEDIA_GROUP,
    max_total_bytes=TELEGRAM_MAX_TOTAL_UPLOAD_BYTES,
    max_file_bytes=TELEGRAM_MAX_PHOTO_BYTES,
)

#: Refusal lines appended to an answer before the count collapses into a tally.
_MAX_REJECTION_LINES = 3


def authorize_upload_root(self: TelegramRenderer, root: object, scratch_root: object = "") -> None:
    """Authorize the provider's resolved cwd as extraction's approved root.

    Anything that is not an ABSOLUTE STRING PATH disables uploads rather than
    widening them: this root is the trust boundary extraction measures every
    reference against — it refuses a path lexically outside the root before
    any metadata probe — so a value it cannot evaluate must be no root at all,
    not a root it guesses at.

    *scratch_root* is the session's own ``$KIROCREW_SCRATCH`` directory. It is
    admitted only beside a valid cwd, and extraction re-checks that it is one
    allocation under the managed scratch root
    (``outbound_files.session_scratch_root``) before it counts.
    """
    if isinstance(root, str) and os.path.isabs(root):
        self._upload_root = root
        self._scratch_root = (
            scratch_root if isinstance(scratch_root, str) and os.path.isabs(scratch_root) else ""
        )
        return
    self._upload_root = ""
    self._scratch_root = ""
    if root:
        # A non-empty root we cannot trust is worth a line: it is the
        # difference between "this instance has no provider cwd" and "the cwd
        # changed shape", and both present as uploads silently not happening.
        logger.info("telegram: refusing an untrusted upload root (%s)", type(root).__name__)


def _uploads_enabled(self: TelegramRenderer) -> bool:
    """Transport capability AND an unrestricted session AND a trusted root."""
    return (
        bool(self.capabilities.files_outbound) and self._uploads_allowed and bool(self._upload_root)
    )


async def _extract_uploads(self: TelegramRenderer, text: str) -> tuple[str, list[OutboundFile]]:
    """Pull local image references out of one sealed segment, fail-soft.

    Runs off-loop (extraction reads files) and never raises into the seal: a
    failure here must cost the picture, not the answer.
    """
    try:
        result = await extract_local_refs_off_loop(
            text,
            within_root=self._upload_root,
            limits=_UPLOAD_LIMITS,
            scratch_root=self._scratch_root,
        )
    except Exception:
        logger.warning("telegram: outbound file extraction failed", exc_info=True)
        return text, []
    if result.rejections:
        sel().log_api_access(
            caller=self._session_key or "telegram",
            operation="telegram_renderer.upload_files",
            outcome="denied",
            source="telegram",
            resources=f"{len(result.rejections)} rejection(s)",
            # Only the closed reason CODES — never the LLM-authored destination.
            error=",".join(sorted({item.reason for item in result.rejections})),
        )
    body = result.rewritten_text.strip()
    if not body and not result.files:
        body = text
    if result.rejections:
        body = self._append_rejections(body, result.rejections)
    if result.files:
        sel().log_api_access(
            caller=self._session_key or "telegram",
            operation="telegram_renderer.upload_files",
            outcome="allowed",
            source="telegram",
            resources=f"{len(result.files)} file(s)",
        )
    return body, result.files


def _append_rejections(self: TelegramRenderer, body: str, rejections: list[Rejection]) -> str:
    """Name every refusal in the answer, as long as the budget permits.

    A file dropped in silence leaves a reply that talks about a picture with
    no picture and no explanation, so the reasons are surfaced; past
    ``_MAX_REJECTION_LINES`` they collapse into a tally rather than crowding
    out the answer.
    """
    for rejection in rejections:
        logger.info("telegram: local image not uploaded (%s)", rejection.reason)
    lines = [f"⚠️ {rejection}" for rejection in rejections[:_MAX_REJECTION_LINES]]
    if len(rejections) > _MAX_REJECTION_LINES:
        lines.append(f"⚠️ …and {len(rejections) - _MAX_REJECTION_LINES} more")
    note = "\n".join(lines)
    if len(body) + len(note) + 2 > self._limit():
        return body
    return f"{body}\n\n{note}"


async def _send_uploads(self: TelegramRenderer, files: list[OutboundFile]) -> None:
    """Ship the extracted images as their own message, after the text seal.

    Photos deliberately do NOT ride the answer as a caption. A caption is
    capped at 1024 characters against the message's 4096, carries no
    ``reply_markup`` on an album, and the answer has already been rendered
    through this channel's HTML/table machinery — folding a truncated second
    copy into a caption would be strictly worse than one clean bubble followed
    by its pictures. ``disable_notification`` because the answer bubble
    already pinged.

    On failure the REFERENCES are restored, not the segment. Unlike Discord —
    which sends text and files in one multipart call, so recovery has to
    re-post the whole thing — the text bubble here has already landed, so
    re-posting the source would duplicate the entire answer. The markup is
    rebuilt from each ``OutboundFile``'s own alt and path (``path`` is
    provenance for exactly this) so the user learns which picture is missing
    and where it is, without the answer arriving twice.
    """
    if not files:
        return
    try:
        sent = await self._client.send_media_group(
            self._chat_id,
            files,
            message_thread_id=self._thread_id,
            disable_notification=True,
        )
    except Exception:
        logger.warning("telegram: image upload raised", exc_info=True)
        sent = []
    if sent:
        return
    logger.warning("telegram: upload of %d image(s) failed; restoring their markup", len(files))
    # The alt text is LLM-authored and the markup itself can reassemble a
    # credential out of formatting Telegram then hides, so the restored
    # references are redacted against the rendered form. Markup that concealed
    # a secret loses its formatting rather than its redaction — the documented
    # direction of that trade.
    restored = _display_safe("\n".join(f"![{item.alt or 'image'}]({item.path})" for item in files))
    # A single truncated bubble keeps only what fits under the cap, so with
    # several failed images the LATER references vanish silently. Measuring
    # the cap in code points also mismatches Telegram's UTF-16 count, so
    # emoji-dense alt text passes the slice and then bounces at the API.
    # Chunk the redacted whole by UTF-16 budget instead (redaction
    # first, so the scanner saw the contiguous text; a chunk is a pure
    # substring of it). Header rides the first bubble only.
    header = "⚠️ Couldn't upload:\n"
    budget = self._limit() - _utf16_len(header)
    for index, chunk in enumerate(_utf16_chunks(restored, budget)):
        await self._client.send_message(
            self._chat_id,
            f"{header}{chunk}" if index == 0 else chunk,
            message_thread_id=self._thread_id,
            disable_notification=True,
        )
