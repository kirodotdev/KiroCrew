"""Slack file attachment processing — images, text, and unsupported types.

Downloads files shared in Slack messages and converts them into text
that can be injected into the LLM prompt.  Images are saved to temp
files so ``AcpClient._send_prompt()`` can inline them as base64 content
blocks.  Text files are read and injected directly.

Safety controls:
- Mimetype allowlist (no executables or archives)
- File size caps checked *before* download (from Slack metadata)
- Temp file cleanup in ``finally`` blocks
- Credential / exfiltration URL redaction on text content
- SEL audit logging on every download / skip / error
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from typing import TYPE_CHECKING

from kiro_crew.doc_parser import extract_text, is_parseable_document
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel

if TYPE_CHECKING:
    from kiro_crew.slack.gateway import GatewayOrchestrator

logger = logging.getLogger(__name__)

# ── Size limits ──
_MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10 MB
_MAX_TEXT_BYTES = 512 * 1024  # 512 KB download cap
_MAX_TEXT_INJECT = 50 * 1024  # 50 KB injected into prompt

# ── Mimetype allowlists ──
# Image types must match AcpClient._send_prompt() regex:
# png|jpg|jpeg|gif|webp|bmp
_IMAGE_MIMETYPES = {
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "image/bmp",
}
_TEXT_PREFIXES = ("text/",)
_TEXT_EXACT = {
    "application/json",
    "application/xml",
    "application/javascript",
}

# ── Audio mimetypes handled by transcribe.py — skip here ──
_AUDIO_PREFIXES = ("audio/", "video/webm")

# Sanitize filetype to alphanumeric only
_SAFE_SUFFIX_RE = re.compile(r"[^a-zA-Z0-9]")


def _safe_suffix(filetype: str, default: str = "bin") -> str:
    """Return a safe file extension from a Slack filetype field."""
    clean = _SAFE_SUFFIX_RE.sub("", filetype)
    return "." + (clean or default)


def _is_audio(mimetype: str) -> bool:
    return any(mimetype.startswith(p) for p in _AUDIO_PREFIXES)


def _is_image(mimetype: str) -> bool:
    return mimetype in _IMAGE_MIMETYPES


def _is_text(mimetype: str) -> bool:
    if any(mimetype.startswith(p) for p in _TEXT_PREFIXES):
        return True
    return mimetype in _TEXT_EXACT


def _is_document(mimetype: str, name: str = "") -> bool:
    return is_parseable_document(mimetype=mimetype, filename=name)


async def process_slack_files(
    orch: GatewayOrchestrator,
    files: list[dict],
) -> tuple[list[str], list[str]]:
    """Process non-audio file attachments from a Slack message.

    Returns:
        (image_paths, text_blocks) — local paths for images (caller
        must clean up) and text strings ready for prompt injection.
    """
    if not orch.slack:
        return [], []

    image_paths: list[str] = []
    text_blocks: list[str] = []

    for f in files:
        mimetype = f.get("mimetype", "")
        name = f.get("name", "unknown")
        size = f.get("size", 0)

        if _is_audio(mimetype):
            continue

        url = f.get("url_private_download") or f.get("url_private", "")
        if not url:
            continue

        if _is_image(mimetype):
            path = await _download_image(orch, f, url, name, size)
            if path:
                image_paths.append(path)

        elif _is_text(mimetype):
            block = await _download_text(orch, f, url, name, size)
            if block:
                text_blocks.append(block)

        elif _is_document(mimetype, name):
            block = await _download_document(orch, f, url, name, size)
            if block:
                text_blocks.append(block)

        else:
            note = f"[Attached file: {name} ({mimetype}," f" {size} bytes) — unsupported type]"
            text_blocks.append(note)
            sel().log_api_access(
                caller="file_processor",
                operation="slack.file_skip",
                outcome="skipped",
                source="slack",
                resources=name,
                error=f"unsupported mimetype: {mimetype}",
            )

    return image_paths, text_blocks


async def _download_image(
    orch: GatewayOrchestrator,
    f: dict,
    url: str,
    name: str,
    size: int,
) -> str | None:
    """Download an image to a temp file. Returns path or None."""
    if size > _MAX_IMAGE_BYTES:
        sel().log_api_access(
            caller="file_processor",
            operation="slack.file_skip",
            outcome="skipped",
            source="slack",
            resources=name,
            error=f"image too large: {size} bytes",
        )
        return None

    suffix = _safe_suffix(f.get("filetype", "png"), "png")
    fd, dest = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    ok = False
    try:
        if not orch.slack:
            return None
        await orch.slack.download_file(url, dest)
        sel().log_api_access(
            caller="file_processor",
            operation="slack.download_file",
            outcome="success",
            source="slack",
            resources=name,
        )
        logger.info("Downloaded image: %s (%d bytes)", name, size)
        ok = True
        return dest
    except Exception:
        logger.exception("Failed to download image %s", name)
        sel().log_api_access(
            caller="file_processor",
            operation="slack.download_file",
            outcome="error",
            source="slack",
            resources=name,
            error="download_failed",
        )
        return None
    finally:
        if not ok:
            try:
                os.unlink(dest)
            except OSError:
                pass


async def _download_text(
    orch: GatewayOrchestrator,
    f: dict,
    url: str,
    name: str,
    size: int,
) -> str | None:
    """Download a text file and return content for prompt injection."""
    if size > _MAX_TEXT_BYTES:
        sel().log_api_access(
            caller="file_processor",
            operation="slack.file_skip",
            outcome="skipped",
            source="slack",
            resources=name,
            error=f"text file too large: {size} bytes",
        )
        return f"[Attached file: {name} ({size} bytes)" f" — too large to inline]"

    fd, dest = tempfile.mkstemp(suffix=".txt")
    os.close(fd)
    try:
        if not orch.slack:
            return None
        await orch.slack.download_file(url, dest)
        with open(dest, "r", encoding="utf-8", errors="replace") as fh:
            content = fh.read()
        sel().log_api_access(
            caller="file_processor",
            operation="slack.download_file",
            outcome="success",
            source="slack",
            resources=name,
        )

        content, _ = redact_exfiltration_urls(content)
        content, _ = redact_credentials(content)

        truncated = ""
        if len(content) > _MAX_TEXT_INJECT:
            content = content[:_MAX_TEXT_INJECT]
            truncated = "\n[… truncated]"

        logger.info(
            "Downloaded text file: %s (%d chars)",
            name,
            len(content),
        )
        return f"[File: {name}]\n{content}{truncated}\n[End of file]"
    except Exception:
        logger.exception("Failed to download text file %s", name)
        sel().log_api_access(
            caller="file_processor",
            operation="slack.download_file",
            outcome="error",
            source="slack",
            resources=name,
            error="download_failed",
        )
        return None
    finally:
        try:
            os.unlink(dest)
        except OSError:
            pass


_MAX_DOC_BYTES = 20 * 1024 * 1024  # 20 MB download cap for documents


async def _download_document(
    orch: "GatewayOrchestrator",
    f: dict,
    url: str,
    name: str,
    size: int,
) -> str | None:
    """Download a document file, extract text, and return for prompt injection."""
    if size > _MAX_DOC_BYTES:
        sel().log_api_access(
            caller="file_processor",
            operation="slack.file_skip",
            outcome="skipped",
            source="slack",
            resources=name,
            error=f"document too large: {size} bytes",
        )
        return f"[Attached document: {name} ({size} bytes) — too large to parse]"

    suffix = _safe_suffix(f.get("filetype", "bin"), "bin")
    fd, dest = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    try:
        if not orch.slack:
            return None
        await orch.slack.download_file(url, dest)
        sel().log_api_access(
            caller="file_processor",
            operation="slack.download_file",
            outcome="success",
            source="slack",
            resources=name,
        )

        content = extract_text(dest, mimetype=f.get("mimetype", ""), filename=name)
        if not content:
            return f"[Attached document: {name} — could not extract text]"

        content, _ = redact_exfiltration_urls(content)
        content, _ = redact_credentials(content)

        truncated = ""
        if len(content) > _MAX_TEXT_INJECT:
            content = content[:_MAX_TEXT_INJECT]
            truncated = "\n[… truncated]"

        logger.info("Parsed document: %s (%d chars)", name, len(content))
        return f"[Document: {name}]\n{content}{truncated}\n[End of document]"
    except Exception:
        logger.exception("Failed to parse document %s", name)
        sel().log_api_access(
            caller="file_processor",
            operation="slack.download_file",
            outcome="error",
            source="slack",
            resources=name,
            error="document_parse_failed",
        )
        return f"[Attached document: {name} — parse error]"
    finally:
        try:
            os.unlink(dest)
        except OSError:
            pass
