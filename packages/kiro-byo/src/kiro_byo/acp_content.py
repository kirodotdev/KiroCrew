"""Pure conversions between ACP ContentBlocks and provider user messages.

The codec has no dependency on session state, routing, tools, telemetry or ACP
transport. AcpServer keeps its historical methods as compatibility delegates.
"""
from __future__ import annotations

from typing import Any

def prompt_text(params: dict[str, Any]) -> str:
    blocks = params.get("prompt") or []
    out: list[str] = []
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            out.append(block["text"])
    return "".join(out)


def prompt_message(params: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Convert ACP v1 ContentBlocks to OpenAI-compatible user content.

    Text and ResourceLink are baseline ACP prompt content and must never be
    silently discarded. Images are translated to data URLs. Embedded Resource
    blocks are accepted best-effort (text and image resources); Kiro BYO does
    *not* advertise ``embeddedContext`` yet because arbitrary binary embedded
    resources cannot be faithfully represented by Chat Completions.
    """
    blocks = params.get("prompt") or []
    parts: list[dict[str, Any]] = []
    has_image = False

    def add_text(text: str) -> None:
        if text:
            parts.append({"type": "text", "text": text})

    def add_image(data: str, mime: str) -> None:
        nonlocal has_image
        parts.append({
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{data}"},
        })
        has_image = True

    for block in blocks:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text" and isinstance(block.get("text"), str):
            add_text(block["text"])
            continue
        if kind == "image" and isinstance(block.get("data"), str):
            add_image(block["data"], str(block.get("mimeType") or "image/png"))
            continue
        if kind == "resource_link":
            uri = str(block.get("uri") or "").strip()
            name = str(block.get("name") or block.get("title") or "resource").strip()
            description = str(block.get("description") or "").strip()
            if uri:
                suffix = f" -- {description}" if description else ""
                add_text(f"[Referenced resource: {name}] {uri}{suffix}")
            continue
        if kind == "resource" and isinstance(block.get("resource"), dict):
            resource = block["resource"]
            uri = str(resource.get("uri") or "").strip()
            mime = str(resource.get("mimeType") or "").strip()
            if isinstance(resource.get("text"), str):
                heading = f"[Embedded resource: {uri}]\n" if uri else "[Embedded resource]\n"
                add_text(heading + resource["text"])
            elif isinstance(resource.get("blob"), str) and mime.lower().startswith("image/"):
                if uri:
                    add_text(f"[Embedded image resource: {uri}]")
                add_image(resource["blob"], mime or "image/png")
            else:
                # Be observable rather than silently dropping an unsupported
                # binary embedded resource. Clients should not send this while
                # embeddedContext is unadvertised, but tolerant handling makes
                # relay/version skew diagnosable.
                label = uri or "unnamed resource"
                add_text(f"[Unsupported embedded binary resource: {label}{' (' + mime + ')' if mime else ''}]")

    if not has_image:
        text = "".join(str(part.get("text") or "") for part in parts if part.get("type") == "text")
        return {"role": "user", "content": text}, False
    return {"role": "user", "content": parts}, True


def prompt_blocks_from_user_message(message: dict[str, Any]) -> list[dict[str, Any]]:
    """Reconstruct ACP prompt blocks from a persisted user-message event."""
    content = message.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    blocks: list[dict[str, Any]] = []
    if not isinstance(content, list):
        return blocks
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text" and isinstance(part.get("text"), str):
            blocks.append({"type": "text", "text": part["text"]})
            continue
        if part.get("type") == "image_url":
            raw = part.get("image_url")
            url = str((raw or {}).get("url") or "") if isinstance(raw, dict) else str(raw or "")
            if url.startswith("data:") and ";base64," in url:
                header, data = url.split(",", 1)
                mime = header[5:].split(";", 1)[0] or "image/png"
                blocks.append({"type": "image", "mimeType": mime, "data": data})
    return blocks


