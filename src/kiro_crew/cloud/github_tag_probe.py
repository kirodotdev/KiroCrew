"""Confirm a release tag on the public repository over HTTPS, without ``git``.

The cloud launcher pins a packaged install's crew to the release tag its own
version names, and confirms the tag with ``git ls-remote`` first
(``ec2.release_tag_exists``). A machine with no trusted ``git`` (most Windows
desktops, a Mac without the developer tools, a minimal Linux image) cannot run
that probe, so for the default public repository this module asks GitHub's REST
API the same question instead. No process is spawned, so the ``PATH``-shim
hazard the trusted-git lookup guards against does not arise here.
"""

from __future__ import annotations

import http.client
import logging
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

#: The HTTPS twin of ``ec2.PUBLIC_REPO_URL``: GitHub's REST endpoint for one
#: exact ref of that same repository. The singular ``ref`` path matches exactly
#: and answers 404 when the tag is absent; the plural ``refs`` path matches by
#: prefix, so ``v0.1`` would "find" ``v0.12.0`` there.
PUBLIC_TAG_API_URL = "https://api.github.com/repos/kirodotdev/KiroCrew/git/ref/tags/"
_USER_AGENT = "kirocrew-cloud-launch/1"


def public_tag_exists(ref: str, timeout: float) -> bool:
    """Whether the public repository carries the tag ``ref``, asked over HTTPS.

    One unauthenticated GET; HTTP 200 is a definite "yes". Anything else (a 404,
    a rate limit, no network, a timeout) logs a WARNING and answers ``False``,
    never raises: the caller then keeps the template default.
    """
    url = PUBLIC_TAG_API_URL + urllib.parse.quote(ref, safe="/")
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/vnd.github+json", "User-Agent": _USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
    except urllib.error.HTTPError as exc:
        logger.warning("tag %s not confirmed at %s (HTTP %d)", ref, url, exc.code)
        return False
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as exc:
        logger.warning("could not ask %s for tag %s: %s", url, ref, exc)
        return False
    if status != 200:
        logger.warning("tag %s not confirmed at %s (HTTP %d)", ref, url, status)
        return False
    return True
