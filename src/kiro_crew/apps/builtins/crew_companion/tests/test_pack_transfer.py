"""Pack transfer — export, import, and the PetDex registry.

Weighted towards the boundary this module owns: content arriving from OUTSIDE the
companion. A bundle the user picked and a sprite sheet fetched from the internet are
both untrusted, so the tests concentrate on what must never happen — a path escaping
the packs directory, an unbounded download, a silently overwritten pack, and a request
sent somewhere other than PetDex.

The network is never touched: every fetch is stubbed, so these run offline and cannot
be flaky on someone else's uptime.
"""

from __future__ import annotations

import base64
import json
import urllib.error

import pytest

from kiro_crew.apps.builtins.crew_companion import pack_transfer
from kiro_crew.apps.builtins.crew_companion.appearances import AppearanceStore

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * 64).decode("ascii")


@pytest.fixture
def store(tmp_path):
    return AppearanceStore(tmp_path)


def _bundle(pack_id="imported", **over):
    b = {
        "kind": "crew-companion-pack",
        "version": 1,
        "id": pack_id,
        "manifest": {
            "meta": {"id": pack_id, "name": "Imported", "format": "svg"},
            "states": {"idle": "idle.svg"},
        },
        "files": {"idle.svg": "<svg/>"},
    }
    b.update(over)
    return b


# ── slug parsing ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://petdex.dev/pets/tabby", "tabby"),
        ("https://petdex.dev/en/pets/tabby", "tabby"),
        ("petdex.dev/pets/Tabby?ref=x", "tabby"),
        ("tabby", "tabby"),
        ("  TABBY  ", "tabby"),
    ],
)
def test_parses_the_slug_a_user_would_paste(raw, expected):
    assert pack_transfer.parse_petdex_slug(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", None, 42, "a" * 100, "has spaces", "!!"])
def test_rejects_input_that_is_not_a_slug(raw):
    """A pasted essay must not become a request."""
    assert pack_transfer.parse_petdex_slug(raw) is None


# ── the host pin ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:6799/steal",
        "http://localhost/admin",
        "https://169.254.169.254/latest/meta-data/",
        "https://evil.test/x.png",
        "https://petdex.dev.evil.test/x.png",
        "file:///etc/passwd",
        "http://petdex.dev/x.png",  # plain HTTP, downgraded
    ],
)
def test_refuses_any_asset_url_that_is_not_petdex_over_https(url):
    """The manifest is third-party data, so the URLs inside it are untrusted.

    Without this pin a hostile manifest would make the GATEWAY issue the request —
    from inside the trust boundary, reaching hosts the attacker cannot reach directly.
    """
    assert pack_transfer._petdex_asset_url(url) is None


@pytest.mark.parametrize(
    "url", ["https://petdex.dev/a.png", "https://cdn.petdex.dev/pets/a.png"]
)
def test_allows_petdex_and_its_subdomains(url):
    assert pack_transfer._petdex_asset_url(url) == url


def test_petdex_fetch_refuses_an_off_host_spritesheet(monkeypatch):
    """A manifest that resolves but points elsewhere fails, and nothing is fetched."""
    fetched = []

    def fake_get(url, *, as_json):
        fetched.append(url)
        if as_json:
            return {"pets": [{"slug": "tabby", "spritesheetUrl": "https://evil.test/a.png"}]}
        raise AssertionError("must not download from an off-host URL")

    monkeypatch.setattr(pack_transfer, "_get", fake_get)
    result = pack_transfer.fetch_petdex_pet("tabby")
    assert result["ok"] is False
    assert fetched == [pack_transfer.PETDEX_MANIFEST_URL]


def test_petdex_fetch_returns_the_sheet_on_the_happy_path(monkeypatch):
    def fake_get(url, *, as_json):
        if as_json and url == pack_transfer.PETDEX_MANIFEST_URL:
            return {
                "pets": [
                    {
                        "slug": "tabby",
                        "displayName": "Tabby",
                        "submittedBy": "someone",
                        "spritesheetUrl": "https://petdex.dev/tabby.png",
                    }
                ]
            }
        return b"spritebytes"

    monkeypatch.setattr(pack_transfer, "_get", fake_get)
    result = pack_transfer.fetch_petdex_pet("https://petdex.dev/pets/tabby")
    assert result["ok"] is True
    assert result["displayName"] == "Tabby"
    assert base64.b64decode(result["spriteBase64"]) == b"spritebytes"


def test_petdex_reports_a_miss_rather_than_raising(monkeypatch):
    monkeypatch.setattr(
        pack_transfer, "_get", lambda url, *, as_json: {"pets": []} if as_json else b""
    )
    result = pack_transfer.fetch_petdex_pet("nobody")
    assert result["ok"] is False
    assert "nobody" in result["error"]


