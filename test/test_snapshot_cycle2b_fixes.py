"""Tests for the cycle-2 review finding: a partial root must carry a component map.

The `kirocrew-partial-` root exists so released versions refuse a selective bundle.
This version accepts it -- and that acceptance created a new hole: `declared is None`
falls through to all-components behaviour, which is correct for a pre-v3 COMPLETE
archive and exactly wrong for a partial one.
"""

from __future__ import annotations

import json
import shutil
import tarfile

import pytest
from test_snapshot import _setup_fake_kirocrew

from kiro_crew import snapshot as snap


@pytest.fixture
def home(tmp_path, monkeypatch):
    d = tmp_path / "home"
    d.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(d))
    _setup_fake_kirocrew(d)
    return d


def _archive(tmp_path, roots: dict[str, dict | None]) -> "object":
    """Build an archive with the given roots. Value is the manifest, or None for none."""
    stage = tmp_path / "stage"
    stage.mkdir(exist_ok=True)
    for name, manifest in roots.items():
        root = stage / name
        (root / "workspace" / "memory").mkdir(parents=True, exist_ok=True)
        (root / "workspace" / "memory" / "preferences.md").write_text("from the bundle")
        if manifest is not None:
            (root / "MANIFEST.json").write_text(json.dumps(manifest))
    out = tmp_path / "bundle.tar.gz"
    with tarfile.open(out, "w:gz") as tf:
        for name in roots:
            tf.add(str(stage / name), arcname=name)
    return out


class TestAPartialRootWithoutAComponentMapIsRefused:
    def test_it_refuses_rather_than_restoring_everything(self, home, tmp_path, capsys):
        bundle = _archive(tmp_path, {"kirocrew-partial-20260101T000000Z": None})
        rc = snap.restore_main([str(bundle), "--mode", "replace", "--force"])
        assert rc == 1
        out = capsys.readouterr().out
        assert "marked partial but carries no component map" in out

    def test_the_live_tree_is_untouched_by_that_refusal(self, home, tmp_path):
        md = home / "workspace" / "memory"
        (md / "preferences.md").write_text("live state")
        bundle = _archive(tmp_path, {"kirocrew-partial-20260101T000000Z": None})
        assert snap.restore_main([str(bundle), "--mode", "replace", "--force"]) == 1
        assert (md / "preferences.md").read_text() == "live state"

    def test_an_explicit_components_flag_still_lets_it_through(self, home, tmp_path):
        """The refusal is about guessing, not about the bundle being unusable.

        Replace is the destructive direction, so the hatch is shown here on a live home
        with nothing to lose: the bundle omits `workspace/knowledge` and so does live
        state, and a tree neither side has cannot be cleared out from under anyone. The
        case where live DOES hold a tree the bundle omits is refused, and merge -- which
        clears nothing -- accepts it either way; both are pinned in cycle12.
        """
        md = home / "workspace" / "memory"
        (md / "preferences.md").write_text("live state")
        shutil.rmtree(home / "workspace" / "knowledge", ignore_errors=True)
        bundle = _archive(tmp_path, {"kirocrew-partial-20260101T000000Z": None})
        rc = snap.restore_main(
            [str(bundle), "--mode", "replace", "--force", "--components", "memory"]
        )
        assert rc == 0
        assert (md / "preferences.md").read_text() == "from the bundle"


def _archive_without_memory(tmp_path, name: str) -> "object":
    """A partial root with no component map that carries only `crons.json`."""
    stage = tmp_path / "stage-nomem"
    stage.mkdir(exist_ok=True)
    root = stage / name
    root.mkdir(parents=True, exist_ok=True)
    (root / "crons.json").write_text(json.dumps({"jobs": []}))
    out = tmp_path / "bundle-nomem.tar.gz"
    with tarfile.open(out, "w:gz") as tf:
        tf.add(str(root), arcname=name)
    return out


