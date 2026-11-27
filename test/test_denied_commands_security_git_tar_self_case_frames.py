"""Cased-vs-lowered frame pairing for the git/tar self-dial floor.

The floor lowercases a line before its argv walk and reads a wrapper's options in the
case they were written (``sudo -H`` takes no value, ``sudo -h`` does). These pin that the
pairing does not desync when the lowered walk carries an alternative frame the cased walk
lacks, on the same ``sandbox-escape-git-tar-self`` rule as the ssh floor's sibling test.
"""

from __future__ import annotations

import pytest
from denied_commands_security_helpers import _own_address_probe_stays_local  # noqa: F401
from denied_commands_security_helpers import _denied_by

pytestmark = pytest.mark.usefixtures("fresh_darwin_libc_handles")


class TestGitTarSelfCaseFrames:
    """The cased/lowered frame pairing stays in step when the walks differ in length."""

    _RULE = "sandbox-escape-git-tar-self"

    @pytest.mark.parametrize(
        "cmd",
        [
            "F=$(which KiroCrew); echo $F; sudo -H git clone localhost:x",  # brand-ok
            "F=$(which KiroCrew); echo $F; bash -c 'sudo -H g?t clone local*:x'",  # brand-ok
            "F=$(which KiroCrew); echo $F; bash -c 'sudo -H t?r -cf local*:x .'",  # brand-ok
        ],
    )
    def test_a_cased_substitution_does_not_desync_the_git_tar_frames(self, cmd):
        # r32 (opus): a cased product name in a substitution body binds on the lowered
        # walk's alternative frame but not on the case-sensitive cased walk, so the two
        # frame lists differ in length. A length-gated positional pairing dropped every
        # cased frame and read ``sudo -H`` as ``-h``, which consumed ``git`` as its value
        # and allowed a self-dial main refuses. Each frame is now also read under every
        # cased frame that lowers to it. A mutant restoring the positional-only pairing
        # re-allows these and fails here.
        assert _denied_by(cmd) == self._RULE, cmd

    def test_a_cased_twin_keeps_an_option_that_takes_an_operand(self):
        # r33 (gpt): reading the lowered frame as well as its cased twin read
        # ``sudo --edit /tmp/git ...`` (sudoedit opens files, runs nothing) as a git run
        # and refused it. A frame with a cased twin is read only under its twins, so this
        # stays allowed, as on main.
        cmd = 'F=$(which KiroCrew); echo "$F"; sudo --edit /tmp/git clone localhost:x'  # brand-ok
        assert _denied_by(cmd) is None, cmd

    @pytest.mark.parametrize(
        "cmd",
        [
            "V=1; ( X=$(true); echo $V )",
            "DIR=/tmp/x; ( RES=$(ls $DIR); echo $RES )",
            'PORT=8080; ( OUT=$(curl -s localhost:$PORT/health); echo "$OUT" )',
            "NAME=foo; ( cd /tmp && OUT=$(ls); echo $NAME $OUT )",
            'BR=main; (SHA=$(git rev-parse HEAD); echo "$BR $SHA"); bash x.sh; git -C r log -1',
            'N=5; (OUT=$(ls); echo $N); bash -c "make"; tar -C build -cf o.tar .',
            'N=5; (OUT=$(ls); echo $N); bash -c "make"; env -C Dir make',
            'N=5; (OUT=$(ls); echo $N); bash -c "make"; ssh -C Host id',
            "BIN=kirocrew; VER=$($BIN --version); echo $VER",
            "SVC=kirocrew; STATUS=$(systemctl --user is-active $SVC); echo $STATUS",
            'export APP=kirocrew; LOG=$(journalctl --user -u $APP -n 20); echo "$LOG"',
            "PK=pkill; R=$(which $PK); echo $R",
            "N=kirocrew; X=$(echo $N); git status",
        ],
    )
    def test_a_subshell_reading_a_cased_outer_variable_keeps_the_walks_paired(self, cmd):
        # The lowered walk writes an outer value into the text before walking, so
        # an additive frame built from it (a ``( ... )`` body, the alternative or rejoined
        # reading) had no cased twin, and the floor failed closed on a command main allows.
        # The floor reads main's frames, without the additive readings.
        assert _denied_by(cmd) is None, cmd


