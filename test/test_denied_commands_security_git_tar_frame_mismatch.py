"""The git/tar-to-self floor fails closed when its two readings of a line (the
case it was written and lowered) disagree on frame count, instead of reading on
with the written-case option words dropped.

The regression rows below pin the fail-closed behaviour on the known
disagree shapes. The fuzz pass then asserts the safety OUTCOME across thousands
of random here-document / quote / case / separator lines wrapped around a
transport to this host: none escapes the floor.
"""

from __future__ import annotations

import random
import shlex
from collections import Counter

import pytest
from denied_commands_security_helpers import (  # noqa: F401
    _denied_by,
    _own_address_probe_stays_local,
)

from kiro_crew.security import is_denied  # noqa: E402

_RULE = "sandbox-escape-git-tar-self"
_S = "local" + "host"


class TestFrameMismatchFailsClosed:
    # The quoted-separator + mixed-case here-document-tag shape that made the two
    # passes count a different number of frames. The floor now refuses a git/tar
    # transport to this host on such a line instead of dropping the written-case
    # option words. A remote host on the same shape is refused too -- an accepted,
    # rare, fail-closed over-refusal of a line whose two readings disagree.
    @pytest.mark.parametrize(
        ("cmd", "rule"),
        [
            ('echo "a;b"; sudo -H git clone ssh://' + _S + "/r; cat <<EOF\neof", _RULE),
            ('echo "a;b"; sudo -H tar -xf ' + _S + ":/x; cat <<EOF\neof", _RULE),
            ('echo "a;b"; sudo -H git clone ssh://github.com/r; cat <<EOF\neof', _RULE),
        ],
        ids=["self-git", "self-tar", "remote-fail-closed"],
    )
    def test_a_disagreeing_line_is_refused(self, cmd, rule):
        assert _denied_by(cmd) == rule, cmd

    # Two payloads that differ only in case are one frame lowered but two as
    # written. That is not a disagreement: a local command is still allowed.
    @pytest.mark.parametrize(
        "cmd",
        [
            "bash -c 'git log --format=%H' && bash -c 'git log --format=%h'",
            "sh -c 'git status -S' ; sh -c 'git status -s'",
            "bash -c 'tar -cf A.tar x' && bash -c 'tar -cf a.tar x'",
            "bash -c 'sudo -h git clone ssh://github.com/r' && "
            "bash -c 'sudo -H git clone ssh://github.com/r'",
        ],
        ids=["git-log", "git-status", "tar-local", "remote"],
    )
    def test_case_only_duplicate_payloads_are_allowed(self, cmd):
        assert is_denied(cmd) is None, cmd

    # Each written spelling of a case-only duplicate is read: ``sudo -H`` runs the
    # git/tar word, so the pair dials this host whichever spelling comes first.
    @pytest.mark.parametrize(
        "cmd",
        [
            f"bash -c 'sudo -h git clone {_S}:/x' && bash -c 'sudo -H git clone {_S}:/x'",
            f"bash -c 'sudo -H git clone {_S}:/x' && bash -c 'sudo -h git clone {_S}:/x'",
            f"bash -c 'sudo -h tar -xf {_S}:/x' ; bash -c 'sudo -H tar -xf {_S}:/x'",
        ],
        ids=["git-h-first", "git-H-first", "tar"],
    )
    def test_every_spelling_of_a_case_only_duplicate_is_read(self, cmd):
        assert _denied_by(cmd) == _RULE, cmd

    # An uppercase same-line variable is resolved in the lowered reading only,
    # so the top frame's text differs between the readings while its words
    # agree: the top frame still pairs, and a local line stays allowed. A line
    # that also dials this host is still refused on the same shape.
    def test_an_uppercase_variable_does_not_unpair_the_top_frame(self):
        lead = "VERSION=1; echo $VERSION; "
        local = "bash -c 'git log --format=%H' && bash -c 'git log --format=%h'"
        assert is_denied(lead + local) is None
        for dial in (
            f"bash -c 'sudo -h git clone {_S}:/x' && bash -c 'sudo -H git clone {_S}:/x'",
            'echo "a;b"; sudo -H git clone ssh://' + _S + "/r; cat <<EOF\neof",
        ):
            assert _denied_by(lead + dial) == _RULE, dial

    # The same disagreeing shape on a line that runs no git/tar and carries no
    # self-dial signal is ordinary shell, so it is allowed: the refusal needs a
    # self-dial signal, as the floor's other fail-closed reads do.
    @pytest.mark.parametrize(
        "cmd",
        [
            'echo "a;b"; npm start; cat <<EOF\neof',
            'echo "a;b"; npm run legit; cat <<EOF\neof',
        ],
        ids=["npm-start", "git-substring"],
    )
    def test_a_disagreeing_line_with_no_self_dial_signal_is_allowed(self, cmd):
        assert is_denied(cmd) is None, cmd

    # 4,000 full ``is_denied`` reads is CPU-bound; under the sharded lane's parallel
    # workers it runs well past the 120s default, so it keeps its own larger bound.
    @pytest.mark.timeout(300)
    def test_fuzz_no_self_transport_escapes_a_heredoc_shape(self):
        # The safety outcome of failing closed: a git or tar transport to this
        # host is refused however a here-document, stray quote, case or separator
        # is wrapped around it. ``sudo -H`` keeps the verb (it takes no value);
        # ``sudo -h`` is excluded because it consumes the next word, so the line
        # runs no git/tar at all.
        rng = random.Random(0x5EED)  # fixed seed: deterministic
        heredocs = [
            "",
            "; cat <<EOF\neof",
            "; cat <<EOF\nEOF",
            " <<< word",
            "; cat <<'EOF'\n'\nEOF",
            "; cat <<A\nit's\nA",
            "\ncat <<EOF\nX\neof",
            "; x=$((1<<2))",
            " <<e\n'\nE",
        ]
        before = ["", 'echo "a;b"; ', "echo 'q'; ", "true && ", "x=1; "]
        selfhosts = ["ssh://" + _S + "/r", _S + ":/x", "127.0.0.1:/x"]
        verbs = ["git clone ", "tar -xf "]
        opts = ["", "sudo ", "sudo -H ", "env d=1 "]

        def mix_case(s: str) -> str:
            return "".join(c.upper() if rng.random() < 0.3 else c for c in s)

        for _ in range(4000):
            cmd = mix_case(
                rng.choice(before)
                + rng.choice(opts)
                + rng.choice(verbs)
                + rng.choice(selfhosts)
                + rng.choice(heredocs)
            )
            assert is_denied(cmd) is not None, cmd

    def test_fuzz_remote_plain_transport_stays_allowed(self):
        # Guard the other side: a plain transport to a remote host with no
        # disagreeing here-document shape is still allowed, so the fail-closed
        # rule did not swallow ordinary remote use.
        rng = random.Random(0x1234)  # fixed seed: deterministic
        remotes = ["ssh://github.com/r", "github.com:/x", "git.example.org:/x"]
        verbs = ["git clone ", "tar -xf "]
        opts = ["", "sudo -H "]
        for _ in range(500):
            cmd = opts[rng.randrange(len(opts))] + rng.choice(verbs) + rng.choice(remotes)
            assert is_denied(cmd) is None, cmd