class TestANamedComponentTheBundleDoesNotCarryIsRefused:
    """The escape hatch is honoured by checking the archive, not by trusting the list.

    Naming a component is an assertion, not evidence. Replace mode moves each live
    core file aside before it knows whether the archive has a replacement, and clears
    a component tree whether or not the archive carries one -- so without this check
    the operator loses the component and is told the restore succeeded.
    """

    def test_it_refuses_instead_of_reporting_success(self, home, tmp_path, capsys):
        bundle = _archive_without_memory(tmp_path, "kirocrew-partial-20260101T000000Z")
        rc = snap.restore_main(
            [str(bundle), "--mode", "replace", "--force", "--components", "memory"]
        )
        assert rc == 1
        out = capsys.readouterr().out
        assert "does not contain: memory" in out
        assert "carries no component map" in out

    def test_the_live_databases_and_trees_survive(self, home, tmp_path):
        (home / "workspace" / "memory" / "preferences.md").write_text("live state")
        bundle = _archive_without_memory(tmp_path, "kirocrew-partial-20260101T000000Z")
        assert (
            snap.restore_main(
                [str(bundle), "--mode", "replace", "--force", "--components", "memory"]
            )
            == 1
        )
        assert (home / "memory.db").is_file()
        assert (home / "workspace" / "memory" / "preferences.md").read_text() == "live state"
        assert (home / "workspace" / "knowledge").is_dir()

    def test_nothing_is_moved_into_a_rollback_directory(self, home, tmp_path):
        bundle = _archive_without_memory(tmp_path, "kirocrew-partial-20260101T000000Z")
        snap.restore_main([str(bundle), "--mode", "replace", "--force", "--components", "memory"])
        assert list(home.glob("pre-restore-*")) == []

    def test_the_refusal_is_audited_with_its_own_reason(self, home, tmp_path, monkeypatch):
        """A refusal that leaves no security event is one nobody can review later.

        The reason is asserted, not just the presence of an event: every rejection on
        this path audits, so a shared or wrong reason makes them indistinguishable.
        """
        events: list[tuple[str, str]] = []
        monkeypatch.setattr(snap, "_audit", lambda et, res: events.append((et, res)))
        bundle = _archive_without_memory(tmp_path, "kirocrew-partial-20260101T000000Z")
        snap.restore_main([str(bundle), "--mode", "replace", "--force", "--components", "memory"])
        assert any(
            et == "state_restore_rejected" and "reason=named_component_absent" in res
            for et, res in events
        ), events

    def test_a_component_present_only_as_a_file_counts_as_carried(self, home, tmp_path):
        """Presence is ANY declared path, not all of them.

        `crons` declares one file and no trees, so the bundle above carries it. A
        rule demanding every declared path would refuse a sound bundle -- a home
        that never wrote `memory_index.db` ships memory with only `memory.db`.
        """
        bundle = _archive_without_memory(tmp_path, "kirocrew-partial-20260101T000000Z")
        rc = snap.restore_main(
            [str(bundle), "--mode", "replace", "--force", "--components", "crons"]
        )
        assert rc == 0

    def test_a_component_present_only_as_a_tree_counts_as_carried(self, home, tmp_path):
        """`_archive` writes workspace/memory but neither memory database."""
        bundle = _archive(tmp_path, {"kirocrew-partial-20260101T000000Z": None})
        assert (
            snap._components_absent_from_bundle(_extract_root(bundle, tmp_path), ["memory"]) == []
        )

    def test_a_declared_map_still_decides_when_there_is_one(self, home, tmp_path):
        """This check is the fallback for a missing map, and must not shadow it.

        The manifest is authoritative when present: a bundle whose map omits a
        component is refused on the map even though the archive happens to hold the
        files, because the map is what the bundle says it carries.
        """
        bundle = _archive(
            tmp_path,
            {
                "kirocrew-partial-20260101T000000Z": {
                    "version": 3,
                    "components": {"crons": "unresolved"},
                }
            },
        )
        rc = snap.restore_main(
            [str(bundle), "--mode", "replace", "--force", "--components", "memory"]
        )
        assert rc == 1