def test_an_unreachable_registry_is_an_error_not_a_crash(monkeypatch):
    def boom(url, *, as_json):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(pack_transfer, "_get", boom)
    assert pack_transfer.fetch_petdex_pet("tabby")["ok"] is False


def test_optional_metadata_failing_does_not_fail_the_import(monkeypatch):
    """pet.json is a nicety; losing it must not cost the user the pet."""

    def fake_get(url, *, as_json):
        if url == pack_transfer.PETDEX_MANIFEST_URL:
            return {
                "pets": [
                    {
                        "slug": "tabby",
                        "spritesheetUrl": "https://petdex.dev/t.png",
                        "petJsonUrl": "https://petdex.dev/t.json",
                    }
                ]
            }
        if url.endswith(".json"):
            raise urllib.error.URLError("gone")
        return b"bytes"

    monkeypatch.setattr(pack_transfer, "_get", fake_get)
    assert pack_transfer.fetch_petdex_pet("tabby")["ok"] is True


# ── export / import round trip ──────────────────────────────────────────────


def test_export_then_import_reproduces_the_pack(store):
    store.save_pack(
        "orig",
        {"meta": {"id": "orig", "name": "Orig"}, "states": {"idle": "idle.svg"}},
        {"idle.svg": "<svg/>"},
    )
    bundle = pack_transfer.export_bundle(store, "orig")
    assert bundle is not None and bundle["kind"] == "crew-companion-pack"

    bundle["id"] = "copy"
    bundle["manifest"]["meta"]["id"] = "copy"
    assert pack_transfer.import_bundle(store, bundle)["ok"] is True
    assert {p["id"] for p in store.list_packs()} >= {"orig", "copy"}


def test_export_of_a_missing_pack_is_none(store):
    assert pack_transfer.export_bundle(store, "ghost") is None


def test_import_refuses_to_replace_an_UNREADABLE_pack(store, tmp_path):
    """list_packs skips a pack whose manifest is corrupt, so a listing-based
    collision check was blind to it: importing a bundle under the same id
    replaced the directory and destroyed the broken pack's still-recoverable
    art. Existence is answered by the directory, not by listability."""
    broken = tmp_path / "appearances" / "wounded"
    broken.mkdir(parents=True)
    (broken / "manifest.json").write_text("{ not json", "utf-8")
    (broken / "idle.svg").write_text("<svg>precious</svg>", "utf-8")
    # Confirm the premise: the broken pack is invisible to the listing.
    assert all(p["id"] != "wounded" for p in store.list_packs())

    bundle = {
        "kind": "crew-companion-pack",
        "version": 1,
        "id": "wounded",
        "manifest": {"meta": {"id": "wounded", "name": "W"}, "states": {"idle": "idle.svg"}},
        "files": {"idle.svg": "<svg>attacker</svg>"},
    }
    result = pack_transfer.import_bundle(store, bundle)
    assert result["ok"] is False
    # The unreadable pack's art survived untouched.
    assert (broken / "idle.svg").read_text("utf-8") == "<svg>precious</svg>"


def test_import_never_collides_with_the_builtin_id(store):
    bundle = {
        "kind": "crew-companion-pack",
        "version": 1,
        "id": "kiro-ghost",
        "manifest": {"meta": {"id": "kiro-ghost", "name": "Fake"}, "states": {"idle": "idle.svg"}},
        "files": {"idle.svg": "<svg/>"},
    }
    assert pack_transfer.import_bundle(store, bundle)["ok"] is False


def test_sprite_export_keeps_the_png_extension(store):
    """Sprite slots exported as `.svg` imported fine and then rendered BLANK:
    pack_detail infers each slot's renderer from the filename extension, so
    base64 PNG data labeled `.svg` was handed to the SVG renderer. Export must
    mirror pack_detail's inference table exactly (.json/.png/.svg)."""
    store.save_pack(
        "spr",
        {
            "meta": {"id": "spr", "name": "Spr", "format": "sprite"},
            "states": {"idle": "idle.png"},
        },
        {"idle.png": "aGVsbG8="},
    )
    bundle = pack_transfer.export_bundle(store, "spr")
    assert bundle is not None
    assert bundle["manifest"]["states"]["idle"] == "idle.png"
    assert "idle.png" in bundle["files"]


