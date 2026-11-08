"""RED on main: dashboard-template `scaffold.py --out-root ~/<dir>` does not expand `~`.

`main()` takes `--out-root` as `type=Path` and passes it straight to `emit()`,
which joins `out_root / "src" / "kiro_crew" / ...`. There is no `expanduser()`,
so a `~`-prefixed `--out-root` resolves to a LITERAL directory named `~` under
the process CWD rather than the operator's home. With `--force` (or on a fresh
tree) the four generated files are then written under `./~/...` instead of the
intended checkout, and the paths the command prints as "written" point into the
literal-`~` tree.

How it is hit: a builder runs the scaffold from outside a checkout and points it
at one in their home, e.g.
`python3 scaffold.py my_card --fold status --out-root ~/ws/KiroCrew --print-only`
and every printed path begins with a literal `~`.

Smallest fix: in `main()`, expand before use —
`root = (args.out_root.expanduser() if args.out_root is not None else repo_root())`.

The script runs as a bare file (`kiro_crew` is off its import path), so this
test imports it by file path the way the skill is invoked.
"""

from __future__ import annotations

from pathlib import Path

from skill_script_helpers import load_skill_script

ROOT = Path(__file__).resolve().parents[1]
SCAFFOLD = (
    ROOT
    / "src"
    / "kiro_crew"
    / "builtin_skills"
    / "kirocrew-dev"
    / "dashboard-template"
    / "scripts"
    / "scaffold.py"
)


def test_scaffold_out_root_expands_leading_tilde(tmp_path, monkeypatch, capsys):
    scaffold = load_skill_script("dashboard_template_scaffold_out_root_tilde", SCAFFOLD)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)

    rc = scaffold.main(["my_card", "--fold", "status", "--out-root", "~/ws/co", "--print-only"])
    assert rc == 0, "scaffold --print-only should succeed"
    printed = capsys.readouterr().out

    # RED on main: every path printed begins with a literal "~/ws/co", i.e. the
    # tilde was never expanded to the operator's home.
    assert "~/ws/co" not in printed, (
        "scaffold printed a literal '~/ws/co' path — `--out-root` did not expand `~`:\n" + printed
    )
    assert str(home) in printed, f"expected paths under $HOME ({home}); got:\n{printed}"


def test_scaffold_out_root_unresolvable_tilde_user_does_not_crash(tmp_path, monkeypatch, capsys):
    # A `~user` naming no account makes Path.expanduser() raise RuntimeError on
    # POSIX (Windows ntpath builds a sibling profile path instead, so the raise
    # is stubbed rather than provoked by a real username -- the test must get the
    # same verdict on every platform). main() translates it to a ScaffoldError so
    # the existing "scaffold: ..." diagnostic and exit 2 apply instead of a traceback.
    scaffold = load_skill_script("dashboard_template_scaffold_out_root_bad_user", SCAFFOLD)
    monkeypatch.chdir(tmp_path)

    def _raise(_self):
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(Path, "expanduser", _raise)

    rc = scaffold.main(
        ["my_card", "--fold", "status", "--out-root", "~nosuchuser/ws", "--print-only"]
    )

    assert rc == 2, "an unresolvable ~user --out-root must exit 2, not raise"
    err = capsys.readouterr().err
    assert "scaffold:" in err and "--out-root" in err