def _extract_root(bundle, tmp_path):
    """Return the bundle's single root directory, extracted."""
    dest = tmp_path / "extracted"
    dest.mkdir(exist_ok=True)
    with tarfile.open(bundle) as tf:
        tf.extractall(dest, filter=snap._data_filter)  # nosec B202
    return next(d for d in dest.iterdir() if d.is_dir())

    def test_a_partial_root_WITH_a_map_is_accepted(self, home, tmp_path):
        bundle = _archive(
            tmp_path,
            {
                "kirocrew-partial-20260101T000000Z": {
                    "version": 3,
                    "components": {"memory": "UNRESOLVED"},
                }
            },
        )
        md = home / "workspace" / "memory"
        (md / "preferences.md").write_text("live state")
        assert snap.restore_main([str(bundle), "--mode", "replace", "--force"]) == 0
        assert (md / "preferences.md").read_text() == "from the bundle"

    def test_a_complete_root_without_a_map_is_still_accepted(self, home, tmp_path):
        """Pre-v3 complete archives legitimately have no map, and for them
        all-components is the correct reading."""
        bundle = _archive(tmp_path, {"kirocrew-snapshot-20260101T000000Z": None})
        assert snap.restore_main([str(bundle), "--mode", "replace", "--force"]) == 0


class TestTwoRootsAreRefusedRatherThanGuessed:
    def test_more_than_one_root_refuses(self, home, tmp_path, capsys):
        bundle = _archive(
            tmp_path,
            {
                "kirocrew-snapshot-20260101T000000Z": {"version": 3, "components": {}},
                "kirocrew-partial-20260101T000001Z": {"version": 3, "components": {}},
            },
        )
        rc = snap.restore_main([str(bundle), "--mode", "replace", "--force"])
        assert rc == 1
        assert "more than one snapshot root" in capsys.readouterr().out

    def test_the_refusal_names_both_roots(self, home, tmp_path, capsys):
        bundle = _archive(
            tmp_path,
            {
                "kirocrew-snapshot-20260101T000000Z": {"version": 3, "components": {}},
                "kirocrew-partial-20260101T000001Z": {"version": 3, "components": {}},
            },
        )
        snap.restore_main([str(bundle), "--mode", "replace", "--force"])
        out = capsys.readouterr().out
        assert "kirocrew-snapshot-20260101T000000Z" in out
        assert "kirocrew-partial-20260101T000001Z" in out


