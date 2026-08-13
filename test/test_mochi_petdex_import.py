"""Petdex.dev pet import.

Two properties carry the weight here. The first is a security boundary: the
asset URLs are parsed OUT of a shell script fetched from a third party, so a
changed or compromised host must not be able to redirect the download —
:func:`_assert_allowed_url` is re-applied to those extracted URLs, and the
counter-cases below are what keep that from being quietly deleted as redundant.

The second is that a petdex ``pet.json`` carries NO state mapping (only
``id``/``displayName``/``description``/``spritesheetPath``). Nothing in this
module may therefore invent one — the row-to-state decision belongs to the user
in the importer, and a test asserts the parsed shape stays that narrow.
"""

from __future__ import annotations

import base64
import json
import re

import pytest

from kiro_crew.apps.builtins.mochi import petdex_import as pdx

# A minimal valid WebP header: RIFF<size>WEBP, which is what petdex serves.
_WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8L" + b"\x00" * 16
_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


class TestNormalizeSlug:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("boba", "boba"),
            ("  Boba  ", "boba"),
            ("petdex.dev/pets/wangcai", "wangcai"),
            ("https://petdex.dev/pets/noir-webling", "noir-webling"),
            # A locale segment sits between the host and /pets/.
            ("https://petdex.dev/en/pets/usagi", "usagi"),
            ("https://petdex.dev/pets/tiko?ref=x#top", "tiko"),
        ],
    )
    def test_accepts_slugs_paths_and_urls(self, raw: str, expected: str) -> None:
        assert pdx.normalize_slug(raw) == expected

    @pytest.mark.parametrize("raw", ["", "   ", "../etc/passwd", "a b", "sl/ash", "x" * 80])
    def test_rejects_junk(self, raw: str) -> None:
        with pytest.raises(pdx.PetdexError):
            pdx.normalize_slug(raw)


class TestUrlAllowList:
    @pytest.mark.parametrize(
        "url",
        [
            "https://petdex.dev/install/boba",
            "https://assets.petdex.dev/curated/boba/sprite-v2.webp",
        ],
    )
    def test_allows_the_two_known_hosts(self, url: str) -> None:
        pdx._assert_allowed_url(url)  # does not raise

    @pytest.mark.parametrize(
        "url",
        [
            "http://petdex.dev/install/boba",  # plaintext
            "https://evil.example/sprite.webp",  # unrelated host
            # Suffix look-alikes: matching must be exact, not endswith().
            "https://notpetdex.dev/sprite.webp",
            "https://petdex.dev.evil.example/sprite.webp",
            "file:///etc/passwd",
        ],
    )
    def test_refuses_everything_else(self, url: str) -> None:
        with pytest.raises(pdx.PetdexError):
            pdx._assert_allowed_url(url)


class TestExtractAssetUrls:
    def test_finds_both_assets_in_a_real_install_script(self) -> None:
        script = (
            '#!/bin/sh\nset -e\nPET_DIR="$HOME/.codex/pets/boba"\nmkdir -p "$PET_DIR"\n'
            'curl -fsSL -o "$PET_DIR/pet.json" '
            "'https://assets.petdex.dev/curated/boba/petjson-v2.json'\n"
            'curl -fsSL -o "$PET_DIR/spritesheet.webp" '
            "'https://assets.petdex.dev/curated/boba/sprite-v2.webp'\n"
        )
        json_url, sheet_url = pdx._extract_asset_urls(script)
        assert json_url.endswith("petjson-v2.json")
        assert sheet_url.endswith("sprite-v2.webp")

    def test_raises_when_the_script_has_no_assets(self) -> None:
        with pytest.raises(pdx.PetdexError):
            pdx._extract_asset_urls("#!/bin/sh\necho hello\n")

    def test_extracted_urls_are_still_subject_to_the_allow_list(self) -> None:
        """The extraction itself is not a trust boundary.

        A script that points somewhere else must fail at
        :func:`_assert_allowed_url`, which ``fetch_pet`` applies to both
        extracted URLs. Without that second check, remote content chooses what
        this process downloads.
        """
        script = (
            "curl -o pet.json 'https://evil.example/petjson.json'\n"
            "curl -o sheet.webp 'https://evil.example/sprite.webp'\n"
        )
        json_url, sheet_url = pdx._extract_asset_urls(script)
        for url in (json_url, sheet_url):
            with pytest.raises(pdx.PetdexError):
                pdx._assert_allowed_url(url)


