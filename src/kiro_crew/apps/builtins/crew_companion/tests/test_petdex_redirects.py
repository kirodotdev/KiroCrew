"""PetDex fetches must stay pinned to PetDex — including across redirects.

The import path takes a link to a third-party pet gallery, reads a manifest from it,
and then fetches the asset URLs that manifest names. Those URLs are untrusted even
though the manifest came from a known host, so the code pins scheme and host before
fetching. The hole this file guards is that the pin was applied to the FIRST url only:
`urlopen` follows redirects by default, so a valid `https://petdex.dev/...` URL could
answer 302 -> `http://127.0.0.1:6799/` and the gateway would fetch it and hand the
bytes back as sprite data. The request originates inside the trust boundary, which is
exactly the attack the validation was written to stop — arriving one line later.

These tests drive the redirect handler directly rather than over a socket: the
decision being tested is "would this hop be followed", which is a pure function of the
URL.
"""

from __future__ import annotations

import inspect
import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from kiro_crew.apps.builtins.crew_companion import pack_transfer


def _would_follow(newurl: str) -> bool:
    """True when the redirect handler would follow a hop to `newurl`."""
    handler = pack_transfer._PinnedRedirects()  # noqa: SLF001
    req = urllib.request.Request("https://petdex.dev/api/manifest")
    try:
        out = handler.redirect_request(req, None, 302, "Found", {}, newurl)
    except urllib.error.HTTPError:
        return False
    return out is not None


class TestRedirectsStayPinned:
    @pytest.mark.parametrize(
        "target",
        [
            "http://127.0.0.1:6799/api/config",       # the gateway itself
            "http://localhost:6799/api/config",
            "http://169.254.169.254/latest/meta-data/",  # cloud metadata
            "http://10.0.0.5/internal",
            "https://evil.example.com/pet.png",       # https, wrong host
            "http://petdex.dev/pet.png",              # right host, downgraded scheme
            "https://petdex.dev.evil.com/pet.png",    # suffix confusion
            "file:///etc/passwd",
        ],
    )
    def test_a_hop_off_petdex_is_refused(self, target):
        assert not _would_follow(target), f"followed a redirect to {target}"

    @pytest.mark.parametrize(
        "target",
        [
            "https://petdex.dev/assets/pet.png",
            "https://cdn.petdex.dev/assets/pet.png",  # a subdomain is still PetDex
        ],
    )
    def test_a_hop_within_petdex_is_allowed(self, target):
        # The pin must not break the ordinary case: PetDex may legitimately redirect
        # to its own CDN, and refusing that would make the feature useless.
        assert _would_follow(target), f"refused a legitimate redirect to {target}"

    def test_the_fetcher_uses_the_validating_opener(self):
        # A regression guard on the wiring, not the logic: building the handler and
        # then calling the module-level `urlopen` anyway would leave the hole open
        # while every test above still passed.
        src = inspect.getsource(pack_transfer._get)  # noqa: SLF001
        assert "_OPENER.open(" in src
        assert "urllib.request.urlopen(" not in src


class TestManifestDeclaresNetwork:
    def test_the_app_declares_the_network_permission_it_uses(self):
        # The manifest is what tells the platform and the user what this app does.
        # It said `network: false` while the import path made outbound HTTPS requests.
        manifest = json.loads(
            (Path(pack_transfer.__file__).parent / "app.json").read_text(encoding="utf-8")
        )
        assert manifest["permissions"]["network"] is True
