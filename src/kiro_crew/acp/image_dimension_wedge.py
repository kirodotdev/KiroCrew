"""The provider's many-image PIXEL-DIMENSION cap: detection and the wedge's
user-facing message.

Once a conversation holds more than ~20 images, every image in it must be
<= 2000 px on its longest edge, and ONE oversized image rejects the whole turn
with "image dimensions exceed max allowed size for many-image requests: 2000
pixels". The offending bytes are an image already in the NATIVE history
(kiro-cli's built-in ``read``/Image tool inlines a file at full resolution and
never checks its pixel size), so Resume re-sends the identical payload and fails
identically -- but UNLIKE the structural rejections it sits beside this one is
RECOVERABLE in place: the oversized images can be downscaled where they sit in
the transcript (``session_image_repair``), losing no turns, rather than needing
a fresh conversation. So it carries its own tag (``image_dimension_wedge``),
distinct from ``context_overflow`` whose only fix is a reset.

This module holds the parts specific to that cap so ``transport_errors`` keeps
the shared classifier scaffolding; the two call ``is_many_image_dimension`` and
``format_many_image_dimension_message`` where the sibling error families are
handled inline.
"""

from __future__ import annotations

import re

from kiro_crew.acp.types import ACP_BACKEND_KIRO

# Matched against the raw provider ``data`` field only, like the image-format
# sibling, so a phrase echo in the JSON-RPC ``message`` cannot stamp an
# unrelated error. Anchored to the dimension verdict, not to any sentence that
# merely mentions images.
_RE_MANY_IMAGE_DIMENSION = re.compile(
    r"image dimensions? exceed max allowed size for many-image requests",
    re.IGNORECASE,
)


def is_many_image_dimension(data: str) -> bool:
    """True when ``data`` is the provider's many-image pixel-dimension refusal."""
    return bool(_RE_MANY_IMAGE_DIMENSION.search(data))


def format_many_image_dimension_message(backend: str, req_id_suffix: str) -> str:
    """The user-facing text for the many-image pixel cap. The provider's sentence
    first, then the fix. Surface-blind like its image siblings: it names no
    dashboard control and no command, only the remedy.

    The remedy DIFFERS by harness, because the dashboard's "Repair images and
    resume" card is wired only for Kiro (the ``image_dimension_wedge`` tag is set
    only on that backend). Telling a non-Kiro harness to repair in place would
    name a recovery it has no way to run, so those harnesses are told to start a
    fresh conversation -- the recovery they do have -- instead.
    """
    if backend in ("", ACP_BACKEND_KIRO):
        recovery = (
            "Recover by repairing the oversized images in place -- "
            "downscaling them in the stored conversation -- which loses no "
            "turns, unlike starting a fresh session."
        )
    else:
        recovery = "Start a new conversation so the oversized images are not replayed."
    return (
        "An image already in this conversation is larger than the model's "
        "2000-pixel limit for conversations with many images, so the whole "
        "turn was refused. Retrying will not help. "
        f"{recovery}"
        f"{req_id_suffix}"
    )
