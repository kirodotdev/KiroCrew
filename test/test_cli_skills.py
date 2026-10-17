"""Tests for the read-only ``kirocrew skills`` command group."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import pytest

from kiro_crew import cli, cli_skills, pinned_fs
from kiro_crew import skills as skills_mod
from kiro_crew import skills_candidate_tree
from kiro_crew.skills import AutoSkillProvenance, SkillsLoader


@pytest.fixture(autouse=True)
def _close_loaders(close_skills_loaders):
    """Close every loader a test here builds, including ones the CLI builds."""


@pytest.fixture()
def loader(tmp_path, _close_loaders):
    return SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False)


def _args(action: str, **values) -> argparse.Namespace:
    return argparse.Namespace(skills_action=action, **values)


def _stage(loader: SkillsLoader, slug: str, *, description: str = "pending candidate") -> None:
    loader.stage_skill_candidate(
        slug,
        description=description,
        triggers=slug,
        procedure_md="## Steps\n\nRun it.\n",
        provenance=AutoSkillProvenance(
            session_key="test",
            created_at=AutoSkillProvenance.now_iso(),
        ),
    )


def _use_loader(monkeypatch, loader) -> None:
    monkeypatch.setattr(cli_skills, "SkillsLoader", lambda **_kwargs: loader)


# ── list ──


def test_list_defaults_to_pending_and_json_preserves_metadata(loader, monkeypatch, capsys):
    _stage(loader, "candidate-one")
    _use_loader(monkeypatch, loader)

    cli_skills._skills(_args("list", live=False, all=False, json=True))

    payload = json.loads(capsys.readouterr().out)
    assert [row["slug"] for row in payload["pending"]] == ["candidate-one"]
    assert "live" not in payload


def test_list_all_includes_pending_and_live(loader, monkeypatch, capsys):
    _stage(loader, "candidate-one")
    loader.create_skill("live-one", "---\nname: live-one\ndescription: Live one\n---\n")
    _use_loader(monkeypatch, loader)

    cli_skills._skills(_args("list", live=False, all=True, json=True))

    payload = json.loads(capsys.readouterr().out)
    assert [row["slug"] for row in payload["pending"]] == ["candidate-one"]
    assert any(row["key"] == "live-one" for row in payload["live"])


def test_list_text_sanitizes_all_skill_derived_fields(monkeypatch, capsys):
    class _Loader:
        def list_pending_skills(self):
            return [
                {
                    "slug": "candidate\x1b[31m-one",
                    "kind": "new\nforged",
                    "description": "pending\nforged",
                }
            ]

        def list_skills(self):
            return [{"key": "live\x1b[31m-one", "description": "active\nforged"}]

    _use_loader(monkeypatch, _Loader())

    cli_skills._skills(_args("list", live=False, all=True, json=False))

    out = capsys.readouterr().out
    assert "\x1b" not in out
    assert "PENDING  candidate-one  [new\\x0aforged]  pending\\x0aforged" in out
    assert "LIVE     live-one  active\\x0aforged" in out


def test_list_redacts_credential_shaped_identities_in_text_and_json(monkeypatch, capsys):
    secret = "pypi-" + "a" * 20

    class _Loader:
        def list_pending_skills(self):
            return [{"slug": secret, "description": "pending"}]

        def list_skills(self):
            return [{"key": secret, "description": "live"}]

    _use_loader(monkeypatch, _Loader())

    cli_skills._skills(_args("list", live=False, all=True, json=False))
    out = capsys.readouterr().out
    assert secret not in out
    assert out.count("[REDACTED: credential]") == 2

    cli_skills._skills(_args("list", live=False, all=True, json=True))
    payload = json.loads(capsys.readouterr().out)
    assert payload["pending"][0]["slug"] == "[REDACTED: credential]"
    assert payload["live"][0]["key"] == "[REDACTED: credential]"
    assert secret not in json.dumps(payload)


def test_pending_slug_that_looks_like_a_credential_is_redacted(loader, monkeypatch, capsys):
    _stage(loader, "pypi-publish-checklist")
    _use_loader(monkeypatch, loader)

    cli_skills._skills(_args("list", live=False, all=False, json=True))

    payload = json.loads(capsys.readouterr().out)
    assert [r["slug"] for r in payload["pending"]] == ["[REDACTED: credential]"]


def test_list_caps_rows_per_section_and_prints_the_overflow_count(monkeypatch, capsys):
    monkeypatch.setattr(cli_skills, "_LIST_MAX_ROWS", 3)

    class _Loader:
        def list_pending_skills(self):
            return [{"slug": f"candidate-{i}", "description": "d"} for i in range(5)]

        def list_skills(self):
            return [{"key": f"live-{i}", "description": "d"} for i in range(4)]

    _use_loader(monkeypatch, _Loader())

    cli_skills._skills(_args("list", live=False, all=True, json=False))
    out = capsys.readouterr().out
    assert out.count("PENDING  ") == 3
    assert "[2 more pending candidate(s) not shown]" in out
    assert out.count("LIVE     ") == 3
    assert "[1 more live skill(s) not shown]" in out

    cli_skills._skills(_args("list", live=False, all=True, json=True))
    payload = json.loads(capsys.readouterr().out)
    assert (len(payload["pending"]), payload["pending_omitted"]) == (3, 2)
    assert (len(payload["live"]), payload["live_omitted"]) == (3, 1)


@pytest.mark.parametrize(
    "secret",
    [
        "aws_secret_access_key=" + "a" * 20 + "\x01" + "b" * 20,
        "aws_secret_access_key=" + "a" * 20 + "\u200b" + "b" * 20,
    ],
)
def test_list_redacts_a_credential_split_by_a_control_or_invisible_character(
    secret, monkeypatch, capsys
):
    class _Loader:
        def list_pending_skills(self):
            return [{"slug": "candidate-one", "description": secret}]

    _use_loader(monkeypatch, _Loader())

    cli_skills._skills(_args("list", live=False, all=False, json=False))

    out = capsys.readouterr().out
    assert "[REDACTED: credential]" in out
    assert "a" * 20 not in out


@pytest.mark.parametrize("error", [UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad"), OSError()])
def test_list_read_failures_exit_with_coded_reason(error, monkeypatch, capsys):
    class _Loader:
        def list_pending_skills(self):
            raise error

    _use_loader(monkeypatch, _Loader())

    with pytest.raises(SystemExit) as exc:
        cli_skills._skills(_args("list", live=False, all=False, json=False))

    assert exc.value.code == 1
    code = (
        "invalid_skill_encoding:" if isinstance(error, UnicodeDecodeError) else "skills_unreadable:"
    )
    assert capsys.readouterr().err.startswith(code)


def test_sanitized_keys_that_collide_both_print():
    safe = cli_skills._terminal_value({"run\x01.py": ["x"], "run.py": ["y"]})

    assert safe == {"run.py": ["x"], "run.py (2)": ["y"]}


def test_terminal_line_and_dashboard_scrub_share_one_order():
    from kiro_crew.dashboard.handlers._shared import _scrub_text

    corpus = [
        "aws_secret_access_key=" + "a" * 20 + "\x01" + "b" * 20,
        "ghp_" + "A" * 20 + "\u2063" + "A" * 16,
        "plain text with a soft\u00adhyphen",
    ]
    for sample in corpus:
        assert cli_skills._terminal_line(sample) == cli_skills.safe_terminal_line(
            _scrub_text(sample)
        )


# ── pending metadata on the list path ──


def test_oversized_meta_lists_as_empty_but_approval_still_reads_it(loader):
    _stage(loader, "big-meta")
    meta = loader._pending_root() / "big-meta" / ".meta.json"
    meta.write_text(
        json.dumps({"kind": "update", "target": "live", "pad": "x" * 70_000}), encoding="utf-8"
    )

    rows = loader.list_pending_skills()

    assert [row["slug"] for row in rows] == ["big-meta"]
    assert rows[0]["kind"] == "new"  # the listing declined the oversized file
    # The approve paths are unchanged: they still see the file's real kind.
    assert loader._read_pending_meta("big-meta")["kind"] == "update"


def test_deeply_nested_meta_lists_as_empty(loader):
    _stage(loader, "deep-meta")
    meta = loader._pending_root() / "deep-meta" / ".meta.json"
    meta.write_text("[" * 20_000 + "]" * 20_000, encoding="utf-8")

    rows = loader.list_pending_skills()

    assert rows[0]["name"] == "auto/deep-meta"


def test_hard_linked_meta_is_not_read_by_the_listing(loader, tmp_path):
    if not hasattr(os, "link"):
        pytest.skip("needs hard links")
    _stage(loader, "linked-meta")
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"name": "auto/stolen", "description": "leak"}), encoding="utf-8")
    meta = loader._pending_root() / "linked-meta" / ".meta.json"
    meta.unlink()
    os.link(outside, meta)

    rows = loader.list_pending_skills()

    assert rows[0]["name"] == "auto/linked-meta"
    assert rows[0].get("description") != "leak"


def test_pending_has_scripts_is_true_only_for_json_true(loader):
    _stage(loader, "string-flag")
    meta = loader._pending_root() / "string-flag" / ".meta.json"
    data = json.loads(meta.read_text(encoding="utf-8"))
    data["has_scripts"] = "false"
    meta.write_text(json.dumps(data), encoding="utf-8")

    rows = {row["slug"]: row for row in loader.list_pending_skills()}

    assert rows["string-flag"]["has_scripts"] is False


def test_candidate_without_meta_still_approves_as_new(loader):
    _stage(loader, "no-meta")
    (loader._pending_root() / "no-meta" / ".meta.json").unlink()

    assert loader.approve_pending_skill_checked("no-meta") == "auto/no-meta"


# ── link-free candidate probes ──


def _forbid_by_name_probes(monkeypatch, root: Path) -> None:
    """Fail any ``is_dir``/``exists`` probe below ``root``: those follow links."""
    real_is_dir, real_exists = Path.is_dir, Path.exists

    def guard(real):
        def probe(self, *args, **kwargs):
            if root in self.parents:
                raise AssertionError(f"a pending probe followed {self.name} by name")
            return real(self, *args, **kwargs)

        return probe

    monkeypatch.setattr(Path, "is_dir", guard(real_is_dir))
    monkeypatch.setattr(Path, "exists", guard(real_exists))


def test_pending_listing_never_follows_a_candidate_path_by_name(loader, monkeypatch):
    _stage(loader, "plain-candidate")
    _forbid_by_name_probes(monkeypatch, loader._pending_root())

    rows = loader.list_pending_skills()

    assert [r["slug"] for r in rows] == ["plain-candidate"]


def test_pending_listing_never_reads_through_a_linked_candidate_directory(
    loader, tmp_path, monkeypatch
):
    _stage(loader, "real-candidate")
    outside = tmp_path / "outside-candidate"
    outside.mkdir()
    (outside / "SKILL.md").write_text("---\nname: x\ndescription: x\n---\n")
    (outside / ".meta.json").write_text(json.dumps({"description": "read through the link"}))
    try:
        os.symlink(outside, loader._pending_root() / "linked-candidate", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this host cannot create a directory symlink")
    _forbid_by_name_probes(monkeypatch, loader._pending_root())

    by_slug = {r["slug"]: r for r in loader.list_pending_skills()}

    assert set(by_slug) == {"real-candidate", "linked-candidate"}
    linked = by_slug["linked-candidate"]
    assert linked["description"] == ""
    if "script_validation" in linked:
        assert linked["script_validation"]["ok"] is False


def test_candidate_swapped_for_a_link_after_lstat_is_not_probed_through(
    loader, tmp_path, monkeypatch
):
    if not pinned_fs.supports_pinned_tree_walk():
        pytest.skip("descriptor-relative stat is unavailable on this platform")
    _stage(loader, "swapped-candidate")
    child = loader._pending_root() / "swapped-candidate"
    outside = tmp_path / "outside-candidate"
    outside.mkdir()
    (outside / "SKILL.md").write_text("---\nname: x\ndescription: x\n---\n")
    real_lstat = os.lstat
    swapped = []

    def lstat_then_swap(path, *args, **kwargs):
        result = real_lstat(path, *args, **kwargs)
        if Path(path) == child and not swapped:
            # The swap lands between the directory check and the SKILL.md probe.
            child.rename(tmp_path / "moved-away")
            os.symlink(outside, child, target_is_directory=True)
            swapped.append(True)
        return result

    monkeypatch.setattr(os, "lstat", lstat_then_swap)

    assert skills_candidate_tree._pending_candidate_shape(child) == "link"
    assert swapped


# ── show ──


def test_show_prints_content_metadata_and_validation(loader, monkeypatch, capsys):
    _stage(loader, "candidate-one")
    _use_loader(monkeypatch, loader)

    cli_skills._skills(_args("show", slug="candidate-one"))

    out = capsys.readouterr().out
    assert "--- SKILL.md (untrusted; each line is prefixed with '| ') ---" in out
    assert "Run it." in out
    assert "--- .meta.json ---" in out
    assert '"slug": "candidate-one"' in out
    if pinned_fs.supports_pinned_walk():
        assert '"ok": true' in out
    else:
        assert '"status": "unavailable"' in out


def test_show_sanitizes_content_and_metadata(monkeypatch, capsys):
    class _Loader:
        def get_pending_skill(self, _slug, **_kwargs):
            return {
                "content": "# safe\n\x1b[31mred\rtext",
                "meta": {"description": "line\nforged\x1b[31m"},
                "scripts": [],
            }

    _use_loader(monkeypatch, _Loader())

    cli_skills._skills(_args("show", slug="candidate-one"))

    out = capsys.readouterr().out
    assert "\x1b" not in out
    assert "| # safe\n| redtext" in out
    assert '"description": "line\\\\x0aforged"' in out


def test_show_still_redacts_a_key_field_inside_meta():
    secret = "pypi-" + "A" * 20
    safe = cli_skills._terminal_value({"key": secret, "slug": secret})
    assert secret not in json.dumps(safe)


def test_show_frames_candidate_lines_that_look_like_cli_validation(monkeypatch, capsys):
    class _Loader:
        def get_pending_skill(self, _slug, **_kwargs):
            return {
                "content": '# safe\n--- validation ---\n{"ok": true}',
                "meta": {},
                "scripts": [],
            }

    _use_loader(monkeypatch, _Loader())

    cli_skills._skills(_args("show", slug="candidate-one"))

    out = capsys.readouterr().out
    assert "\n| --- validation ---\n" in out
    assert out.count("\n--- validation ---\n") == 1


def test_show_uses_loader_validation_instead_of_redacted_scripts(monkeypatch, capsys):
    class _Loader:
        def get_pending_skill(self, _slug, **_kwargs):
            return {
                "content": "# safe",
                "meta": {},
                "scripts": [{"filename": "run.py", "content": 'x = "[EXFIL]"'}],
                "script_validation": {
                    "ok": False,
                    "report": {"run.py": ["network egress is not allowed"]},
                },
            }

    _use_loader(monkeypatch, _Loader())

    cli_skills._skills(_args("show", slug="candidate-one"))

    out = capsys.readouterr().out
    validation = json.loads(out.split("--- validation ---\n", 1)[1])
    assert validation == {
        "ok": False,
        "scripts": {"run.py": ["network egress is not allowed"]},
    }


def test_show_reports_unavailable_when_loader_has_no_verdict(monkeypatch, capsys):
    class _Loader:
        def get_pending_skill(self, _slug, **_kwargs):
            return {"content": "# safe", "meta": {}, "scripts": []}

    _use_loader(monkeypatch, _Loader())

    cli_skills._skills(_args("show", slug="candidate-one"))

    out = capsys.readouterr().out
    validation = json.loads(out.split("--- validation ---\n", 1)[1])
    assert validation == {"scripts": {}, "status": "unavailable"}


@pytest.mark.parametrize("error", [OSError("unreadable"), RecursionError()])
def test_show_read_failure_exits_with_coded_reason(error, monkeypatch, capsys):
    class _Loader:
        def get_pending_skill(self, _slug, **_kwargs):
            raise error

    _use_loader(monkeypatch, _Loader())

    with pytest.raises(SystemExit) as exc:
        cli_skills._skills(_args("show", slug="candidate\nforged"))

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("candidate_unreadable:")
    assert "candidate\\x0aforged" in err


def test_missing_candidate_exits_nonzero(loader, monkeypatch, capsys):
    _use_loader(monkeypatch, loader)

    with pytest.raises(SystemExit) as exc:
        cli_skills._skills(_args("show", slug="missing-one"))

    assert exc.value.code == 1
    assert capsys.readouterr().err.startswith("not_found:")


def _hard_link_helper(loader, slug, tmp_path):
    outside = tmp_path / "outside.py"
    outside.write_text("print('hidden')\n", encoding="utf-8")
    script = loader._pending_root() / slug / "scripts" / "run.py"
    script.parent.mkdir()
    os.link(outside, script)


def _bad_helper(loader, slug, _tmp_path):
    script = loader._pending_root() / slug / "scripts" / "run.py"
    script.parent.mkdir()
    script.write_bytes(b"\xff")


def _bad_body(loader, slug, _tmp_path):
    (loader._pending_root() / slug / "SKILL.md").write_bytes(b"\xff")


def _huge_body(loader, slug, _tmp_path):
    (loader._pending_root() / slug / "SKILL.md").write_text(
        "hidden\n" * (skills_mod._PENDING_DOCUMENT_MAX_BYTES // 7 + 10), encoding="utf-8"
    )


@pytest.mark.parametrize("plant", [_hard_link_helper, _bad_helper, _bad_body, _huge_body])
def test_show_refuses_a_candidate_it_cannot_show_whole(
    plant, loader, monkeypatch, capsys, tmp_path
):
    if plant is _hard_link_helper and not hasattr(os, "link"):
        pytest.skip("needs hard links")
    _stage(loader, "cannot-show")
    plant(loader, "cannot-show", tmp_path)
    _use_loader(monkeypatch, loader)

    with pytest.raises(SystemExit) as exc:
        cli_skills._skills(_args("show", slug="cannot-show"))

    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("candidate_unreadable:")
    assert "hidden" not in captured.out
    assert captured.out == ""


def test_show_reports_metadata_nested_too_deeply_without_a_traceback(loader, monkeypatch, capsys):
    _stage(loader, "deep-json")
    _use_loader(monkeypatch, loader)

    def deep(_value):
        raise RecursionError

    monkeypatch.setattr(cli_skills, "_terminal_value", deep)

    with pytest.raises(SystemExit) as exc:
        cli_skills._skills(_args("show", slug="deep-json"))

    captured = capsys.readouterr()
    assert exc.value.code == 1
    assert captured.err.startswith("candidate_unreadable:")
    assert "--- SKILL.md" not in captured.out


def test_show_does_not_follow_linked_skill_md_by_name(loader, tmp_path, monkeypatch, capsys):
    _stage(loader, "linked-show")
    skill_file = loader._pending_root() / "linked-show" / "SKILL.md"
    outside = tmp_path / "outside.md"
    outside.write_text("outside marker\n", encoding="utf-8")
    skill_file.unlink()
    try:
        os.symlink(outside, skill_file)
    except (OSError, NotImplementedError):
        pytest.skip("this host cannot create a file symlink")

    real_exists = Path.exists
    real_stat = Path.stat

    def forbid_exists(path, *args, **kwargs):
        if path == skill_file:
            raise AssertionError("skills show followed SKILL.md through Path.exists")
        return real_exists(path, *args, **kwargs)

    def forbid_stat(path, *args, **kwargs):
        if path == skill_file:
            raise AssertionError("skills show followed SKILL.md through Path.stat")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "exists", forbid_exists)
    monkeypatch.setattr(Path, "stat", forbid_stat)
    _use_loader(monkeypatch, loader)

    with pytest.raises(SystemExit) as exc:
        cli_skills._skills(_args("show", slug="linked-show"))

    assert exc.value.code == 1
    assert capsys.readouterr().err.startswith("candidate_unreadable:")


# ── scope: nothing here changes a skill ──


@pytest.mark.parametrize("action", ["approve", "dismiss"])
def test_parser_does_not_expose_skill_mutations(action, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["kirocrew", "skills", action, "candidate-one"])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_parser_does_not_expose_redundant_pending_flag(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["kirocrew", "skills", "list", "--pending"])

    with pytest.raises(SystemExit) as exc:
        cli.main()

    assert exc.value.code == 2
    assert "unrecognized arguments: --pending" in capsys.readouterr().err


def test_list_and_show_leave_the_pending_queue_untouched(loader, monkeypatch, capsys):
    _stage(loader, "stays-put")
    _use_loader(monkeypatch, loader)
    before = sorted(p.name for p in (loader._pending_root() / "stays-put").iterdir())

    cli_skills._skills(_args("list", live=False, all=True, json=True))
    cli_skills._skills(_args("show", slug="stays-put"))
    capsys.readouterr()

    assert sorted(p.name for p in (loader._pending_root() / "stays-put").iterdir()) == before
    assert not (loader._dir / "auto" / "stays-put").exists()
