"""The conductor's ledger item-entry codec (``scripts/ledger_entry.py``).

The codec is the ONE code owner of the entry format the conductor persists in
the session ledger's ``artifacts`` map. Two properties are load-bearing and
each has a dedicated test class:

- **Its bounds are the ledger's bounds.** The constants are asserted equal to
  ``session_ledger``'s own, so a ledger-side change fails here instead of
  silently drifting (the ledger CLAMPS rather than rejects, so drift would be
  invisible at runtime — a too-long value is truncated into corrupt JSON).
- **Rotation never destroys active state.** Terminal entries collapse then
  drop oldest-first; anything the codec cannot PROVE terminal (active, opaque,
  malformed) survives every rotation.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from skill_script_helpers import load_skill_script

from kiro_crew import session_ledger

SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "kiro_crew"
    / "builtin_skills"
    / "goal-conductor"
    / "scripts"
    / "ledger_entry.py"
)


def _mod():
    return load_skill_script("_ledger_entry_under_test", SCRIPT)


def _fields(**overrides):
    fields = {
        "accept": {"kind": "pr_checks", "pr": 123, "repo": "o/r"},
        "session": "dashboard:slot-1",
        "round": 2,
        "status": "running",
        "since": "cursor-17",
    }
    fields.update(overrides)
    return fields


class TestBoundsMirrorTheLedger:
    """The codec's constants ARE the ledger's — drift fails loudly."""

    def test_value_bound_is_the_ledger_text_clamp(self):
        assert _mod().MAX_VALUE_CHARS == session_ledger._MAX_TEXT

    def test_key_bound_is_the_ledger_key_clamp(self):
        # record() clamps artifact keys with _clamp(k, 128); the literal is the
        # contract (session_ledger has no named constant for it).
        assert _mod().MAX_KEY_CHARS == 128

    def test_entry_cap_is_the_ledger_artifacts_cap(self):
        assert _mod().MAX_ENTRIES == session_ledger._MAX_ARTIFACTS


class TestEncodeDecode:
    def test_round_trip(self):
        mod = _mod()
        encoded = mod.mode_encode(_fields())
        assert encoded["ok"] is True
        assert isinstance(encoded["value"], str)
        assert "\n" not in encoded["value"]
        decoded = mod.mode_decode({"value": encoded["value"]})
        assert decoded["ok"] is True
        assert decoded["entry"] == _fields()
        assert decoded["terminal"] is False
        assert decoded["complete"] is True

    def test_encode_is_deterministic(self):
        mod = _mod()
        a = mod.mode_encode(_fields())["value"]
        b = mod.mode_encode(_fields())["value"]
        assert a == b

    def test_encode_since_is_optional(self):
        mod = _mod()
        fields = _fields()
        del fields["since"]
        out = mod.mode_encode(fields)
        assert out["ok"] is True
        assert "since" not in json.loads(out["value"])

    def test_encode_rejects_missing_required_field(self):
        mod = _mod()
        for name in ("accept", "session", "round", "status"):
            fields = _fields()
            del fields[name]
            out = mod.mode_encode(fields)
            assert out["ok"] is False, name
            assert out["error"]["code"] == "missing_field"

    def test_encode_rejects_unknown_field(self):
        """A typo'd field must not silently vanish from the durable entry."""
        out = _mod().mode_encode(_fields(sesion_key="oops"))
        assert out["ok"] is False
        assert out["error"]["code"] == "unknown_field"
        assert "sesion_key" in out["error"]["detail"]

    def test_encode_rejects_bool_round(self):
        """bool is an int subclass; round=true must not encode."""
        out = _mod().mode_encode(_fields(round=True))
        assert out["ok"] is False
        assert out["error"]["code"] == "bad_field_type"

    def test_encode_rejects_unknown_status(self):
        """A synonym would decode as non-terminal and never rotate, filling the
        map toward an unresolvable cap error — refuse it at authoring time."""
        mod = _mod()
        for synonym in ("passed", "done", "complete", "refused", "error"):
            out = mod.mode_encode(_fields(status=synonym))
            assert out["ok"] is False, synonym
            assert out["error"]["code"] == "unknown_status"

    def test_encode_accepts_fails_counter(self):
        """A failed acceptance CHECK keeps the item running with `fails`
        incremented — the count is retry state and must survive round-trips."""
        mod = _mod()
        encoded = mod.mode_encode(_fields(fails=2))
        assert encoded["ok"] is True
        decoded = mod.mode_decode({"value": encoded["value"]})
        assert decoded["ok"] is True
        assert decoded["entry"]["fails"] == 2
        assert decoded["terminal"] is False

    def test_decode_deeply_nested_json_is_a_structured_error(self):
        """Nesting can exceed the parser's recursion limit well inside the
        2000-char bound; a damaged entry must never crash decode."""
        depth = 100_000
        out = _mod().mode_decode({"value": "[" * depth + "]" * depth})
        assert out["ok"] is False
        assert out["error"]["code"] in ("not_json", "not_an_object")

    def test_encode_rejects_wrong_types(self):
        mod = _mod()
        for bad in (
            _fields(accept="not-a-dict"),
            _fields(session=7),
            _fields(round="2"),
            _fields(status=None),
            _fields(since=3),
        ):
            out = mod.mode_encode(bad)
            assert out["ok"] is False
            assert out["error"]["code"] == "bad_field_type"

    def test_encode_refuses_a_value_the_ledger_would_truncate(self):
        """The ledger silently clamps at the cap, corrupting the stored JSON;
        the codec must refuse BEFORE the write instead."""
        mod = _mod()
        out = mod.mode_encode(_fields(accept={"kind": "file", "path": "x" * 2100}))
        assert out["ok"] is False
        assert out["error"]["code"] == "value_too_long"

    def test_decode_rejects_non_string_value(self):
        """The exact constraint behind artifacts_not_string_map."""
        out = _mod().mode_decode({"value": {"accept": {}, "status": "running"}})
        assert out["ok"] is False
        assert out["error"]["code"] == "value_not_string"

    def test_decode_rejects_non_json(self):
        out = _mod().mode_decode({"value": "{not json"})
        assert out["ok"] is False
        assert out["error"]["code"] == "not_json"

    def test_decode_rejects_non_object_json(self):
        out = _mod().mode_decode({"value": "[1, 2]"})
        assert out["ok"] is False
        assert out["error"]["code"] == "not_an_object"

    def test_decode_requires_status(self):
        out = _mod().mode_decode({"value": json.dumps({"round": 2})})
        assert out["ok"] is False
        assert out["error"]["code"] == "missing_field"

    def test_decode_flags_terminal(self):
        mod = _mod()
        for status, terminal in (("pass", True), ("fail", True), ("running", False)):
            out = mod.mode_decode({"value": json.dumps({"round": 1, "status": status})})
            assert out["ok"] is True
            assert out["terminal"] is terminal, status

    def test_decode_collapsed_entry_is_incomplete(self):
        """A rotated one-line outcome decodes fine but is not a full contract."""
        out = _mod().mode_decode({"value": json.dumps({"round": 2, "status": "pass"})})
        assert out["ok"] is True
        assert out["terminal"] is True
        assert out["complete"] is False

    def test_decode_preserves_unknown_fields(self):
        """Forward compatibility: an older codec must not drop a newer field."""
        value = json.dumps({"status": "running", "round": 1, "note": "new-field"})
        out = _mod().mode_decode({"value": value})
        assert out["ok"] is True
        assert out["entry"]["note"] == "new-field"


