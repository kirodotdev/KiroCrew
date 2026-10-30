"""RED on main: `kirocrew perf sample --output ~/sub/x.folded` does not expand `~`.

`cli_perf._write_artifact(output, text)` is handed `args.output` verbatim as a
`Path`, and `--output` is `type=Path` with no `expanduser()`. So a `~`-prefixed
output path is written to a LITERAL directory named `~` under the process CWD
instead of the operator's home. Every other caller-supplied path in the CLI
(`cli_commands_app`, `cli_config`, `cli_commands_gateway_api`, ...) calls
`Path(...).expanduser()`; this one is the outlier.

How it is hit: an operator (or an agent, which can run any CLI command) runs
`KIROCREW_DEBUG=1 kirocrew perf sample --call m:f --output ~/profiles/p.folded`
and the profile silently lands in `./~/profiles/p.folded`, not `~/profiles/`.

Smallest fix: `_write_artifact` (or the two call sites) expands the path:
`output = output.expanduser()` before `output.parent.mkdir(...)`.
"""

from __future__ import annotations

from pathlib import Path

from kiro_crew import cli_perf


def test_write_artifact_expands_leading_tilde(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))  # Windows home
    # Run from a scratch CWD so a literal "~" dir, if created, is visible here.
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)

    # The exact shape argparse hands _write_artifact for `--output ~/prof/p.folded`.
    out = Path("~/prof/p.folded")
    ok = cli_perf._write_artifact(out, "folded-stacks-body")
    assert ok, "write should succeed"

    expanded = home / "prof" / "p.folded"
    literal = cwd / "~" / "prof" / "p.folded"

    # RED on main: the profile lands under a literal "~" in CWD, not in $HOME.
    assert (
        not literal.exists()
    ), f"profile was written to a literal '~' dir: {literal} — `~` was not expanded"
    assert expanded.exists(), f"profile should have been written under $HOME at {expanded}"
    assert expanded.read_text(encoding="utf-8") == "folded-stacks-body"


def test_write_artifact_unresolvable_tilde_user_does_not_crash(tmp_path, monkeypatch, capsys):
    # A `~user` naming no account makes Path.expanduser() raise RuntimeError on
    # POSIX (Windows ntpath builds a sibling profile path instead, so the raise
    # is stubbed rather than provoked by a real username -- the test must get the
    # same verdict on every platform). The expansion runs inside _write_artifact's
    # write-error handler, so this returns False with a diagnostic rather than a
    # traceback over an already-collected profile.
    monkeypatch.chdir(tmp_path)

    def _raise(_self):
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(Path, "expanduser", _raise)

    ok = cli_perf._write_artifact(Path("~nosuchuser/prof/p.folded"), "folded-stacks-body")

    assert ok is False, "an unresolvable ~user must be reported, not raised"
    err = capsys.readouterr().err
    assert "Could not write the profile" in err
    # Nothing is written under a literal "~nosuchuser..." dir either.
    assert not (tmp_path / "~nosuchuser").exists()
