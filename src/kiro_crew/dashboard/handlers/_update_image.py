"""Does the published container image exist for a release-feed version?

A container install cannot update itself, but its About panel can still say a
newer release exists. The release feed lists a version before that version's
image is built, so the feed alone could name a tag nobody can pull yet. This
module answers "can the image for *version* be pulled" with GHCR's anonymous
pull-token flow: exactly two requests, both to a fixed host with a fixed scope,
neither carrying any user credential.

The token lives only in this function's locals. It is never logged, cached or
returned; callers get a bool.
"""

from __future__ import annotations

import json
import logging

import aiohttp

from kiro_crew.dashboard.handlers._shared import read_capped_response
from kiro_crew.http_proxy import resolve_proxy
from kiro_crew.release_channel import release_refs

logger = logging.getLogger("kiro_crew.dashboard.handlers.updates")

# Fixed in code on purpose: neither config nor the feed may steer where the
# probe goes or which repository it asks about.
_REGISTRY = "https://ghcr.io"
_REPOSITORY = "kirodotdev/kirocrew"
_TOKEN_URL = f"{_REGISTRY}/token?scope=repository:{_REPOSITORY}:pull&service=ghcr.io"
_MANIFEST_URL = f"{_REGISTRY}/v2/{_REPOSITORY}/manifests/{{tag}}"
_RELEASE_NOTES_URL = "https://github.com/kirodotdev/KiroCrew/releases/tag/{ref}"
_TIMEOUT_SECS = 5
_TOKEN_MAX_BYTES = 8192
_MANIFEST_ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)


def image_ref(version: str) -> str | None:
    """The git tag (``v``-prefixed) whose image ``publish-docker.yml`` tags.

    The workflow tags the image with the release's tag minus the ``v``. A wheel
    insider stamp (``0.12.2rc2``) maps to two candidate tags; release.yml cuts
    only the ``-rc.N`` spelling today, so that one is probed and the legacy
    ``-insider.N`` spelling is not (it would be a third request). Nightly builds
    cut no tag, so there is no image to name and None is returned.
    """
    refs = release_refs(version)
    for ref in refs:
        if "-rc." in ref:
            return ref
    return refs[0] if len(refs) == 1 else None


def release_notes_url(ref: str) -> str:
    return _RELEASE_NOTES_URL.format(ref=ref)


async def image_is_pullable(ref: str) -> bool:
    """True only when GHCR answers 200 for the manifest of tag *ref*."""
    tag = ref[1:] if ref.startswith("v") else ref
    timeout = aiohttp.ClientTimeout(total=_TIMEOUT_SECS)
    try:
        # trust_env stays False and the proxy is chosen explicitly, so aiohttp
        # never reads netrc credentials into either request.
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                _TOKEN_URL, proxy=resolve_proxy(_TOKEN_URL), allow_redirects=False
            ) as resp:
                if resp.status != 200:
                    logger.debug("GHCR pull-auth request returned HTTP %s", resp.status)
                    return False
                body = await read_capped_response(resp, _TOKEN_MAX_BYTES)
            if len(body) > _TOKEN_MAX_BYTES:
                return False
            payload = json.loads(body.decode("utf-8"))
            token = payload.get("token") if isinstance(payload, dict) else None
            if not isinstance(token, str) or not token:
                return False
            url = _MANIFEST_URL.format(tag=tag)
            async with session.head(
                url,
                proxy=resolve_proxy(url),
                allow_redirects=False,
                headers={"Authorization": f"Bearer {token}", "Accept": _MANIFEST_ACCEPT},
            ) as resp:
                # 401 is NOT "exists": GHCR answers 401 for missing tags too.
                if resp.status != 200:
                    logger.debug("Image manifest for %s returned HTTP %s", tag, resp.status)
                return resp.status == 200
    except Exception as exc:  # noqa: BLE001 - any failure keeps today's text
        # Type only: an exception message could echo request headers.
        logger.debug("Image availability probe failed: %s", type(exc).__name__)
        return False