class TestValidate:
    def test_clean_map_passes(self):
        mod = _mod()
        value = mod.mode_encode(_fields())["value"]
        out = mod.mode_validate({"artifacts": {"item-1": value}})
        assert out == {"ok": True, "violations": []}

    def test_key_too_long(self):
        out = _mod().mode_validate({"artifacts": {"k" * 129: "v"}})
        assert out["ok"] is False
        assert [v["code"] for v in out["violations"]] == ["key_too_long"]

    def test_key_at_the_bound_passes(self):
        out = _mod().mode_validate({"artifacts": {"k" * 128: "v"}})
        assert out["ok"] is True

    def test_value_not_a_string(self):
        out = _mod().mode_validate({"artifacts": {"item-1": {"nested": "object"}}})
        assert out["ok"] is False
        assert [v["code"] for v in out["violations"]] == ["value_not_string"]

    def test_value_too_long(self):
        out = _mod().mode_validate({"artifacts": {"item-1": "x" * 2001}})
        assert out["ok"] is False
        assert [v["code"] for v in out["violations"]] == ["value_too_long"]

    def test_value_at_the_bound_passes(self):
        out = _mod().mode_validate({"artifacts": {"item-1": "x" * 2000}})
        assert out["ok"] is True

    def test_too_many_entries(self):
        arts = {f"item-{i}": "v" for i in range(33)}
        out = _mod().mode_validate({"artifacts": arts})
        assert out["ok"] is False
        assert [v["code"] for v in out["violations"]] == ["too_many_entries"]

    def test_exactly_at_the_entry_cap_passes(self):
        arts = {f"item-{i}": "v" for i in range(32)}
        out = _mod().mode_validate({"artifacts": arts})
        assert out["ok"] is True

    def test_rejects_non_object_artifacts(self):
        out = _mod().mode_validate({"artifacts": ["not", "a", "map"]})
        assert out["ok"] is False
        assert out["error"]["code"] == "bad_field_type"


