"""Tests for the read-only ``kirocrew skills`` command group."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import pytest

from kiro_crew import cli, cli_commands, pinned_fs
from kiro_crew import skills as skills_mod
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
    monkeypatch.setattr(cli_commands, "SkillsLoader", lambda **_kwargs: loader)


def test_list_defaults_to_pending_and_json_preserves_metadata(loader, monkeypatch, capsys):
    _stage(loader, "candidate-one")
    _use_loader(monkeypatch, loader)

    cli_commands._skills(_args("list", live=False, all=False, json=True))

    payload = json.loads(capsys.readouterr().out)
    assert [row["slug"] for row in payload["pending"]] == ["candidate-one"]
    assert "live" not in payload


def test_list_all_includes_pending_and_live(loader, monkeypatch, capsys):
    _stage(loader, "candidate-one")
    loader.create_skill("live-one", "---\nname: live-one\ndescription: Live one\n---\n")
    _use_loader(monkeypatch, loader)

    cli_commands._skills(_args("list", live=False, all=True, json=True))

    payload = json.loads(capsys.readouterr().out)
    assert [row["slug"] for row in payload["pending"]] == ["candidate-one"]
    assert any(row["key"] == "live-one" for row in payload["live"])


def test_list_text_sanitizes_all_skill_derived_fields(monkeypatch, capsys):
    class _Loader:
        def list_pending_skills_bounded(self):
            return [
                {
                    "slug": "candidate\x1b[31m-one",
                    "kind": "new\nforged",
                    "description": "pending\nforged",
                }
            ], 0

        def list_skills_bounded(self):
            return [{"key": "live\x1b[31m-one", "description": "active\nforged"}], 0

        def catalog_status(self):
            return "complete"

    _use_loader(monkeypatch, _Loader())

    cli_commands._skills(_args("list", live=False, all=True, json=False))

    out = capsys.readouterr().out
    assert "\x1b" not in out
    assert "PENDING  candidate-one  [new\\x0aforged]  pending\\x0aforged" in out
    assert "LIVE     live-one  active\\x0aforged" in out


def test_list_redacts_credential_shaped_identities_in_text_and_json(monkeypatch, capsys):
    secret = "pypi-" + "a" * 20

    class _Loader:
        def list_pending_skills_bounded(self):
            return [{"slug": secret, "description": "pending"}], 0

        def list_skills_bounded(self):
            return [{"key": secret, "description": "live"}], 0

        def catalog_status(self):
            return "complete"

    _use_loader(monkeypatch, _Loader())

    cli_commands._skills(_args("list", live=False, all=True, json=False))
    out = capsys.readouterr().out
    assert secret not in out
    assert out.count("[REDACTED: credential]") == 2

    cli_commands._skills(_args("list", live=False, all=True, json=True))
    payload = json.loads(capsys.readouterr().out)
    assert payload["pending"][0]["slug"] == "[REDACTED: credential]"
    assert payload["live"][0]["key"] == "[REDACTED: credential]"
    assert secret not in json.dumps(payload)


def test_show_still_redacts_a_key_field_inside_meta():
    secret = "pypi-" + "A" * 20
    safe = cli_commands._skills_terminal_value({"key": secret, "slug": secret})
    assert secret not in json.dumps(safe)


def test_list_json_sanitizes_skill_derived_fields(monkeypatch, capsys):
    class _Loader:
        def list_pending_skills_bounded(self):
            return [{"slug": "candidate\x1b[31m-one", "description": "line\nforged"}], 0

    _use_loader(monkeypatch, _Loader())

    cli_commands._skills(_args("list", live=False, all=False, json=True))

    payload = json.loads(capsys.readouterr().out)
    assert payload["pending"] == [{"slug": "candidate-one", "description": "line\\x0aforged"}]


def test_list_prints_the_overflow_count(monkeypatch, capsys):
    rows = [{"slug": f"candidate-{index}", "description": "d"} for index in range(5)]

    class _Loader:
        def list_pending_skills_bounded(self):
            return list(rows), 2

    _use_loader(monkeypatch, _Loader())

    cli_commands._skills(_args("list", live=False, all=False, json=False))

    out = capsys.readouterr().out
    assert out.count("PENDING  ") == 5
    assert "[2 more pending candidate(s) not shown]" in out


def test_bounded_pending_list_reads_only_admitted_candidates(loader, monkeypatch):
    for name in ("c-cand", "a-cand", "e-cand", "b-cand", "d-cand"):
        _stage(loader, name)
    read: list[str] = []
    original = loader._pending_entry

    def spy(child):
        read.append(child.name)
        return original(child)

    monkeypatch.setattr(loader, "_pending_entry", spy)
    monkeypatch.setattr(skills_mod, "_LIST_MAX_ROWS", 2)

    rows, omitted = loader.list_pending_skills_bounded()

    assert [row["slug"] for row in rows] == ["a-cand", "b-cand"]
    assert omitted == 3
    assert read == ["a-cand", "b-cand"]


def test_bounded_live_list_builds_only_admitted_rows(loader, monkeypatch):
    for name in ("live-a", "live-b", "live-c"):
        loader.create_skill(name, f"---\nname: {name}\ndescription: {name}\n---\n")
    built: list[int] = []
    original = loader.list_skills

    def spy(project_dir=None, *, _entries=None, **kwargs):
        built.append(len(_entries))
        return original(project_dir, _entries=_entries, **kwargs)

    monkeypatch.setattr(loader, "list_skills", spy)
    total = len(loader._iter_visible(None))
    monkeypatch.setattr(skills_mod, "_LIST_MAX_ROWS", 1)

    rows, omitted = loader.list_skills_bounded()

    assert len(rows) == 1
    assert built == [1]
    assert omitted == total - 1


def test_bounded_live_list_reads_only_admitted_metadata_rows(loader, monkeypatch):
    for name in ("live-a", "live-b", "live-c"):
        loader.create_skill(name, f"---\nname: {name}\ndescription: {name}\n---\n")
    loader.list_skills()  # warm the metadata table with every live row
    index = loader._search_index
    if index is None:
        pytest.skip("search index unavailable on this host")
    requested: list[list[str] | None] = []
    original = index.metadata_snapshot

    def spy(paths=None):
        requested.append(None if paths is None else list(paths))
        return original(paths)

    monkeypatch.setattr(index, "metadata_snapshot", spy)
    monkeypatch.setattr(skills_mod, "_LIST_MAX_ROWS", 1)

    rows, _ = loader.list_skills_bounded()

    assert len(rows) == 1
    assert requested and all(paths is not None and len(paths) == 1 for paths in requested)


def test_bounded_live_list_treats_oversized_cached_metadata_as_a_miss(loader, monkeypatch):
    import sqlite3
    from contextlib import closing

    from kiro_crew import skill_search_index as skill_search_index_module

    loader.create_skill("live-a", "---\nname: live-a\ndescription: trusted\n---\n")
    loader.list_skills()
    index = loader._search_index
    if index is None:
        pytest.skip("search index unavailable on this host")
    poison = json.dumps({"_catalog_key": "live-a", "name": "poison", "description": "x" * 200})
    with closing(sqlite3.connect(loader._dir.parent / "skill_search_index.sqlite3")) as db:
        with db:
            db.execute("UPDATE skill_metadata SET metadata = ?", (poison,))
    monkeypatch.setattr(skill_search_index_module, "LISTING_ROW_MAX_FILE_BYTES", 100, raising=False)
    real_loads = skill_search_index_module.json.loads

    def reject_poison(raw, *args, **kwargs):
        assert raw != poison, "oversized cached metadata reached json.loads"
        return real_loads(raw, *args, **kwargs)

    monkeypatch.setattr(skill_search_index_module.json, "loads", reject_poison)

    rows, omitted = loader.list_skills_bounded()

    assert omitted == 0
    assert {row["key"]: row.get("description") for row in rows}["live-a"] == "trusted"


@pytest.mark.parametrize("missing_index", [False, True])
def test_bounded_live_list_uses_published_cache_when_index_unavailable(
    loader, monkeypatch, missing_index
):
    for name in ("live-a", "live-b", "live-c"):
        loader.create_skill(name, f"---\nname: {name}\ndescription: {name}\n---\n")
    total = len(loader.list_skills())
    index = loader._search_index
    if index is None:
        pytest.skip("search index unavailable on this host")
    if missing_index:
        loader._search_index = None
    else:
        monkeypatch.setattr(index, "_db", lambda: None)
    monkeypatch.setattr(skills_mod, "_LIST_MAX_ROWS", 1)

    rows, omitted = loader.list_skills_bounded()

    assert len(rows) == 1
    assert omitted == total - 1
    assert loader.catalog_status() == "complete"


def test_show_refuses_deeply_nested_meta(loader, monkeypatch):
    _stage(loader, "deep-show")
    meta = loader._pending_root() / "deep-show" / ".meta.json"
    # Small enough to pass the per-file byte cap. Whether this depth raises
    # depends on the interpreter's parser, so force the RecursionError.
    deep = "[" * 1500 + "]" * 1500
    meta.write_text(deep, encoding="utf-8")
    real_loads = skills_mod.json.loads

    def loads(text, *args, **kwargs):
        if text == deep:
            raise RecursionError("maximum recursion depth exceeded")
        return real_loads(text, *args, **kwargs)

    monkeypatch.setattr(skills_mod.json, "loads", loads)

    assert loader.get_pending_skill("deep-show") is None


def test_oversized_meta_reads_as_empty(loader):
    _stage(loader, "big-meta")
    meta = loader._pending_root() / "big-meta" / ".meta.json"
    meta.write_text(json.dumps({"name": "x" * 70_000}), encoding="utf-8")

    rows = loader.list_pending_skills()

    assert [row["slug"] for row in rows] == ["big-meta"]
    assert rows[0]["name"] == "auto/big-meta"


def test_deeply_nested_meta_reads_as_empty(loader):
    _stage(loader, "deep-meta")
    meta = loader._pending_root() / "deep-meta" / ".meta.json"
    meta.write_text("[" * 20_000 + "]" * 20_000, encoding="utf-8")

    rows = loader.list_pending_skills()

    assert rows[0]["name"] == "auto/deep-meta"


def test_hard_linked_meta_is_not_read(loader, tmp_path):
    _stage(loader, "linked-meta")
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"name": "auto/stolen", "description": "leak"}), encoding="utf-8")
    meta = loader._pending_root() / "linked-meta" / ".meta.json"
    meta.unlink()
    os.link(outside, meta)

    rows = loader.list_pending_skills()

    assert rows[0]["name"] == "auto/linked-meta"
    assert rows[0].get("description") != "leak"


def test_list_redacts_credential_reassembled_by_control_stripping(monkeypatch, capsys):
    secret = "aws_secret_access_key=" + "a" * 20 + "\x01" + "b" * 20

    class _Loader:
        def list_pending_skills_bounded(self):
            return [{"slug": "candidate-one", "description": secret}], 0

    _use_loader(monkeypatch, _Loader())

    cli_commands._skills(_args("list", live=False, all=False, json=False))

    out = capsys.readouterr().out
    assert "[REDACTED: credential]" in out
    assert "a" * 20 + "b" * 20 not in out


@pytest.mark.parametrize("error", [UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad"), OSError()])
def test_list_read_failures_exit_with_coded_reason(error, monkeypatch, capsys):
    class _Loader:
        def list_pending_skills_bounded(self):
            raise error

    _use_loader(monkeypatch, _Loader())

    with pytest.raises(SystemExit) as exc:
        cli_commands._skills(_args("list", live=False, all=False, json=False))

    assert exc.value.code == 1
    code = (
        "invalid_skill_encoding:" if isinstance(error, UnicodeDecodeError) else "skills_unreadable:"
    )
    assert capsys.readouterr().err.startswith(code)


def test_show_prints_content_metadata_and_validation(loader, monkeypatch, capsys):
    _stage(loader, "candidate-one")
    _use_loader(monkeypatch, loader)

    cli_commands._skills(_args("show", slug="candidate-one"))

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

    cli_commands._skills(_args("show", slug="candidate-one"))

    out = capsys.readouterr().out
    assert "\x1b" not in out
    assert "| # safe\n| redtext" in out
    assert '"description": "line\\\\x0aforged"' in out


def test_show_frames_candidate_lines_that_look_like_cli_validation(monkeypatch, capsys):
    class _Loader:
        def get_pending_skill(self, _slug, **_kwargs):
            return {
                "content": '# safe\n--- validation ---\n{"ok": true}',
                "meta": {},
                "scripts": [],
            }

    _use_loader(monkeypatch, _Loader())

    cli_commands._skills(_args("show", slug="candidate-one"))

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

    cli_commands._skills(_args("show", slug="candidate-one"))

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

    cli_commands._skills(_args("show", slug="candidate-one"))

    out = capsys.readouterr().out
    validation = json.loads(out.split("--- validation ---\n", 1)[1])
    assert validation == {"scripts": {}, "status": "unavailable"}


def test_show_oserror_exits_with_coded_reason(monkeypatch, capsys):
    class _Loader:
        def get_pending_skill(self, _slug, **_kwargs):
            raise OSError("unreadable")

    _use_loader(monkeypatch, _Loader())

    with pytest.raises(SystemExit) as exc:
        cli_commands._skills(_args("show", slug="candidate\nforged"))

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("candidate_unreadable:")
    assert "candidate\\x0aforged" in err


def test_missing_candidate_exits_nonzero(loader, monkeypatch, capsys):
    _use_loader(monkeypatch, loader)

    with pytest.raises(SystemExit) as exc:
        cli_commands._skills(_args("show", slug="missing-one"))

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
        "hidden\n" * (skills_mod.MAX_SCRIPT_BYTES // 7 + 10), encoding="utf-8"
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
        cli_commands._skills(_args("show", slug="cannot-show"))

    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("candidate_unreadable:")
    assert "hidden" not in captured.out
    assert captured.out == ""


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


def test_oversized_update_meta_refuses_both_approval_paths(loader):
    _stage(loader, "big-update")
    meta = loader._pending_root() / "big-update" / ".meta.json"
    meta.write_text(
        json.dumps({"kind": "update", "target": "live", "pad": "x" * 70_000}),
        encoding="utf-8",
    )

    for approve in (loader.approve_pending_skill_checked, loader.approve_pending_update_checked):
        with pytest.raises(skills_mod.PendingApprovalRefused) as refused:
            approve("big-update")
        assert refused.value.reason == "invalid_layout"
    assert (loader._pending_root() / "big-update" / "SKILL.md").exists()
    assert not (loader._dir / "auto" / "big-update").exists()


def test_candidate_without_meta_still_approves_as_new(loader):
    _stage(loader, "no-meta")
    (loader._pending_root() / "no-meta" / ".meta.json").unlink()

    assert loader.approve_pending_skill_checked("no-meta") == "auto/no-meta"


def test_bounded_live_list_caps_the_read_when_the_prescreen_size_is_stale(loader, monkeypatch):
    loader.create_skill(
        "live-grown", "---\nname: live-grown\ndescription: " + "g" * 5000 + "\n---\n"
    )
    monkeypatch.setattr(skills_mod, "_LIST_ROW_MAX_FILE_BYTES", 200)
    # A stale walk fingerprint (or a stat taken before the file grew) admits it.
    monkeypatch.setattr(loader, "_list_row_skip_reason", lambda *_args: None)
    read_sizes: list[int] = []
    real_open = skills_mod.Path.open

    def sized_open(self, *args, **kwargs):
        handle = real_open(self, *args, **kwargs)
        if self.name == "SKILL.md" and "live-grown" in str(self):
            real_read = handle.read

            def read(size=-1):
                data = real_read(size)
                read_sizes.append(len(data))
                return data

            handle.read = read
        return handle

    monkeypatch.setattr(skills_mod.Path, "open", sized_open)
    monkeypatch.setattr(
        skills_mod.Path,
        "read_bytes",
        lambda self: pytest.fail(f"unbounded read of {self}"),
    )

    rows, _ = loader.list_skills_bounded()

    row = {row["key"]: row for row in rows}["live-grown"]
    assert row.get("oversized") is True
    assert "description" not in row
    assert read_sizes and max(read_sizes) <= 201


@pytest.mark.skipif(not hasattr(os, "link"), reason="needs hard links")
def test_bounded_live_list_redacts_before_cutting(loader, monkeypatch):
    secret = "ghp_" + "A" * 36
    loader.create_skill(
        "live-secret", "---\nname: live-secret\ndescription: " + "d" * 80 + secret + "\n---\n"
    )
    monkeypatch.setattr(skills_mod, "_LIST_ROW_MAX_FIELD_CHARS", 100)

    rows, _ = loader.list_skills_bounded()

    description = {row["key"]: row for row in rows}["live-secret"]["description"]
    assert "ghp_" not in description
    assert "AAAAAAAA" not in description


# 41 characters: a 40-character token split by one invisible separator.
_SPLIT_SECRET = "ghp_" + "A" * 20 + "\u2063" + "A" * 16


def test_bounded_live_list_scrubs_an_invisible_split_key_before_cutting(loader, monkeypatch):
    loader.create_skill(
        "live-split",
        "---\nname: live-split\ndescription: " + "d" * 80 + _SPLIT_SECRET + "\n---\n",
    )
    # The cut falls one character short of the token's end.
    monkeypatch.setattr(skills_mod, "_LIST_ROW_MAX_FIELD_CHARS", 120)

    rows, _ = loader.list_skills_bounded()

    description = {row["key"]: row for row in rows}["live-split"]["description"]
    assert "ghp_" not in description
    assert "AAAAAAAA" not in description


def test_pending_rows_scrub_an_invisible_split_key_before_cutting(loader, monkeypatch):
    _stage(loader, "split-pending", description="d" * 80 + _SPLIT_SECRET)
    monkeypatch.setattr(skills_mod, "_LIST_ROW_MAX_FIELD_CHARS", 120)

    rows = loader.list_pending_skills()

    values = [v for row in rows for v in row.values() if isinstance(v, str)]
    assert not any("ghp_" in v or "AAAAAAAA" in v for v in values)


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

    rows, omitted = loader.list_pending_skills_bounded()

    assert [r["slug"] for r in rows] == ["plain-candidate"]
    assert omitted == 0


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

    for rows in (loader.list_pending_skills_bounded()[0], loader.list_pending_skills()):
        by_slug = {r["slug"]: r for r in rows}
        assert set(by_slug) == {"real-candidate", "linked-candidate"}
        linked = by_slug["linked-candidate"]
        assert linked["description"] == ""
        if "script_validation" in linked:
            assert linked["script_validation"]["ok"] is False


def test_pending_slug_that_looks_like_a_credential_is_redacted(loader):
    _stage(loader, "pypi-publish-checklist")

    rows, _ = loader.list_pending_skills_bounded()

    assert [r["slug"] for r in rows] == ["[REDACTED: credential]"]


def test_bounded_live_list_streams_the_catalog_without_materializing_it(loader, monkeypatch):
    for name in ("live-a", "live-b", "live-c"):
        loader.create_skill(name, f"---\nname: {name}\ndescription: {name}\n---\n")
    total = len(loader.list_skills())  # builds and stores the catalog snapshot
    index = loader._search_index
    if index is None:
        pytest.skip("search index unavailable on this host")

    def materialize(*_args, **_kwargs):
        raise AssertionError("the bounded listing must not materialize the catalog")

    monkeypatch.setattr(loader, "_iter", materialize)
    monkeypatch.setattr(loader, "_iter_visible", materialize)
    monkeypatch.setattr(loader, "_load_catalog_snapshot", materialize)
    monkeypatch.setattr(index, "catalog_snapshot", materialize)
    monkeypatch.setattr(skills_mod, "_LIST_MAX_ROWS", 1)

    rows, omitted = loader.list_skills_bounded()

    assert len(rows) == 1
    assert omitted == total - 1


def test_bounded_live_list_refuses_a_catalog_with_a_refused_row(loader, monkeypatch):
    loader.create_skill("live-a", "---\nname: live-a\ndescription: a\n---\n")
    loader.list_skills()
    monkeypatch.setattr(loader, "_snapshot_row_checker", lambda _key: lambda *_row: None)
    monkeypatch.setattr(skills_mod, "_COLD_CATALOG_WAIT_SECS", 0.05)

    rows, omitted = loader.list_skills_bounded()

    assert (rows, omitted) == ([], 0)
    assert loader.catalog_status() == "building"


def test_pending_has_scripts_is_true_only_for_json_true(loader):
    _stage(loader, "string-flag")
    meta = loader._pending_root() / "string-flag" / ".meta.json"
    data = json.loads(meta.read_text(encoding="utf-8"))
    data["has_scripts"] = "false"
    meta.write_text(json.dumps(data), encoding="utf-8")

    rows = {row["slug"]: row for row in loader.list_pending_skills()}

    assert rows["string-flag"]["has_scripts"] is False


def test_skills_spec_states_the_listing_bounds_the_code_enforces():
    spec = (
        Path(__file__).resolve().parents[1] / "docs/system-specs/modules/memory-skills-hooks.md"
    ).read_text(encoding="utf-8")
    line = next(row for row in spec.splitlines() if row.startswith("- `skills list` defaults"))
    assert f"keeps at most {skills_mod._LIST_MAX_ROWS} names" in line
    assert f"first {skills_mod._LIST_MAX_ROWS} visible entries" in line
    assert skills_mod._LIST_ROW_MAX_FILE_BYTES == 1024 * 1024 and "(1 MiB)" in line
    assert f"({skills_mod._LIST_ROW_MAX_FIELD_CHARS:,})" in line


def test_terminal_line_and_dashboard_scrub_share_one_order():
    from kiro_crew.dashboard.handlers._shared import _scrub_text

    corpus = [
        "aws_secret_access_key=" + "a" * 20 + "\x01" + "b" * 20,
        "ghp_" + "A" * 20 + "\u2063" + "A" * 16,
        "plain text with a soft\u00adhyphen",
    ]
    for sample in corpus:
        assert cli_commands._skills_terminal_line(sample) == cli_commands.safe_terminal_line(
            _scrub_text(sample)
        )


def test_list_live_reports_a_catalog_still_being_discovered(monkeypatch, capsys):
    class _Loader:
        def list_skills_bounded(self):
            return [], 0

        def catalog_status(self):
            return "building"

    _use_loader(monkeypatch, _Loader())

    cli_commands._skills(_args("list", live=True, all=False, json=False))
    out = capsys.readouterr().out
    assert "still being discovered" in out
    assert "No live skills." not in out

    cli_commands._skills(_args("list", live=True, all=False, json=True))
    assert json.loads(capsys.readouterr().out)["live_catalog_status"] == "building"


def test_list_redacts_credential_split_by_an_invisible_character(monkeypatch, capsys):
    secret = "aws_secret_access_key=" + "a" * 20 + "\u200b" + "b" * 20

    class _Loader:
        def list_pending_skills_bounded(self):
            return [{"slug": "candidate-one", "description": secret}], 0

    _use_loader(monkeypatch, _Loader())

    cli_commands._skills(_args("list", live=False, all=False, json=False))

    out = capsys.readouterr().out
    assert "[REDACTED: credential]" in out
    assert "a" * 20 not in out


def test_sanitized_keys_that_collide_both_print():
    safe = cli_commands._skills_terminal_value({"run\x01.py": ["x"], "run.py": ["y"]})

    assert safe == {"run.py": ["x"], "run.py (2)": ["y"]}


def test_list_redacts_a_credential_whose_boundary_is_a_control_character(monkeypatch, capsys):
    token = "MTIzNDU2Nzg5MDEyMzQ1Njc4.Gh1jkl.abcdefghijklmnopqrstuvwxy12345"

    class _Loader:
        def list_skills_bounded(self):
            return [{"key": "live-one", "description": "token\x01" + token}], 0

        def catalog_status(self):
            return "complete"

    _use_loader(monkeypatch, _Loader())

    cli_commands._skills(_args("list", live=True, all=False, json=False))

    out = capsys.readouterr().out
    assert token not in out
    assert "abcdefghijklmnopqrstuvwxy12345" not in out


def test_bounded_live_list_sizes_a_confined_entry_only_through_the_pinned_reader(
    loader, monkeypatch, tmp_path
):
    root = tmp_path / "project" / ".kiro" / "skills"
    small = root / "proj-small" / "SKILL.md"
    big = root / "proj-big" / "SKILL.md"
    for path, body in ((small, "small"), (big, "x" * 5000)):
        path.parent.mkdir(parents=True)
        path.write_text(f"---\nname: {path.parent.name}\ndescription: d\n---\n{body}\n")
    entries = [
        skills_mod._ScopedSkillEntry("proj-small", small, str(root), None),
        skills_mod._ScopedSkillEntry("proj-big", big, str(root), None),
    ]
    monkeypatch.setattr(loader, "_bounded_catalog_entries", lambda _key: (entries, 0))
    monkeypatch.setattr(skills_mod, "_LIST_ROW_MAX_FILE_BYTES", 2048)
    real_stat = skills_mod.Path.stat

    def no_name_stat(self, *args, **kwargs):
        if self in (small, big):
            raise AssertionError("a confined entry must not be stat'ed by name")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(skills_mod.Path, "stat", no_name_stat)

    rows, _ = loader.list_skills_bounded()

    by_key = {row["key"]: row for row in rows}
    assert by_key["proj-big"] == {"key": "proj-big", "name": "proj-big", "oversized": True}
    assert "oversized" not in by_key["proj-small"]


def test_bounded_live_list_makes_no_size_claim_for_a_row_list_skills_refused(loader, monkeypatch):
    loader.create_skill("live-a", "---\nname: live-a\ndescription: a\n---\n")
    monkeypatch.setattr(loader, "list_skills", lambda project_dir=None, **_kwargs: [])

    rows, _ = loader.list_skills_bounded()

    assert {"key": "live-a", "name": "live-a"} in rows


def test_pending_rows_cut_every_retained_field(loader, monkeypatch):
    _stage(loader, "long-fields", description="d" * 9000)
    monkeypatch.setattr(skills_mod, "_LIST_ROW_MAX_FIELD_CHARS", 100)

    rows = loader.list_pending_skills()

    assert all(len(v) <= 100 for row in rows for v in row.values() if isinstance(v, str))


def test_show_serves_metadata_the_listing_accepts(loader):
    _stage(loader, "mid-meta")
    meta = loader._pending_root() / "mid-meta" / ".meta.json"
    data = json.loads(meta.read_text(encoding="utf-8"))
    data["notes"] = "n" * (5 * 1024)
    meta.write_text(json.dumps(data), encoding="utf-8")

    assert loader.get_pending_skill("mid-meta") is not None


def test_bounded_live_list_caps_fields_and_skips_oversized_files(loader, monkeypatch):
    loader.create_skill("live-long", "---\nname: live-long\ndescription: " + "d" * 9000 + "\n---\n")
    loader.create_skill("live-big", "---\nname: live-big\ndescription: big\n---\n" + "x" * 5000)
    monkeypatch.setattr(skills_mod, "_LIST_ROW_MAX_FIELD_CHARS", 100)
    read: list[str] = []
    original = loader.list_skills

    def spy(project_dir=None, *, _entries=None, **kwargs):
        read.extend(entry.key for entry in _entries)
        return original(project_dir, _entries=_entries, **kwargs)

    monkeypatch.setattr(loader, "list_skills", spy)
    monkeypatch.setattr(skills_mod, "_LIST_ROW_MAX_FILE_BYTES", 2048)

    rows, _ = loader.list_skills_bounded()

    by_key = {row["key"]: row for row in rows}
    assert by_key["live-big"] == {"key": "live-big", "name": "live-big", "oversized": True}
    assert "live-big" not in read
    assert all(len(v) <= 100 for row in rows for v in row.values() if isinstance(v, str))


def test_show_reports_metadata_nested_too_deeply_without_a_traceback(loader, monkeypatch, capsys):
    _stage(loader, "deep-json")
    _use_loader(monkeypatch, loader)

    def deep(_value):
        raise RecursionError

    monkeypatch.setattr(cli_commands, "_skills_terminal_value", deep)

    with pytest.raises(SystemExit) as exc:
        cli_commands._skills(_args("show", slug="deep-json"))

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
        cli_commands._skills(_args("show", slug="linked-show"))

    assert exc.value.code == 1
    assert capsys.readouterr().err.startswith("candidate_unreadable:")
