"""Review-workflow tests that run Code Review's grep steps against a real two-commit
repository.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest
from ai_review_workflows_helpers import (
    _bash,
    _child_env,
    _proc_log,
    _step_script,
    _workflow,
)


def _grep_has_pcre(bash: str, path: str) -> bool:
    """The Code Review greps use `grep -P`, which BSD grep and Git for Windows lack."""
    probe = subprocess.run(
        [bash, "-c", "printf 'a\\n' | grep -qP 'a(?!b)'"],
        env=_child_env({"PATH": path}),
        check=False,
        capture_output=True,
    )
    return probe.returncode == 0


# A `grep` that hands `-P` patterns to perl (PCRE's own dialect) and every other
# call to the real grep. The Code Review steps only ever read `-P` from stdin with
# `-n`, `-q` or `-v`, which is all this implements.
_PCRE_GREP_SHIM = r"""#!/usr/bin/env bash
real=%s
case "$1" in -*P*) ;; *) exec "$real" "$@" ;; esac
flags=$1; shift
pat=$1
[ "$pat" = -- ] && { shift; pat=$1; }
N=0; V=0; Q=0
case "$flags" in *n*) N=1 ;; esac
case "$flags" in *v*) V=1 ;; esac
case "$flags" in *q*) Q=1 ;; esac
PAT=$pat N=$N V=$V Q=$Q exec perl -ne '
  my $hit = /$ENV{PAT}/ ? 1 : 0;
  $hit = !$hit if $ENV{V};
  if ($hit) { $m = 1; next if $ENV{Q}; print $ENV{N} ? "$.:$_" : $_ }
  END { exit($m ? 0 : 1) }'