class TestRotate:
    def _entry(self, mod, status, n):
        return mod.mode_encode(
            {
                "accept": {"kind": "pr_checks", "pr": n},
                "session": f"s-{n}",
                "round": 1,
                "status": status,
            }
        )["value"]

    def test_collapses_terminal_entries(self):
        mod = _mod()
        arts = {
            "item-1": self._entry(mod, "pass", 1),
            "item-2": self._entry(mod, "running", 2),
        }
        out = mod.mode_rotate({"artifacts": arts})
        assert out["ok"] is True
        assert out["collapsed"] == ["item-1"]
        assert out["dropped"] == []
        assert json.loads(out["artifacts"]["item-1"]) == {"round": 1, "status": "pass"}
        # The active entry is untouched, byte for byte.
        assert out["artifacts"]["item-2"] == arts["item-2"]

    def test_rotate_is_idempotent(self):
        mod = _mod()
        arts = {"item-1": self._entry(mod, "fail", 1)}
        once = mod.mode_rotate({"artifacts": arts})
        twice = mod.mode_rotate({"artifacts": once["artifacts"]})
        assert twice["artifacts"] == once["artifacts"]
        assert twice["collapsed"] == []

    def test_drops_oldest_terminal_first_under_the_cap(self):
        mod = _mod()
        arts = {}
        # 3 terminal (oldest) then 31 active = 34 entries, 2 over the cap.
        for i in range(3):
            arts[f"done-{i}"] = self._entry(mod, "pass", i)
        for i in range(31):
            arts[f"live-{i}"] = self._entry(mod, "running", 100 + i)
        out = mod.mode_rotate({"artifacts": arts})
        assert out["ok"] is True
        assert out["dropped"] == ["done-0", "done-1"]
        assert len(out["artifacts"]) == 32
        assert "done-2" in out["artifacts"]
        assert all(f"live-{i}" in out["artifacts"] for i in range(31))

    def test_never_drops_an_active_item(self):
        mod = _mod()
        arts = {f"live-{i}": self._entry(mod, "running", i) for i in range(33)}
        out = mod.mode_rotate({"artifacts": arts})
        assert out["ok"] is False
        assert out["error"]["code"] == "cap_exceeded_all_active"

    def test_preserves_opaque_and_malformed_entries(self):
        """Rotation must never destroy what it cannot prove is finished."""
        mod = _mod()
        arts = {
            "opaque": "not json at all",
            "item-1": self._entry(mod, "pass", 1),
        }
        out = mod.mode_rotate({"artifacts": arts})
        assert out["ok"] is True
        assert out["artifacts"]["opaque"] == "not json at all"

    def test_rotation_preserves_a_failing_but_retrying_item(self):
        """status=running with fails>0 is retry state, not a terminal verdict:
        rotation must keep the full entry (spec, session, cursor) intact."""
        mod = _mod()
        value = mod.mode_encode(
            {
                "accept": {"kind": "pr_checks", "pr": 9},
                "session": "s-9",
                "round": 2,
                "status": "running",
                "fails": 2,
            }
        )["value"]
        out = mod.mode_rotate({"artifacts": {"item-9": value}})
        assert out["ok"] is True
        assert out["collapsed"] == []
        assert out["artifacts"]["item-9"] == value

    def test_pre_dispatch_rotation_of_combined_map_protects_active_items(self):
        """The dispatch-time invariant: rotate the CURRENT map plus the entries
        about to be written, so the codec — not the ledger's status-blind
        age-out — decides what drops. A full map (31 active + 1 old terminal)
        plus one new dispatch must drop the terminal entry, never an active
        one; the result fits the cap so the ledger's own eviction never runs."""
        mod = _mod()
        combined = {"done-old": self._entry(mod, "pass", 0)}
        for i in range(31):
            combined[f"live-{i}"] = self._entry(mod, "running", i)
        combined["item-new"] = self._entry(mod, "running", 99)  # the new dispatch
        assert len(combined) == 33
        out = mod.mode_rotate({"artifacts": combined})
        assert out["ok"] is True
        assert out["dropped"] == ["done-old"]
        assert len(out["artifacts"]) == 32
        assert "item-new" in out["artifacts"]
        assert all(f"live-{i}" in out["artifacts"] for i in range(31))

    def test_malformed_entries_count_as_active_for_the_cap(self):
        mod = _mod()
        arts = {f"opaque-{i}": "not json" for i in range(33)}
        out = mod.mode_rotate({"artifacts": arts})
        assert out["ok"] is False
        assert out["error"]["code"] == "cap_exceeded_all_active"