class TestParsePetJson:
    def test_keeps_only_the_four_known_fields(self) -> None:
        parsed = pdx.parse_pet_json(
            json.dumps(
                {
                    "id": "boba",
                    "displayName": "Boba",
                    "description": "An otter.",
                    "spritesheetPath": "spritesheet.webp",
                    "spriteVersionNumber": 2,
                    "somethingElse": {"nested": True},
                }
            )
        )
        assert parsed == {
            "id": "boba",
            "displayName": "Boba",
            "description": "An otter.",
            "spritesheetPath": "spritesheet.webp",
        }

    def test_carries_no_state_mapping(self) -> None:
        """The format has none, so nothing here may synthesise one."""
        parsed = pdx.parse_pet_json(json.dumps({"id": "x", "displayName": "X"}))
        for key in ("states", "moods", "rows", "rowAssignments"):
            assert key not in parsed

    @pytest.mark.parametrize(
        "text", ["not json", "[]", '"a string"', json.dumps({"description": "no id or name"})]
    )
    def test_rejects_bad_shapes(self, text: str) -> None:
        with pytest.raises(pdx.PetdexError):
            pdx.parse_pet_json(text)


class TestSniffMime:
    def test_recognises_webp_and_png(self) -> None:
        assert pdx._sniff_mime(_WEBP) == "image/webp"
        assert pdx._sniff_mime(_PNG) == "image/png"

    def test_rejects_a_non_image(self) -> None:
        with pytest.raises(pdx.PetdexError):
            pdx._sniff_mime(b"#!/bin/sh\nrm -rf /\n")