def test_the_source_sheet_survives_export_and_import(store):
    """Export built files from the animation slots only, so an exported pack
    rendered fine but silently LOST its re-editable sheet -- export -> delete
    -> import destroyed it permanently. The sheet now rides in the bundle and
    comes back out of pack_detail as `sourceImage`."""
    store.save_pack(
        "sheeted",
        {
            "meta": {"id": "sheeted", "name": "S", "format": "sprite"},
            "states": {"idle": "idle.png"},
            "sprite": {"frameWidth": 32, "frameHeight": 32, "source": "source.png"},
        },
        {"idle.png": "c3RyaXA=", "source.png": "c2hlZXQ="},
    )
    bundle = pack_transfer.export_bundle(store, "sheeted")
    assert bundle is not None
    assert bundle["files"]["source.png"] == "c2hlZXQ="

    bundle["id"] = "sheetcopy"
    bundle["manifest"]["meta"]["id"] = "sheetcopy"
    assert pack_transfer.import_bundle(store, bundle)["ok"] is True
    detail = store.pack_detail("sheetcopy")
    assert detail is not None
    assert detail["sourceImage"] == "c2hlZXQ="


def test_sprite_export_import_round_trip_stays_a_sprite(store):
    """End-to-end pin: an exported sprite pack, imported under a new id, must
    still report format `sprite` per slot -- the regression rendered imported
    sprite avatars blank."""
    store.save_pack(
        "spr2",
        {
            "meta": {"id": "spr2", "name": "Spr2", "format": "sprite"},
            "states": {"idle": "idle.png"},
        },
        {"idle.png": "aGVsbG8="},
    )
    bundle = pack_transfer.export_bundle(store, "spr2")
    assert bundle is not None
    bundle["id"] = "sprcopy"
    bundle["manifest"]["meta"]["id"] = "sprcopy"
    assert pack_transfer.import_bundle(store, bundle)["ok"] is True
    detail = store.pack_detail("sprcopy")
    assert detail is not None
    assert detail["animations"]["idle"]["format"] == "sprite"


def test_export_preserves_all_three_categories(store):
    """The FOURTH sighting of the flattening bug was here: export dumped every
    slot into `states`, so an exported-then-imported pack misfiled its moods and
    random clips and the next editor save deleted them. Export now categorizes
    via pack_detail's authoritative `categories` taxonomy."""
    store.save_pack(
        "rich",
        {
            "meta": {"id": "rich", "name": "Rich"},
            "states": {"idle": "idle.svg"},
            "moods": {"happy": "happy.svg"},
            "random": {"wave": "wave.svg"},
        },
        {"idle.svg": "<svg/>", "happy.svg": "<svg/>", "wave.svg": "<svg/>"},
    )
    bundle = pack_transfer.export_bundle(store, "rich")
    assert bundle is not None
    assert set(bundle["manifest"]["states"]) == {"idle"}
    assert set(bundle["manifest"]["moods"]) == {"happy"}
    assert set(bundle["manifest"]["random"]) == {"wave"}


def test_export_import_round_trip_keeps_categories(store):
    """End-to-end pin for the whole pipeline: export -> import -> pack_detail
    must report the same taxonomy, or the editor deletes clips on the next save."""
    store.save_pack(
        "orig2",
        {
            "meta": {"id": "orig2", "name": "Orig2"},
            "states": {"idle": "idle.svg"},
            "random": {"spin": "spin.svg"},
        },
        {"idle.svg": "<svg/>", "spin.svg": "<svg/>"},
    )
    bundle = pack_transfer.export_bundle(store, "orig2")
    assert bundle is not None
    bundle["id"] = "copy2"
    assert pack_transfer.import_bundle(store, bundle)["ok"] is True
    detail = store.pack_detail("copy2")
    assert detail["randomNames"] == ["spin"]
    assert detail["categories"]["states"] == ["idle"]
    assert detail["categories"]["random"] == ["spin"]


def test_states_only_pack_exports_unchanged(store):
    """A pre-taxonomy pack (states only) keeps its old export shape."""
    store.save_pack(
        "plain",
        {"meta": {"id": "plain"}, "states": {"idle": "idle.svg"}},
        {"idle.svg": "<svg/>"},
    )
    bundle = pack_transfer.export_bundle(store, "plain")
    assert bundle is not None
    assert set(bundle["manifest"]["states"]) == {"idle"}
    assert bundle["manifest"]["moods"] == {}
    assert bundle["manifest"]["random"] == {}


