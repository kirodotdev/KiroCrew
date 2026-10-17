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
import urllib.error
import urllib.request

import pytest

from kiro_crew.appearance_packs import transfer as pack_transfer


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
            "http://127.0.0.1:6799/api/config",  # the gateway itself
            "http://localhost:6799/api/config",
            "http://169.254.169.254/latest/meta-data/",  # cloud metadata
            "http://10.0.0.5/internal",
            "https://evil.example.com/pet.png",  # https, wrong host
            "http://petdex.dev/pet.png",  # right host, downgraded scheme
            "https://petdex.dev.evil.com/pet.png",  # suffix confusion
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
        assert "_opener().open(" in src
        assert "urllib.request.urlopen(" not in src

    def test_every_fetch_shares_one_pinned_opener(self):
        first = pack_transfer._opener()  # noqa: SLF001
        assert pack_transfer._opener() is first  # noqa: SLF001
        pinned = pack_transfer._PinnedRedirects  # noqa: SLF001
        assert any(isinstance(h, pinned) for h in first.handlers)

    def test_importing_the_dashboard_modules_does_not_build_the_opener(self):
        """Proved in a fresh interpreter: the dashboard route table imports
        these modules before the socket binds, so the opener waits for a fetch."""
        import subprocess
        import sys

        code = (
            "import kiro_crew.dashboard.appearances\n"
            "import kiro_crew.dashboard.handlers.appearances\n"
            "from kiro_crew.appearance_packs import transfer\n"
            "print(transfer._shared_opener is None)\n"
        )
        out = subprocess.run(
            [sys.executable, "-B", "-c", code],  # -B: no __pycache__ in the checkout
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
            check=True,
        )
        assert out.stdout.strip() == "True", out.stdout