class TestInstalledPets:
    """The no-network source: pets the petdex CLI already wrote to disk."""

    @staticmethod
    def _install(root, slug: str, *, sheet: bytes = _WEBP, sheet_name: str = "spritesheet.webp"):
        pet = root / slug
        pet.mkdir(parents=True)
        (pet / "pet.json").write_text(
            json.dumps(
                {
                    "id": slug,
                    "displayName": slug.title(),
                    "description": f"{slug} pet",
                    "spritesheetPath": sheet_name,
                }
            ),
            encoding="utf-8",
        )
        (pet / sheet_name).write_bytes(sheet)
        return pet

    @pytest.fixture()
    def pets_root(self, tmp_path, monkeypatch):
        root = tmp_path / "pets"
        root.mkdir()
        monkeypatch.setattr(pdx, "INSTALLED_PETS_DIR", root)
        return root

    def test_lists_complete_pets_only(self, pets_root) -> None:
        self._install(pets_root, "boba")
        # A pet.json with no sheet is not installable art.
        (pets_root / "half").mkdir()
        (pets_root / "half" / "pet.json").write_text(
            json.dumps({"id": "half", "displayName": "Half"}), encoding="utf-8"
        )
        # A stray directory with nothing in it.
        (pets_root / "empty").mkdir()

        assert [p["slug"] for p in pdx.list_installed()] == ["boba"]

    def test_missing_folder_is_an_empty_list_not_an_error(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(pdx, "INSTALLED_PETS_DIR", tmp_path / "nope")
        assert pdx.list_installed() == []

    def test_one_unreadable_pet_does_not_hide_the_others(self, pets_root) -> None:
        self._install(pets_root, "boba")
        bad = pets_root / "broken"
        bad.mkdir()
        (bad / "pet.json").write_text("{ not json", encoding="utf-8")
        (bad / "spritesheet.webp").write_bytes(_WEBP)
        assert [p["slug"] for p in pdx.list_installed()] == ["boba"]

    def test_read_returns_the_sheet_as_base64_with_a_sniffed_mime(self, pets_root) -> None:
        self._install(pets_root, "wangcai")
        payload = pdx.read_installed("wangcai")
        assert payload["slug"] == "wangcai"
        assert payload["meta"]["displayName"] == "Wangcai"
        assert payload["imageMime"] == "image/webp"
        assert base64.b64decode(payload["imageBase64"]) == _WEBP

    def test_oversized_installed_sheet_is_rejected_before_reading(
        self, pets_root, monkeypatch
    ) -> None:
        """An oversized on-disk spritesheet must be rejected by a stat-size check
        BEFORE read_bytes(), or the whole file is allocated and can OOM the
        gateway."""
        from pathlib import Path

        monkeypatch.setattr(pdx, "_MAX_SHEET_BYTES", 8)
        self._install(pets_root, "chonk", sheet=b"x" * 64, sheet_name="spritesheet.png")

        read_calls: list[str] = []
        orig_read = Path.read_bytes

        def _spy_read(self, *a, **k):  # type: ignore[no-untyped-def]
            read_calls.append(str(self))
            return orig_read(self, *a, **k)

        monkeypatch.setattr(Path, "read_bytes", _spy_read)

        with pytest.raises(pdx.PetdexError, match="too large"):
            pdx.read_installed("chonk")
        assert not any(
            "spritesheet.png" in c for c in read_calls
        ), "the oversized sheet must not be read into memory before the guard"

    def test_read_finds_a_png_sheet_too(self, pets_root) -> None:
        self._install(pets_root, "pixel", sheet=_PNG, sheet_name="spritesheet.png")
        assert pdx.read_installed("pixel")["imageMime"] == "image/png"

    def test_read_rejects_an_unknown_pet(self, pets_root) -> None:
        with pytest.raises(pdx.PetdexError):
            pdx.read_installed("ghost-pet")

    @pytest.mark.parametrize("slug", ["../outside", "..", "a/b"])
    def test_read_cannot_escape_the_pets_folder(self, pets_root, slug: str) -> None:
        """Traversal is refused by the slug rules AND by a containment check.

        Belt and braces on purpose: the slug pattern is the first gate, but the
        pets root can itself be reached through a symlink, so the resolved path
        is re-checked against it.
        """
        (pets_root.parent / "outside").mkdir(exist_ok=True)
        with pytest.raises(pdx.PetdexError):
            pdx.read_installed(slug)

    def test_a_declared_sheet_path_cannot_point_out_of_the_pet_folder(self, pets_root) -> None:
        """``spritesheetPath`` comes from a downloaded file, so it is data.

        Only a bare filename is honoured; anything with a separator falls back to
        the known names, and here there are none — so the read fails rather than
        reaching for a file elsewhere on disk.
        """
        secret = pets_root.parent / "secret.webp"
        secret.write_bytes(_WEBP)
        pet = pets_root / "sneaky"
        pet.mkdir()
        (pet / "pet.json").write_text(
            json.dumps({"id": "sneaky", "displayName": "S", "spritesheetPath": "../secret.webp"}),
            encoding="utf-8",
        )
        with pytest.raises(pdx.PetdexError):
            pdx.read_installed("sneaky")


class TestRedirectRefusal:
    """``fetch_pet`` must never follow a redirect.

    Every URL is validated against the host allow-list BEFORE its request, so
    following a 30x would let the (third-party) server route the follow-up
    anywhere — including link-local/metadata addresses — with validation
    already behind us. The client therefore disables redirects and surfaces a
    30x as an error.
    """

    @staticmethod
    def _fake_session(status: int, recorded: list[dict]):
        class _Resp:
            def __init__(self) -> None:
                self.status = status
                self.headers = {"Location": "http://169.254.169.254/"}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        class _Session:
            def __init__(self, *a, **kw) -> None:
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            def get(self, url, **kw):
                recorded.append({"url": url, **kw})
                return _Resp()

        return _Session

    @pytest.mark.asyncio
    async def test_a_redirect_is_an_error_not_followed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import aiohttp

        recorded: list[dict] = []
        monkeypatch.setattr(aiohttp, "ClientSession", self._fake_session(302, recorded))

        with pytest.raises(pdx.PetdexError):
            await pdx.fetch_pet("boba")

        # Exactly one request went out (the 302 was fatal, not followed), and
        # it explicitly disabled redirect following.
        assert len(recorded) == 1
        assert recorded[0]["allow_redirects"] is False

    @pytest.mark.asyncio
    async def test_every_request_disables_redirects(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Source-level pin: no ``session.get`` in this module may omit
        ``allow_redirects=False`` — a new fetch added without it would silently
        reopen the redirect hole."""
        import inspect

        from kiro_crew.apps.builtins.mochi import petdex_import as mod

        src = inspect.getsource(mod)
        gets = re.findall(r"session\.get\([^)]*\)", src)
        assert gets, "expected session.get calls in petdex_import"
        for call in gets:
            assert "allow_redirects=False" in call, f"redirects not disabled in: {call}"