# The floor pairs its lowered walk's frames with its cased walk's one for one,
# and fails closed when the two disagree. It reads the walk without the mint floor's
# additive readings (the alternative, rejoined, seeded and ``( ... )`` body frames), so the
# two walks it pairs are main's.
_CASED = "Kiro" + "Crew"  # brand-ok: a written-case product name in a substitution
_PREFIXES = [
    f'F=$(which {_CASED}); echo "$F"; ',
    f"F=$(which {_CASED}); echo $F; ",
    'X=$(echo Hi); echo "$X"; ',
    f'T=$(: "\'"; printf %s {_CASED}); echo $T; ',
]
_TAILS = [
    "git status",
    "git log -1",
    "sudo --edit /tmp/git clone " + "local" + "host:x",
    "sudo -H git status",
    "tar -tf a.tar",
    "git clone https://github.com/a/b",
    'bash -c "git status"',
    "echo git clone " + "local" + "host:x",
    "sudo -u bob git pull",
]
_PROBE_ROWS = [p + t for p in _PREFIXES for t in _TAILS]


def _fuzz_rows() -> "list[str]":
    """The frame-mismatch test's fuzz lines, both seeds (0x5EED and 0x1234)."""
    import random

    host = "local" + "host"
    rng = random.Random(0x5EED)
    heredocs = ["", "; cat <<EOF\neof", "; cat <<EOF\nEOF", " <<< word", "; cat <<'EOF'\n'\nEOF"]
    heredocs += ["; cat <<A\nit's\nA", "\ncat <<EOF\nX\neof", "; x=$((1<<2))", " <<e\n'\nE"]
    before = ["", 'echo "a;b"; ', "echo 'q'; ", "true && ", "x=1; "]
    hosts = ["ssh://" + host + "/r", host + ":/x", "127.0.0.1:/x"]
    verbs = ["git clone ", "tar -xf "]
    opts = ["", "sudo ", "sudo -H ", "env d=1 "]
    rows = []
    for _ in range(4000):
        line = rng.choice(before) + rng.choice(opts) + rng.choice(verbs) + rng.choice(hosts)
        line += rng.choice(heredocs)
        rows.append("".join(c.upper() if rng.random() < 0.3 else c for c in line))
    rng = random.Random(0x1234)
    remotes = ["ssh://github.com/r", "github.com:/x", "git.example.org:/x"]
    for _ in range(500):
        rows.append(["", "sudo -H "][rng.randrange(2)] + rng.choice(verbs) + rng.choice(remotes))
    return rows


def _disagreeing_walks(rows: "list[str]", monkeypatch) -> "list[str]":
    """The rows whose typed git/tar read gets cased and lowered walks that do not pair:
    a different frame count, or a frame past the top whose lowered source differs (a
    reading of the top line itself may differ through the pre-walk's lowercase-only
    rewrites, as the floor's own top-frame pairing allows)."""
    from kiro_crew import security
    from kiro_crew.security import argv_floor_git_tar as floor

    bad: "list[str]" = []
    current: "list[str]" = [""]
    read = floor._git_tar_read

    def spy(text_lower: str, *, typed: bool = False) -> bool:
        raw = floor._RAW_COMMAND.get()
        if typed and raw is not None and raw != raw.lower():
            lowered = floor._shell_payload_walk(
                floor._mask_quoted_separators(floor._ssh_floor_unmasked_text(text_lower)),
                additive=False,
            )
            cased = floor._shell_payload_walk(floor._ssh_floor_source_text(raw), additive=False)
            top = (lowered[0][0], cased[0][0].lower())
            if len(lowered) != len(cased) or any(
                a != b.lower() and (a, b.lower()) != top
                for (a, _), (b, _) in zip(lowered[1:], cased[1:])
            ):
                bad.append(current[0])
        return read(text_lower, typed=typed)

    monkeypatch.setattr(floor, "_git_tar_read", spy)
    for row in rows:
        current[0] = row
        security.is_denied(row)
    return sorted(set(bad))


def test_a_cased_substitution_gives_both_walks_the_same_frames(monkeypatch):
    # A substitution naming the product in written case (``$(which KiroCrew)``) made the  # brand-ok
    # alternative frame in the lowered walk only, so the floor found a lowered frame with
    # no cased twin and refused ``git status``. A mutant that decides a reading on unlowered
    # text, or drops an additive frame equal to the baseline in one walk, fails here.
    assert _disagreeing_walks(_PROBE_ROWS, monkeypatch) == []
    for row in _PROBE_ROWS:
        assert _denied_by(row) is None, row


@pytest.mark.timeout(300)
def test_the_walks_agree_on_every_seed_and_fuzz_line_but_a_here_document(monkeypatch):
    # The floor's own disagreeing shapes are here-document lines, which it fails closed;
    # nothing this walk adds may disagree anywhere else.
    import json
    from pathlib import Path

    seed = Path(__file__).parent.parent / "src/kiro_crew/builtin_skills/security-conductor"
    paths = json.loads((seed / "golden-paths.json").read_text())["golden_paths"]
    rows = [r["command_or_flow"] for r in paths if r.get("kind") == "shell"] + _fuzz_rows()
    assert [r for r in _disagreeing_walks(rows, monkeypatch) if "<<" not in r] == []
