"""Re-editing a pack must not delete the categories the editor never saw.

A manifest has three art sections: required `states`, optional `moods`, and
open-ended `random` clips. `pack_detail` returned only `states`, and the editor
saves what it was handed — so loading a PetDex pack with random clips and pressing
save rebuilt the pack WITHOUT them, deleting the art. Nothing errored; the clips
simply stopped playing.
"""

from __future__ import annotations

import json

from kiro_crew.apps.builtins.crew_companion import appearances as ap


def _manifest() -> dict:
    return {
        "meta": {"id": "petdexish", "format": "svg", "type": "custom"},
        "states": {"idle": "idle.svg"},
        "moods": {"happy": "happy.svg"},
        "random": {"wave": "random-wave.svg"},
    }


FILES = {
    "idle.svg": "<svg id='idle'/>",
    "happy.svg": "<svg id='happy'/>",
    "random-wave.svg": "<svg id='wave'/>",
}


class TestDetailCarriesEveryCategory:
    def test_states_moods_and_random_all_come_back(self, tmp_path):
        store = ap.AppearanceStore(tmp_path)
        assert store.save_pack("petdexish", _manifest(), FILES)

        detail = store.pack_detail("petdexish")
        assert detail is not None
        anim = detail["animations"]
        assert set(anim) == {"idle", "happy", "wave"}, f"detail dropped art: {sorted(anim)}"
        assert anim["wave"]["content"] == "<svg id='wave'/>"

    def test_a_round_trip_through_the_editor_keeps_the_random_clip(self, tmp_path):
        """The data-loss path: load a pack, save it back, lose nothing."""
        store = ap.AppearanceStore(tmp_path)
        assert store.save_pack("petdexish", _manifest(), FILES)

        # What the editor does: read the detail, then save from what it read.
        detail = store.pack_detail("petdexish")
        assert detail is not None
        anim = detail["animations"]
        rebuilt_files = {f"{slot}.svg": entry["content"] for slot, entry in anim.items()}
        rebuilt = {
            "meta": detail["meta"] | {"format": "svg"},
            "states": {"idle": "idle.svg"},
            "moods": {"happy": "happy.svg"},
            "random": {"wave": "wave.svg"},
        }
        assert store.save_pack("petdexish", rebuilt, rebuilt_files)

        reloaded = store.pack_detail("petdexish")
        assert reloaded is not None
        after = reloaded["animations"]
        assert "wave" in after, "the random clip was lost on re-save"
        assert "happy" in after, "the mood clip was lost on re-save"

    def test_a_pack_with_only_states_is_unaffected(self, tmp_path):
        store = ap.AppearanceStore(tmp_path)
        m = {"meta": {"id": "plain", "format": "svg", "type": "custom"},
             "states": {"idle": "idle.svg"}}
        assert store.save_pack("plain", m, {"idle.svg": "<svg/>"})
        plain = store.pack_detail("plain")
        assert plain is not None
        assert set(plain["animations"]) == {"idle"}


class TestDetailNamesTheRandomExtras:
    """`randomNames` tells the editor which flat-map keys are open-ended random
    clips. Without it the editor can't tell a random clip apart from a state or
    mood in the folded map, treats them as absent, and drops them on re-save.
    """

    def test_random_names_lists_exactly_the_random_slots_in_manifest_order(self, tmp_path):
        store = ap.AppearanceStore(tmp_path)
        manifest = {
            "meta": {"id": "petdexish", "format": "svg", "type": "custom"},
            "states": {"idle": "idle.svg"},
            "moods": {"happy": "happy.svg"},
            # Insertion order is manifest order; detail must preserve it.
            "random": {"wave": "random-wave.svg", "spin": "random-spin.svg"},
        }
        files = {
            "idle.svg": "<svg id='idle'/>",
            "happy.svg": "<svg id='happy'/>",
            "random-wave.svg": "<svg id='wave'/>",
            "random-spin.svg": "<svg id='spin'/>",
        }
        assert store.save_pack("petdexish", manifest, files)

        detail = store.pack_detail("petdexish")
        assert detail is not None
        # Only the random slots, and only those — not states or moods.
        assert detail["randomNames"] == ["wave", "spin"]

    def test_pack_with_no_random_section_returns_empty_list(self, tmp_path):
        store = ap.AppearanceStore(tmp_path)
        manifest = {
            "meta": {"id": "plain", "format": "svg", "type": "custom"},
            "states": {"idle": "idle.svg"},
            "moods": {"happy": "happy.svg"},
        }
        assert store.save_pack("plain", manifest, {"idle.svg": "<svg/>", "happy.svg": "<svg/>"})
        detail = store.pack_detail("plain")
        assert detail is not None
        assert detail["randomNames"] == []

    def test_random_entry_with_missing_file_is_excluded(self, tmp_path):
        """A random clip whose content won't load is skipped from animations, and
        must likewise be absent from randomNames — otherwise the editor would try
        to load a clip that isn't there.
        """
        store = ap.AppearanceStore(tmp_path)
        manifest = {
            "meta": {"id": "gappy", "format": "svg", "type": "custom"},
            "states": {"idle": "idle.svg"},
            "random": {"wave": "random-wave.svg", "ghost": "random-ghost.svg"},
        }
        # `ghost`'s file is deliberately not provided.
        assert store.save_pack("gappy", manifest, {
            "idle.svg": "<svg/>",
            "random-wave.svg": "<svg id='wave'/>",
        })
        detail = store.pack_detail("gappy")
        assert detail is not None
        assert "ghost" not in detail["animations"]
        assert detail["randomNames"] == ["wave"]


class TestMalformedMetaOnDisk:
    def test_detail_tolerates_a_non_dict_meta(self, tmp_path):
        """`.get("meta", {})` only defaults when the key is ABSENT — a
        hand-edited or legacy manifest with `"meta": []` reached
        `[].get("format")` and the detail endpoint 500ed; for the active
        pack the avatar could not load at all. Save refuses non-dict meta,
        so stage the manifest directly on disk the way an older writer or
        a hand edit would leave it."""
        store = ap.AppearanceStore(tmp_path)
        pack_dir = tmp_path / ap.PACKS_DIRNAME / "handmade"
        pack_dir.mkdir(parents=True)
        (pack_dir / "manifest.json").write_text(
            json.dumps({"meta": [], "states": {"idle": "idle.svg"}}), "utf-8"
        )
        (pack_dir / "idle.svg").write_text("<svg id='idle'/>", "utf-8")

        detail = store.pack_detail("handmade")
        assert detail is not None
        assert detail["animations"]["idle"]["content"] == "<svg id='idle'/>"
        # Falls back to the svg default and the builtin meta rather than crashing.
        assert detail["animations"]["idle"]["format"] == "svg"