def test_import_normalizes_a_spoofed_inner_identity(store):
    """The bundle names its id twice; only the outer one was validated. A bundle
    whose manifest.meta.id named an INSTALLED pack saved under the outer id but
    displayed as the victim — deleting the displayed entry deleted the victim.
    The inner id is now overwritten with the validated outer id."""
    store.save_pack(
        "victim",
        {"meta": {"id": "victim", "name": "Victim"}, "states": {"idle": "idle.svg"}},
        {"idle.svg": "<svg>victim-art</svg>"},
    )
    spoofed = _bundle("foo")
    spoofed["manifest"]["meta"]["id"] = "victim"
    result = pack_transfer.import_bundle(store, spoofed)
    assert result["ok"] is True and result["id"] == "foo"
    # The import's identity is its own, not the victim's.
    assert store.pack_detail("foo")["meta"]["id"] == "foo"
    # The victim is untouched.
    assert "victim-art" in json.dumps(store.pack_detail("victim"))


def test_import_with_no_meta_gains_one_with_the_outer_id(store):
    bad = _bundle("bare")
    bad["manifest"] = {"states": {"idle": "idle.svg"}}
    result = pack_transfer.import_bundle(store, bad)
    assert result["ok"] is True
    assert store.pack_detail("bare")["meta"]["id"] == "bare"


def test_import_refuses_to_overwrite_an_installed_pack(store):
    """Refusing beats clobbering: the user may not realise the id collides."""
    store.save_pack(
        "dup",
        {"meta": {"id": "dup"}, "states": {"idle": "idle.svg"}},
        {"idle.svg": "<svg>keep</svg>"},
    )
    result = pack_transfer.import_bundle(store, _bundle("dup"))
    assert result["ok"] is False
    assert "already installed" in result["error"]
    # The original art survived.
    assert "keep" in json.dumps(store.pack_detail("dup"))


@pytest.mark.parametrize("pack_id", ["../escape", "/abs", "..", ".", "", "a" * 200, None])
def test_import_rejects_a_pack_id_that_is_a_path(store, pack_id):
    """The bundle names its own id, which makes it the least trustworthy field."""
    assert pack_transfer.import_bundle(store, _bundle(pack_id))["ok"] is False


def test_import_rejects_a_traversing_filename(store):
    bad = _bundle(files={"../../etc/passwd": "x"})
    assert pack_transfer.import_bundle(store, bad)["ok"] is False


def test_import_rejects_a_disallowed_extension(store):
    assert pack_transfer.import_bundle(store, _bundle(files={"run.sh": "x"}))["ok"] is False


def test_import_rejects_an_oversized_file(store):
    huge = "x" * (pack_transfer.MAX_FILE_BYTES + 1)
    assert pack_transfer.import_bundle(store, _bundle(files={"idle.svg": huge}))["ok"] is False


@pytest.mark.parametrize(
    "payload",
    [
        None,
        "not a dict",
        {},
        {"kind": "something-else", "id": "x"},
        _bundle(manifest="not a dict"),
        _bundle(files={}),
    ],
)
def test_import_rejects_anything_that_is_not_a_bundle(store, payload):
    assert pack_transfer.import_bundle(store, payload)["ok"] is False


# ── sprite packs ────────────────────────────────────────────────────────────


def test_saves_a_sprite_pack(store):
    result = pack_transfer.save_sprite_pack(
        store, "sprites", {"meta": {"id": "sprites", "format": "sprite"}}, PNG
    )
    assert result["ok"] is True
    assert any(p["id"] == "sprites" for p in store.list_packs())


def test_rejects_art_that_cannot_be_decoded(store):
    """Caught here rather than at paint time, where the cause is far less obvious."""
    result = pack_transfer.save_sprite_pack(
        store, "bad", {"meta": {"id": "bad"}}, "not!base64!"
    )
    assert result["ok"] is False
    assert store.pack_detail("bad") in (None, {})


@pytest.mark.parametrize("art", ["", None, 123])
def test_rejects_missing_sprite_art(store, art):
    assert pack_transfer.save_sprite_pack(store, "x", {"meta": {"id": "x"}}, art)["ok"] is False


def test_rejects_an_oversized_sprite(store):
    big = base64.b64encode(b"x" * (pack_transfer.MAX_FILE_BYTES + 1)).decode("ascii")
    assert pack_transfer.save_sprite_pack(store, "big", {"meta": {"id": "big"}}, big)["ok"] is False


@pytest.mark.parametrize("name", ["../x.png", "run.sh", "", None])
def test_rejects_an_unsafe_sprite_filename(store, name):
    result = pack_transfer.save_sprite_pack(store, "s", {"meta": {"id": "s"}}, PNG, name)
    assert result["ok"] is False


def test_sprite_pack_id_is_validated_like_any_other(store):
    assert pack_transfer.save_sprite_pack(store, "../esc", {"meta": {"id": "x"}}, PNG)["ok"] is False