class TestCli:
    """The script is invoked as ``python3 ledger_entry.py <mode>`` by the
    conductor; the process contract (stdin/stdout/exit codes) is the API."""

    def _run(self, mode, payload):
        return subprocess.run(
            [sys.executable, str(SCRIPT), mode],
            input=json.dumps(payload) if payload is not None else "",
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    def test_encode_decode_round_trip_via_cli(self):
        proc = self._run("encode", _fields())
        assert proc.returncode == 0, proc.stderr
        value = json.loads(proc.stdout)["value"]
        proc = self._run("decode", {"value": value})
        assert proc.returncode == 0
        assert json.loads(proc.stdout)["entry"] == _fields()

    def test_domain_errors_exit_zero(self):
        """A domain problem is a structured result the conductor reads, never a
        nonzero exit that hides it."""
        proc = self._run("decode", {"value": "{broken"})
        assert proc.returncode == 0
        assert json.loads(proc.stdout)["error"]["code"] == "not_json"

    def test_unknown_mode_exits_2(self):
        proc = self._run("frobnicate", {})
        assert proc.returncode == 2
        # The cause must land on stdout — the stream the conductor reads —
        # matching the malformed-stdin error and accept_eval.py.
        assert "usage" in proc.stdout

    def test_json_array_stdin_exits_2_without_traceback(self):
        """A JSON array parses fine but is not an object; the contract is a
        structured exit-2, never a crash inside a mode handler."""
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "rotate"],
            input='["not", "an", "object"]',
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert proc.returncode == 2
        assert "Traceback" not in proc.stderr
        assert json.loads(proc.stdout)["error"] == "stdin must be a JSON object"

    def test_malformed_stdin_exits_2(self):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "encode"],
            input="not json",
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert proc.returncode == 2

    def test_missing_mode_exits_2(self):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT)],
            input="{}",
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert proc.returncode == 2


class TestLedgerAcceptsWhatTheCodecEmits:
    """End-to-end against the real store: an encoded entry survives a real
    ``session_ledger.record`` write and reads back byte-identical."""

    def test_encoded_entry_round_trips_through_the_ledger(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
        mod = _mod()
        value = mod.mode_encode(_fields())["value"]
        session_ledger.record("slot-a", artifacts={"item-1": value})
        state = session_ledger.read_state("slot-a")
        assert state["artifacts"]["item-1"] == value
        decoded = mod.mode_decode({"value": state["artifacts"]["item-1"]})
        assert decoded["ok"] is True
        assert decoded["entry"] == _fields()
