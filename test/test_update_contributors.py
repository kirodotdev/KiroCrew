"""Unit tests for scripts/update_contributors.py.

The script edits the public README's Contributors block from PR-controlled
author data, so the invariants that matter are: existing entries survive
byte-for-byte (hand-curated names and inline comments must not be clobbered),
insertion keeps the block's case-insensitive login order, the operation is
idempotent, and no display name can inject HTML. Each gets a test here; the
script's own ``--test`` mode covers the same families for a repo-free smoke run.
"""

from __future__ import annotations

import importlib.util
import os
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPT_PATH = os.path.join(_REPO_ROOT, "scripts", "update_contributors.py")


def _load():
    spec = importlib.util.spec_from_file_location("update_contributors", _SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["update_contributors"] = module
    spec.loader.exec_module(module)
    return module


uc = _load()


def _entry(login: str, name: str) -> str:
    return (
        f'<a href="https://github.com/{login}" title="{name}">'
        f'<img src="https://github.com/{login}.png?size=64"'
        f' width="64" height="64" alt="{name}" /></a>'
    )


def _readme(*logins_and_names: tuple[str, str]) -> str:
    body = "\n".join(_entry(lg, nm) for lg, nm in logins_and_names)
    return f"## Contributors\n\nintro paragraph\n\n{body}\n\ntrailer\n"


def _logins(text: str) -> list[str]:
    return [m.group("login") for m in (uc._ENTRY_RE.match(line) for line in text.splitlines()) if m]


def test_inserts_at_sorted_position():
    text = _readme(("0V", "G2"), ("bob", "Bob"))
    out, added = uc.add_contributors(text, [{"login": "amy", "name": "Amy"}])
    assert added == ["amy"]
    assert _logins(out) == ["0V", "amy", "bob"]


def test_idempotent_case_insensitive():
    text = _readme(("amy", "Amy"), ("bob", "Bob"))
    out, added = uc.add_contributors(text, [{"login": "AMY", "name": "whatever"}])
    assert added == []
    assert out == text


def test_existing_lines_preserved_byte_for_byte():
    # A hand-curated entry with a trailing inline comment (like the repo's
    # wokeignore marker) must survive untouched when a new entry is inserted.
    curated = _entry("bob", "Bob The Great") + " <!-- keep me -->"
    text = f"## Contributors\n\nintro\n\n{_entry('0V', 'G2')}\n{curated}\n\ntail\n"
    out, added = uc.add_contributors(text, [{"login": "amy", "name": "Amy"}])
    assert added == ["amy"]
    assert curated in out.splitlines()


def test_html_in_display_name_is_escaped():
    text = _readme(("bob", "Bob"))
    payload = '"><script>alert(1)</script>'
    out, _ = uc.add_contributors(text, [{"login": "carol", "name": payload}])
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "&quot;&gt;" in out


@pytest.mark.parametrize(
    "login",
    ["bad login", "-lead", "trail-", "has_underscore", "github-actions[bot]", "", "a" * 40],
)
def test_invalid_logins_dropped(login):
    text = _readme(("bob", "Bob"))
    out, added = uc.add_contributors(text, [{"login": login, "name": "x"}])
    assert added == []
    assert out == text


def test_valid_login_edges():
    text = _readme(("bob", "Bob"))
    ok = ["a", "A1", "abc-def", "a" * 39, "0", "user-name-1"]
    out, added = uc.add_contributors(text, [{"login": lg, "name": lg} for lg in ok])
    assert sorted(added) == sorted(ok)


def test_empty_name_falls_back_to_login():
    text = _readme(("bob", "Bob"))
    out, _ = uc.add_contributors(text, [{"login": "dave", "name": ""}])
    assert 'title="dave"' in out
    assert 'alt="dave"' in out


def test_batch_dedup_and_order():
    text = _readme(("0V", "G2"), ("bob", "Bob"))
    out, added = uc.add_contributors(
        text,
        [
            {"login": "zed", "name": "Zed"},
            {"login": "AAA", "name": "Triple A"},
            {"login": "zed", "name": "dup ignored"},
        ],
    )
    assert sorted(added) == ["AAA", "zed"]
    # Digits sort before letters under case-folding, so 0V leads.
    assert _logins(out) == ["0V", "AAA", "bob", "zed"]


def test_manual_out_of_order_entry_is_not_moved():
    # A maintainer added "mZebra" by hand between "amy" and "bob" (out of strict
    # sort order). Automation must leave it exactly there and never reorder it.
    manual = _entry("mZebra", "Manual Zebra Add")
    text = (
        "## Contributors\n\nintro\n\n"
        f"{_entry('amy', 'Amy')}\n"
        f"{manual}\n"
        f"{_entry('bob', 'Bob')}\n"
        "\ntail\n"
    )
    out, added = uc.add_contributors(text, [{"login": "carl", "name": "Carl"}])
    assert added == ["carl"]
    out_lines = out.splitlines()
    # The manual line survives byte-for-byte and keeps its relative position.
    assert manual in out_lines
    assert out_lines.index(_entry("amy", "Amy")) < out_lines.index(manual)
    assert out_lines.index(manual) < out_lines.index(_entry("bob", "Bob"))


def test_manually_added_name_not_re_added():
    # A name a maintainer added by hand must not be duplicated when the same
    # author later shows up in the automated sweep (case-insensitive match).
    manual = _entry("Jane-Doe", "Jane Doe (hand-added)")
    text = f"## Contributors\n\nintro\n\n{manual}\n\ntail\n"
    out, added = uc.add_contributors(text, [{"login": "jane-doe", "name": "jane-doe"}])
    assert added == []
    assert out == text


@pytest.mark.parametrize(
    "credit",
    [
        '<a href="https://github.com/gus">Gus</a>',  # anchor
        "see https://github.com/gus).",  # bare url, punctuation after
        "see https://github.com/gus/ for art",  # trailing slash
        "profile https://github.com/gus?tab=repositories",  # query string
        "profile https://github.com/gus#readme",  # fragment
        "shorthand github.com/gus mentioned",  # no scheme
        "https://www.github.com/gus",  # www host
        "http://github.com/gus",  # http scheme
    ],
)
def test_manual_credit_outside_block_not_re_added(credit):
    # A contributor credited by hand ANYWHERE in the README, in ANY reasonable
    # URL form, must not be re-added into the sorted block (dedup is by profile
    # link across the whole file, not just canonical block entries).
    text = f"## Contributors\n\nintro {credit}\n\n{_entry('amy', 'Amy')}\n{_entry('zoe', 'Zoe')}\n\ntail\n"
    out, added = uc.add_contributors(text, [{"login": "gus", "name": "Gus"}])
    assert added == []
    assert out == text


@pytest.mark.parametrize(
    "repo_link",
    [
        'See <a href="https://github.com/kirodotdev/KiroCrew/releases">releases</a>.',
        "docs at https://github.com/kirodotdev/KiroCrew/",  # trailing slash on a repo
        "https://github.com/kirodotdev/KiroCrew#install",  # repo + fragment
    ],
)
def test_repo_and_deep_links_are_not_treated_as_contributors(repo_link):
    # A deeper path (repo, releases, ...) must never be read as a profile, or a
    # real new contributor named like the repo owner would be wrongly skipped.
    text = f"## Contributors\n\n{repo_link}\n\n{_entry('amy', 'Amy')}\n\ntail\n"
    out, added = uc.add_contributors(text, [{"login": "kirodotdev", "name": "Kiro"}])
    assert added == ["kirodotdev"]


def test_optout_login_never_added_even_when_absent():
    # The removal promise: a login on the opt-out list must not be re-added by a
    # full rebuild, even though it is absent from the README.
    text = _readme(("amy", "Amy"))
    out, added = uc.add_contributors(
        text,
        [{"login": "quinn", "name": "Quinn"}, {"login": "rob", "name": "Rob"}],
        optout={"quinn"},
    )
    assert added == ["rob"]
    assert "github.com/quinn" not in out


def test_optout_is_case_insensitive():
    text = _readme(("amy", "Amy"))
    out, added = uc.add_contributors(text, [{"login": "Quinn", "name": "Quinn"}], optout={"quinn"})
    assert added == []
    assert out == text


def test_load_optout_parses_comments_and_blanks(tmp_path):
    f = tmp_path / "optout.txt"
    f.write_text(
        "# a comment\n\nAlice\nbob   # trailing note\n\n  Carol  \n",
        encoding="utf-8",
    )
    assert uc._load_optout(f) == {"alice", "bob", "carol"}


def test_load_optout_missing_file_is_empty(tmp_path):
    # A missing opt-out file must not break the run; the feature is opt-in.
    assert uc._load_optout(tmp_path / "does-not-exist.txt") == set()


def test_main_honors_optout_file(tmp_path):
    readme = tmp_path / "README.md"
    readme.write_text(_readme(("amy", "Amy")), encoding="utf-8")
    optout = tmp_path / "optout.txt"
    optout.write_text("quinn\n", encoding="utf-8")
    rc = uc.main(
        ["--login", "quinn", "--name", "Quinn", "--readme", str(readme), "--optout", str(optout)]
    )
    # Nothing added -> exit 0, README unchanged.
    assert rc == 0
    assert "github.com/quinn" not in readme.read_text(encoding="utf-8")


@pytest.mark.parametrize("boundary", ["\n", "\r", "\u2028", "\u2029", "\x85", "\x0b", "\x0c"])
def test_line_boundary_in_name_stays_single_line_and_idempotent(boundary):
    # A display name carrying a line-boundary char must render exactly one line;
    # otherwise the block becomes non-contiguous and every later run wedges.
    text = _readme(("bob", "Bob"))
    name = f"Ca{boundary}rol"
    out, added = uc.add_contributors(text, [{"login": "carol", "name": name}])
    assert added == ["carol"]
    carol_lines = [ln for ln in out.splitlines() if 'github.com/carol"' in ln]
    assert len(carol_lines) == 1
    # And re-running is a clean no-op (block stayed contiguous).
    out2, added2 = uc.add_contributors(out, [{"login": "carol", "name": name}])
    assert added2 == []
    assert out2 == out


def test_null_login_is_dropped_not_stringified():
    # A deleted PR author yields a null login; it must be dropped, never turned
    # into the string "None" and injected as github.com/None.
    text = _readme(("bob", "Bob"))
    records = uc._coerce_records([{"login": None, "name": None}, {"login": "real", "name": "Real"}])
    out, added = uc.add_contributors(text, records)
    assert added == ["real"]
    assert "github.com/None" not in out


def test_coerce_records_null_login_becomes_empty():
    recs = uc._coerce_records([{"login": None, "name": None}])
    assert recs == [{"login": "", "name": ""}]


def test_missing_block_raises():
    with pytest.raises(ValueError):
        uc.add_contributors("# README\n\nno contributors here\n", [{"login": "x", "name": "x"}])


def test_non_contiguous_block_raises():
    # An anchor run split by a stray line is ambiguous — refuse rather than guess.
    text = (
        "## Contributors\n\n"
        f"{_entry('0V', 'G2')}\n"
        "stray line in the middle\n"
        f"{_entry('bob', 'Bob')}\n"
    )
    with pytest.raises(ValueError):
        uc.add_contributors(text, [{"login": "amy", "name": "Amy"}])


def test_preserves_trailing_newline_and_crlf():
    text = _readme(("bob", "Bob"))
    out, _ = uc.add_contributors(text, [{"login": "amy", "name": "Amy"}])
    assert out.endswith("\n")

    crlf = text.replace("\n", "\r\n")
    out_crlf, _ = uc.add_contributors(crlf, [{"login": "amy", "name": "Amy"}])
    assert "\r\n" in out_crlf
    assert "\n\n" not in out_crlf.replace("\r\n", "")


def test_no_change_returns_original_text():
    text = _readme(("amy", "Amy"))
    out, added = uc.add_contributors(text, [])
    assert added == []
    assert out == text


def test_coerce_records_rejects_non_list():
    with pytest.raises(ValueError):
        uc._coerce_records({"login": "x"})


def test_coerce_records_normalizes():
    recs = uc._coerce_records([{"login": " amy ", "name": " Amy "}, {"login": "bob"}])
    assert recs == [{"login": "amy", "name": "Amy"}, {"login": "bob", "name": ""}]


def test_self_test_passes():
    assert uc._self_test() == 0


def test_main_single_flag_updates_readme(tmp_path):
    readme = tmp_path / "README.md"
    readme.write_text(_readme(("bob", "Bob")), encoding="utf-8")
    rc = uc.main(["--login", "amy", "--name", "Amy", "--readme", str(readme)])
    assert rc == 0
    assert 'title="Amy"' in readme.read_text(encoding="utf-8")


def test_main_missing_block_exits_one(tmp_path):
    readme = tmp_path / "README.md"
    readme.write_text("# no block\n", encoding="utf-8")
    rc = uc.main(["--login", "amy", "--name", "Amy", "--readme", str(readme)])
    assert rc == 1


def test_main_preserves_crlf_end_to_end(tmp_path):
    # A CRLF README must survive a real file round-trip: reading with universal
    # newlines would strip \r before the CRLF-preserving logic ever saw it, then
    # rewrite the whole file as LF. Read/write with newline="" prevents that.
    readme = tmp_path / "README.md"
    crlf = _readme(("bob", "Bob")).replace("\n", "\r\n")
    readme.write_bytes(crlf.encode("utf-8"))
    before_crlf = readme.read_bytes().count(b"\r\n")

    rc = uc.main(["--login", "amy", "--name", "Amy", "--readme", str(readme)])
    assert rc == 0

    raw = readme.read_bytes()
    # The inserted line adds one CRLF; crucially, none are lost/converted to LF.
    assert raw.count(b"\r\n") == before_crlf + 1
    assert b"\n" not in raw.replace(b"\r\n", b"")  # no bare LF introduced
    assert b'title="Amy"' in raw


def test_main_preserves_lf_end_to_end(tmp_path):
    # The mirror case: a pure-LF README must not gain any CR.
    readme = tmp_path / "README.md"
    readme.write_bytes(_readme(("bob", "Bob")).encode("utf-8"))
    rc = uc.main(["--login", "amy", "--name", "Amy", "--readme", str(readme)])
    assert rc == 0
    assert b"\r" not in readme.read_bytes()


def test_main_unreadable_readme_exits_two(tmp_path):
    # A missing/unreadable --readme is an environment error (exit 2), not a
    # crash and not "no findings".
    rc = uc.main(["--login", "amy", "--name", "Amy", "--readme", str(tmp_path / "nope.md")])
    assert rc == 2


def test_main_unwritable_readme_exits_two_and_preserves_original(tmp_path, monkeypatch):
    # A write failure after a successful read must honor the exit-2
    # environment-error contract AND leave the original README intact (atomic
    # write): a partial/truncated file would be worse than no update. Simulate a
    # mid-write failure by making the final atomic rename raise.
    readme = tmp_path / "README.md"
    original = _readme(("bob", "Bob"))
    readme.write_text(original, encoding="utf-8")

    def failing_replace(src, dst, *args, **kwargs):
        raise OSError("no space left on device")

    monkeypatch.setattr(uc.os, "replace", failing_replace)
    rc = uc.main(["--login", "amy", "--name", "Amy", "--readme", str(readme)])
    assert rc == 2
    # Original content is untouched, and no temp file was left behind.
    assert readme.read_text(encoding="utf-8") == original
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "README.md"]
    assert leftovers == []


def test_atomic_write_leaves_no_temp_and_preserves_newlines(tmp_path):
    # Happy path: the atomic write replaces the file, preserves CRLF, and leaves
    # no .contributors-*.tmp sibling.
    target = tmp_path / "README.md"
    uc._atomic_write(target, "line1\r\nline2\r\n")
    assert target.read_bytes() == b"line1\r\nline2\r\n"
    assert [p.name for p in tmp_path.iterdir()] == ["README.md"]