def test_a_manifest_without_meta_is_refused_not_silently_invisible(store):
    """The bug these tests caught: saved, on disk, and absent from the gallery.

    The read path lists a pack from ``manifest["meta"]``, so a manifest without it used
    to save successfully and then never appear. Refusing at the write is the whole
    point — the caller can say why.
    """
    assert store.save_pack("flat", {"id": "flat"}, {"idle.svg": "<svg/>"}) is False
    assert all(p["id"] != "flat" for p in store.list_packs())


def test_the_builtin_cannot_be_exported(store):
    """Its art lives in the frontend bundle, not on disk.

    Found live: exporting it produced a bundle with no files, which then failed to
    import with a confusing "no art in it". Refusing here names the real reason.
    """
    assert pack_transfer.export_bundle(store, "kiro-ghost") is None


def test_a_custom_pack_round_trips_through_a_bundle(store):
    """The case that actually matters: move a hand-made pack between machines."""
    store.save_pack(
        "handmade",
        {"meta": {"id": "handmade", "name": "Handmade"}, "states": {"idle": "idle.svg"}},
        {"idle.svg": "<svg id='mine'/>"},
    )
    bundle = pack_transfer.export_bundle(store, "handmade")
    assert bundle and bundle["files"], "a custom pack must export its art"
    bundle["id"] = "handmade-2"
    bundle["manifest"]["meta"]["id"] = "handmade-2"
    assert pack_transfer.import_bundle(store, bundle)["ok"] is True
    assert "mine" in str(store.pack_detail("handmade-2"))


def test_save_pack_refuses_wholesale_on_an_oversized_file(store):
    """A save that silently DROPS an oversized file and then replaces the pack
    destroys the original slot's art behind a success response. All-or-nothing:
    the save fails, the existing pack is untouched."""
    store.save_pack(
        "prior",
        {"meta": {"id": "prior"}, "states": {"idle": "idle.svg"}},
        {"idle.svg": "<svg>original</svg>"},
    )
    huge = "x" * (8 * 1024 * 1024 + 1)
    ok = store.save_pack(
        "prior",
        {"meta": {"id": "prior"}, "states": {"idle": "idle.svg", "walk": "walk.svg"}},
        {"idle.svg": "<svg>new</svg>", "walk.svg": huge},
    )
    assert ok is False
    # The prior art survived the refused overwrite.
    assert "original" in json.dumps(store.pack_detail("prior"))


def test_save_pack_refuses_case_colliding_filenames(store):
    """`idle.svg` and `IDLE.SVG` are distinct dict keys but ONE file on the
    case-insensitive filesystems macOS and Windows default to — the second
    write silently replaced the first while the save reported success. The
    save refuses wholesale, and an existing pack survives the refusal."""
    store.save_pack(
        "cased",
        {"meta": {"id": "cased"}, "states": {"idle": "idle.svg"}},
        {"idle.svg": "<svg>original</svg>"},
    )
    ok = store.save_pack(
        "cased",
        {
            "meta": {"id": "cased"},
            "states": {"idle": "idle.svg", "shout": "IDLE.SVG"},
        },
        {"idle.svg": "<svg>new</svg>", "IDLE.SVG": "<svg>shout</svg>"},
    )
    assert ok is False
    # The prior art survived the refused overwrite.
    assert "original" in json.dumps(store.pack_detail("cased"))


def test_save_pack_refuses_an_oversized_manifest(store):
    """Every pack FILE is size-capped, but the generated manifest itself was
    not — while the read path rejects an oversized manifest.json. An overwrite
    that wrote one swapped the target and deleted the backup, then the pack
    vanished from the gallery: data loss reported as success. The save must
    refuse before touching the existing pack."""
    store.save_pack(
        "prior-m",
        {"meta": {"id": "prior-m"}, "states": {"idle": "idle.svg"}},
        {"idle.svg": "<svg>original</svg>"},
    )
    ok = store.save_pack(
        "prior-m",
        {
            "meta": {
                "id": "prior-m",
                "description": "d" * (8 * 1024 * 1024 + 1),
            },
            "states": {"idle": "idle.svg"},
        },
        {"idle.svg": "<svg>new</svg>"},
    )
    assert ok is False
    # The prior pack is intact and still readable.
    assert "original" in json.dumps(store.pack_detail("prior-m"))


def test_save_pack_refuses_wholesale_on_an_unsafe_filename(store):
    ok = store.save_pack(
        "unsafe",
        {"meta": {"id": "unsafe"}, "states": {"idle": "../escape.svg"}},
        {"../escape.svg": "<svg/>"},
    )
    assert ok is False