"""


def _pcre_grep_path(bash: str, tmp_path: Path) -> str:
    """PATH under which `grep -P` works: the host's own, else the perl shim."""
    path = os.environ.get("PATH", "")
    if _grep_has_pcre(bash, path):
        return path
    real = subprocess.run(
        [bash, "-c", "command -v grep"],
        env=_child_env({"PATH": path}),
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()
    shim_dir = tmp_path / "pcre-grep"
    shim_dir.mkdir(exist_ok=True)
    shim = shim_dir / "grep"
    shim.write_bytes((_PCRE_GREP_SHIM % shlex.quote(real)).encode())
    shim.chmod(0o755)
    return os.pathsep.join([str(shim_dir), path])


class TestCodeReviewGreps:
    """Run the Code Review grep steps against a real two-commit repository.

    Each case adds one line to one file and asserts what the step does with it,
    so a pathspec that skips a file or a regex that misses a shape fails here
    instead of passing silently in CI.
    """

    def _run(
        self, tmp_path: Path, step: str, rel: str, line: str, workdir: str
    ) -> "subprocess.CompletedProcess[str]":
        # The Code Review job runs these steps under bash with GNU `grep -P`.
        # A host whose grep lacks -P runs them through the perl shim; a host
        # without bash, git or a working -P fails, never skips.
        bash = _bash()
        assert bash is not None, "these cases need bash (Git Bash on Windows)"
        assert shutil.which("git") is not None, "these cases need git"
        path = _pcre_grep_path(bash, tmp_path)
        assert _grep_has_pcre(bash, path), "these cases need grep -P or perl"
        repo = tmp_path / "repo"
        repo.mkdir(exist_ok=True)
        git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run([*git, "init", "-q"], check=True)
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"// seed\n")
        subprocess.run([*git, "add", "-A"], check=True)
        subprocess.run([*git, "commit", "-qm", "base"], check=True)
        base = subprocess.run(
            [*git, "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()
        target.write_bytes(f"// seed\n{line}\n".encode())
        subprocess.run([*git, "commit", "-qam", "head"], check=True)
        head = subprocess.run(
            [*git, "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()
        script = _step_script(_workflow("code-review.yml"), step)
        env = _child_env({"PATH": path, "BASE": base, "HEAD": head})
        return subprocess.run(
            [bash, "-c", script],
            cwd=repo / workdir,
            env=env,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    @pytest.mark.parametrize(
        ("rel", "line", "blocks"),
        (
            # A file directly under src/ is reached (':(glob)' pathspec).
            ("src/App.tsx", "el.innerHTML = html", True),
            ("src/rum.ts", "el.outerHTML += html", True),
            ("src/a/b.ts", "el['innerHTML'] = html", True),
            ("src/a/b.ts", "el.insertAdjacentHTML('beforeend', html)", True),
            ("src/a/b.ts", "if (el.innerHTML === prev) {}", False),
            ("src/a/b.ts", "mermaid.initialize({ 'securityLevel' : \"antiscript\" })", True),
            ("src/a/b.ts", "mermaid.initialize({ securityLevel: 'strict' })", False),
            ("src/main.tsx", "<span onClick={go}>x</span>", True),
            ("src/a/b.tsx", '<div data-role="x" onClick={go}>x</div>', True),
            ("src/a/b.tsx", '<span onClick={() => go()} role="button">x</span>', False),
            ("src/a/b.tsx", "// a <div onClick> in a comment", False),
            # Brand components are not exempt; only the legacy KiroGhost.tsx is excluded.
            ("src/components/FooLogo.tsx", '<svg viewBox="0 0 1 1"></svg>', True),
            ("src/components/KiroGhost.tsx", '<svg viewBox="0 0 1 1"></svg>', False),
        ),
    )
    def test_frontend_blocking_greps(self, tmp_path: Path, rel: str, line: str, blocks: bool):
        result = self._run(
            tmp_path, "Check frontend blocking rules", f"website/{rel}", line, "website"
        )
        assert (result.returncode != 0) is blocks, _proc_log(result)

    @pytest.mark.parametrize(
        ("rel", "line", "warns"),
        (
            ("src/index.css", "@keyframes spin { }", True),
            ("src/a/b.css", "/* no @keyframes here */", False),
            ("src/App.tsx", '<p className="text-[8px]" />', True),
            ("src/a/b.tsx", "// bg-gray-100 is a token we avoid", False),
        ),
    )
    def test_frontend_advisory_greps(self, tmp_path: Path, rel: str, line: str, warns: bool):
        result = self._run(
            tmp_path, "Check frontend blocking rules", f"website/{rel}", line, "website"
        )
        assert result.returncode == 0, _proc_log(result)
        assert ("::warning::" in result.stdout) is warns, _proc_log(result)

    @pytest.mark.parametrize(
        "line",
        (
            "open(Path.home() / '.kiro/crew/.env')",
            "open(os.path.expanduser('~/.kiro/crew/security_policy.json'))",
            "open('.kirocrew/.env')",
        ),
    )
    def test_backend_sensitive_path_grep_knows_the_data_home(self, tmp_path: Path, line: str):
        result = self._run_backend(tmp_path, "src/kiro_crew/thing.py", line)
        assert result.returncode != 0, _proc_log(result)
        assert "Sensitive credential/keystone paths" in result.stdout, _proc_log(result)

    def _run_backend(
        self, tmp_path: Path, rel: str, line: str
    ) -> "subprocess.CompletedProcess[str]":
        # The step also asserts the keystone leaf and tuple in the tree; seed the
        # two files it greps so only the check under test decides the outcome.
        repo = tmp_path / "repo"
        for seed, text in (
            ("src/kiro_crew/security/paths.py", '"denied_commands.json"\n'),
            ("src/kiro_crew/platform/governance.py", '".kiro/crew/denied_commands.json"\n'),
        ):
            (repo / seed).parent.mkdir(parents=True, exist_ok=True)
            (repo / seed).write_text(text, encoding="utf-8")
        return self._run(tmp_path, "Check backend security rules", rel, line, ".")

    def test_bool_tripwire_covers_the_security_handler(self, tmp_path: Path):
        result = self._run_backend(
            tmp_path,
            "src/kiro_crew/dashboard/handlers/security.py",
            "x = bool(denied.get('disable_all'))",
        )
        assert result.returncode != 0, _proc_log(result)
        assert "_coerce_bool()" in result.stdout, _proc_log(result)