class TestTheAuthorizationDoesNotClaimToBeAgentProof:
    """The keystone floor is a policy layer, not an OS boundary: it gates the agent's
    file tools and command lines naming the path, and neither reads a program's body.
    An agent running a script by path defeats both. The gate is still worth having --
    it makes an agent-provisioned destination require deliberate circumvention rather
    than being the default outcome -- but the docs, the docstring and the two refusal
    messages must not promise a guarantee this design does not deliver. Overstating it
    is what these tests exist to prevent; the claim was made and had to be withdrawn.
    """

    OVERSTATEMENTS = (
        "agent cannot create",
        "cannot create the file",
        "nothing the agent drives can",
        "agent cannot redirect",
        # A phrasing that survived the first version of this ratchet: the claim was
        # withdrawn everywhere the ban list named, and one docstring restated it in
        # different words. Ban the CLAIM, in the spellings it actually appears in, not
        # one sentence.
        "agent-driven caller cannot",
        "an agent cannot",
        "agent is unable to create",
        "the operator can create this file and",
    )

    def _sources(self):
        import pathlib

        import kiro_crew.backup_cli as bc
        import kiro_crew.snapshot_remote as sr

        root = pathlib.Path(sr.__file__).parent
        # encoding is REQUIRED, not decorative: these sources carry em-dashes, so a
        # locale-codepage read raises UnicodeDecodeError on a Windows runner.
        return {
            "snapshot_remote.py": pathlib.Path(sr.__file__).read_text(encoding="utf-8"),
            "backup_cli.py": pathlib.Path(bc.__file__).read_text(encoding="utf-8"),
            "snapshot-and-restore.md": (root / "docs" / "snapshot-and-restore.md").read_text(
                encoding="utf-8"
            ),
        }

    # The one place these words appear legitimately: the documented limitation states
    # the claim in order to DENY it ("not a control an agent cannot defeat"). A
    # substring ban cannot tell an assertion from its negation, and the sibling test
    # REQUIRES that sentence, so it is removed before scanning rather than weakening
    # the ban -- the broad spellings are what caught a docstring that restated the
    # withdrawn claim in words the first ban list did not name.
    NEGATED = ("a control an agent cannot defeat",)

    def test_no_source_claims_the_agent_is_unable_to_authorize(self):
        for name, text in self._sources().items():
            lowered = text.lower()
            for allowed in self.NEGATED:
                lowered = lowered.replace(allowed, "")
            for phrase in self.OVERSTATEMENTS:
                assert phrase not in lowered, (
                    f"{name} claims '{phrase}'. The keystone floor does not establish "
                    "that: a script run by path defeats both policy layers. Say what "
                    "the gate records (an operator's choice), not what it prevents."
                )

    def test_the_docs_state_the_limitation_explicitly(self):
        doc = self._sources()["snapshot-and-restore.md"]
        # Substring only, so markdown emphasis and line wrapping cannot break it.
        assert "a control an agent cannot defeat" in doc
        assert "PreToolUse" in doc, "the doc should name what would make it true"
        assert "do not enable this feature" in doc, (
            "the doc should tell a reader whose threat model includes a determined "
            "agent what to actually do"
        )

    def test_the_refusal_says_what_the_file_records(self, home, monkeypatch):
        """The operator-facing text has to be honest too, not just the comments."""
        import kiro_crew.snapshot_remote as sr

        with pytest.raises(sr.DestinationError) as e:
            sr.consume_authorization("123456789012", "us-west-2", "b")
        msg = str(e.value).lower()
        assert "not a control that an agent is unable to defeat" in msg
        for phrase in self.OVERSTATEMENTS:
            assert phrase not in msg


class TestTextIOPinsUtf8:
    """JSON is UTF-8 by specification. A `read_text()` with no encoding decodes with
    the platform's locale codepage, so on a Windows host an authorization file holding
    any non-ASCII byte is refused as unreadable instead of honoured. This is not
    hypothetical: the encoding-less form shipped here and reddened two Windows shards.
    Asserted on the source so the check runs on every platform, not only the one that
    would fail.
    """

    def test_no_text_io_in_the_backup_path_omits_an_encoding(self):
        """Checked with the AST, not a regex: the call can span lines and its
        arguments can contain nested calls with their own parentheses, both of
        which defeat line-oriented matching. A regex version of this test let a
        `write_text(payload)` mutant survive.
        """
        import ast
        import pathlib

        import kiro_crew.backup_cli as bc
        import kiro_crew.snapshot as sn
        import kiro_crew.snapshot_remote as sr

        offenders = []
        for mod in (sr, bc, sn):
            path = pathlib.Path(mod.__file__)
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                if not isinstance(fn, ast.Attribute):
                    continue
                if fn.attr not in ("read_text", "write_text"):
                    continue
                if any(kw.arg == "encoding" for kw in node.keywords):
                    continue
                offenders.append(f"{path.name}:{node.lineno} .{fn.attr}()")

        assert not offenders, (
            "text I/O without an explicit encoding: "
            + ", ".join(offenders)
            + ". Pin encoding='utf-8' — the default is the platform locale codepage, "
            "so these break on a Windows host for any non-ASCII content."
        )

    def test_the_authorization_reader_accepts_a_utf8_file(self, tmp_path, monkeypatch):
        """The behaviour the encoding exists for: a non-ASCII byte must not make a
        legitimate authorization unreadable."""
        import json

        import kiro_crew.snapshot_remote as sr

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        token = sr.authorization_token_path()
        token.parent.mkdir(parents=True, exist_ok=True)
        token.write_text(
            json.dumps(
                {"account": "123456789012", "region": "us-west-2", "note": "café ✅"},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        # Matching destination -> consumed, not refused.
        sr.consume_authorization("123456789012", "us-west-2", "some-bucket")
        assert not token.exists(), "a matching authorization is spent on success"