def test_case_replay_keeps_each_distinct_inherited_environment_once(monkeypatch):
    from kiro_crew.security import argv_floor_git_tar as floor

    observed = []
    original = floor._git_command_targets_self

    def record(args, env):
        if args == ["status"]:
            observed.append(tuple((name, tuple(values)) for name, values in sorted(env.items())))
        return original(args, env)

    monkeypatch.setattr(floor, "_git_command_targets_self", record)
    scripts = []
    for assignments, tag in [
        ("a=one b=two", "AAA"),
        ("b=two a=one", "aaa"),
        ("a=other b=two", "AAA"),
    ]:
        command = f"git status; echo {tag}"
        for _ in range(2):
            command = "sh -c " + shlex.quote(command)
        scripts.append(assignments + " " + command)
    command = "; ".join(scripts)
    assert not floor._is_git_tar_to_self(command.lower(), raw_text=command)
    # Three written payload frames, each read under both boundary splits.
    assert Counter(observed) == {
        (("a", ("one",)), ("b", ("two",))): 6,
        (("a", ("other",)), ("b", ("two",))): 6,
    }
    assert is_denied(command) is None


def test_parallel_reexports_keep_the_shared_word_cap_patchable(monkeypatch):
    from kiro_crew.security import argv_floor, argv_floor_git_tar, argv_floor_git_tar_parallel

    for name in (
        "_parallel_replacements",
        "_git_lead_without_paths",
        "_rewrites_any",
        "_parallel_runs",
    ):
        assert getattr(argv_floor, name) is getattr(argv_floor_git_tar_parallel, name)
        assert getattr(argv_floor_git_tar, name) is getattr(argv_floor_git_tar_parallel, name)
    monkeypatch.setattr(argv_floor, "_GIT_TAR_WORD_CAP", 0)
    assert argv_floor_git_tar._parallel_runs(["{}", ":::", "value"]) is None


@pytest.mark.parametrize("launches", [12, 24])
def test_environment_signatures_only_visit_incoming_launches(monkeypatch, launches):
    from kiro_crew.security import argv_floor_git_tar as floor

    signature_sizes = []
    original_sorted = sorted

    def count_signature(items, *args, **kwargs):
        if isinstance(items, type({}.items())):
            signature_sizes.append(len(items))
        return original_sorted(items, *args, **kwargs)

    monkeypatch.setattr(floor, "sorted", count_signature, raising=False)
    command = "export " + " ".join(f"v{i}=x" for i in range(20)) + "; "
    command += "; ".join(f'x={i} sh -c "git status"' for i in range(launches))
    assert is_denied(command) is None
    # Each boundary split signs each incoming environment once, never its retained history.
    assert signature_sizes == [21] * (2 * launches)


@pytest.mark.parametrize("exports", [0, 1000])
def test_case_replay_has_one_work_budget(monkeypatch, exports):
    from kiro_crew.security import argv_floor_git_tar as floor

    reads = 0
    original = floor._git_command_targets_self

    def record(args, env):
        nonlocal reads
        reads += 1
        return original(args, env)

    monkeypatch.setattr(floor, "_git_command_targets_self", record)
    scripts = []
    for i in range(256):
        tag = "".join(c.upper() if i & (1 << j) else c for j, c in enumerate("abcdefgh"))
        scripts.append(f'x={i} sh -c "git status; echo {tag}"')
    command = "; ".join(scripts)
    if exports:
        command = "export " + " ".join(f"v{i}=x" for i in range(exports)) + "; " + command
    assert _denied_by(command) == _RULE
    # Every git reading costs at least its command words times its environment names.
    assert reads <= floor._GIT_TAR_REPLAY_WORK_CAP // (2 * (exports + 1))


def test_case_replay_refuses_before_reading_past_its_budget(monkeypatch):
    from kiro_crew.security import argv_floor_git_tar as floor

    command = "bash -c 'git log --format=%H' && bash -c 'git log --format=%h'"
    assert is_denied(command) is None
    monkeypatch.setattr(floor, "_GIT_TAR_REPLAY_WORK_CAP", 1)
    assert _denied_by(command) == _RULE
