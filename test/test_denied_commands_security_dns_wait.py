"""The ssh-to-self floor on the first check of a new dotted name.

Off the event loop, the first check waits a short, bounded time for the DNS lookup it
started, so a remote name is allowed on the first try. A loopback answer, a failed
lookup and a lookup still running when the wait ends stay refused. The same wait covers
the destination of a glob that bash runs (``ss* README.md``). A glob that is another
program's operand (``git add s* notes.txt``) is a file name: the words after it are
never looked up. Every lookup here is a stub: no test reaches the network.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading

import pytest
from denied_commands_security_helpers import _own_address_probe_stays_local  # noqa: F401
from denied_commands_security_helpers import _REAL_RESOLVED_HOST_VERDICT, _denied_by

from kiro_crew import security
from kiro_crew.security import argv_floor as _argv_floor
from kiro_crew.security import argv_floor_hosts as _argv_floor_hosts
from kiro_crew.security import ssh_config_route as _ssh_config_route
from kiro_crew.security.call_views import sharing_views
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS, wait_until

_RULE = "sandbox-escape-ssh-self"

_REMOTE = [(2, 1, 6, "", ("198.51.100.7", 0))]
_LOOPBACK = [(2, 1, 6, "", ("127.0.0.1", 0))]


@pytest.fixture
def dns(monkeypatch):
    """The real verdict layer over a stub resolver; yields the stub's controls.

    ``answers`` maps a name to its getaddrinfo result or to an exception to raise;
    a name in ``hold`` blocks until ``release`` is set. ``asked`` records every
    name looked up. Each test ends with every worker released and joined.
    """
    own = security.socket.gethostname().strip().lower()
    monkeypatch.setattr(
        _argv_floor, "_OWN_HOST_NAMES_CACHE", frozenset({own, own.split(".", 1)[0]})
    )
    monkeypatch.setattr(_argv_floor, "_OWN_HOST_RESOLVE_DONE", True)
    monkeypatch.setattr(_argv_floor, "_NETLINK_ADDRS_PUBLISHED", True)
    assert _REAL_RESOLVED_HOST_VERDICT is not None
    monkeypatch.setattr(_argv_floor, "_resolved_host_verdict", _REAL_RESOLVED_HOST_VERDICT)
    monkeypatch.setattr(_argv_floor, "_HOST_VERDICT_CACHE", {})
    monkeypatch.setattr(_argv_floor, "_HOST_VERDICT_STAMP", {})
    monkeypatch.setattr(_argv_floor, "_HOST_VERDICT_PENDING", set())
    # The wait ends on the worker's signal, never on the clock: a loaded runner
    # cannot turn an allow into a refusal.
    monkeypatch.setattr(
        _argv_floor, "_HOST_VERDICT_WAIT_SECS", FALLBACK_TIMEOUT_SECS, raising=False
    )
    release = threading.Event()
    state: dict = {"answers": {}, "hold": set(), "asked": [], "release": release}
    workers: "list[threading.Thread]" = []
    real_thread = threading.Thread

    def _gai(host, *args, **kwargs):
        state["asked"].append(host)
        if host in state["hold"]:
            release.wait(FALLBACK_TIMEOUT_SECS)
        answer = state["answers"].get(host)
        if answer is None:
            raise socket.gaierror(socket.EAI_AGAIN, "temporary failure")
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def _thread(*args, **kwargs):
        worker = real_thread(*args, **kwargs)
        if kwargs.get("name") == "kirocrew-host-verdict":
            workers.append(worker)
        return worker

    monkeypatch.setattr(_argv_floor_hosts.socket, "getaddrinfo", _gai)
    monkeypatch.setattr(_argv_floor_hosts.threading, "Thread", _thread)
    yield state
    release.set()
    for worker in workers:
        worker.join(FALLBACK_TIMEOUT_SECS)


def test_a_remote_name_is_allowed_on_the_first_check(dns):
    dns["answers"]["github.com"] = _REMOTE
    assert _denied_by("ssh -T git@github.com") is None
    assert dns["asked"] == ["github.com"]


def test_a_loopback_alias_is_refused_on_the_first_check(dns):
    dns["answers"]["self.alias.example"] = _LOOPBACK
    assert _denied_by("ssh self.alias.example id") == _RULE
    assert _denied_by("ssh self.alias.example id") == _RULE


def test_a_failed_lookup_stays_refused(dns):
    # EAI_AGAIN is a temporary failure: nothing is cached and every check refuses.
    assert _denied_by("ssh flaky.example id") == _RULE
    assert _denied_by("ssh flaky.example id") == _RULE


def test_a_lookup_still_running_when_the_wait_ends_is_refused(dns, monkeypatch):
    # A spent wait budget with the lookup still held: the check refuses.
    monkeypatch.setattr(_argv_floor, "_HOST_VERDICT_WAIT_SECS", 0, raising=False)
    dns["answers"]["slow.example"] = _REMOTE
    dns["hold"].add("slow.example")
    assert _denied_by("ssh slow.example id") == _RULE
    # The answer that lands later is used by the next check.
    dns["release"].set()
    wait_until(lambda: "slow.example" not in _argv_floor_hosts._HOST_VERDICT_PENDING)
    assert _denied_by("ssh slow.example id") is None


def test_the_event_loop_never_waits(dns):
    # The lookup answers at once, so a check that waited would allow; on the
    # loop the first check answers before the lookup and refuses.
    dns["answers"]["github.com"] = _REMOTE

    async def _on_loop():
        return _denied_by("ssh -T git@github.com")

    assert asyncio.run(_on_loop()) == _RULE


def test_one_check_shares_one_wait_budget(manual_clock, monkeypatch):
    manual_clock.install(monkeypatch, _argv_floor_hosts)
    budget = _argv_floor_hosts._HOST_VERDICT_WAIT_SECS
    assert _argv_floor_hosts._host_verdict_wait_left() == budget

    @sharing_views
    def _one_check() -> "list[float]":
        first = _argv_floor_hosts._host_verdict_wait_left()
        manual_clock.advance(0.3)
        second = _argv_floor_hosts._host_verdict_wait_left()
        manual_clock.advance(budget)
        return [first, second, _argv_floor_hosts._host_verdict_wait_left()]

    first, second, third = _one_check()
    assert first == pytest.approx(budget)
    assert second == pytest.approx(budget - 0.3)
    assert third <= 0


@pytest.mark.parametrize(
    "cmd",
    [
        "git add s* README.md",
        "git add ss* README.md",
        "/usr/bin/git add s* README.md",
        "GIT_DIR=.git git add s* README.md",
    ],
)
def test_a_file_name_after_a_glob_is_allowed_on_the_first_check(dns, cmd):
    # ``ss*`` can name ``ssh``, but as an operand of ``git add`` it is a file
    # name, so the next word is checked against this host's own names only.
    dns["answers"]["readme.md"] = _REMOTE
    assert _denied_by(cmd) is None, cmd


@pytest.mark.parametrize(
    "cmd",
    [
        "git add ss* localhost",
        "ss* self.alias.example id",
        "find . -maxdepth 0 -exec s?h self.alias.example id ;",
        "cd /tmp;s?h self.alias.example id",
        "true&&s?h self.alias.example id",
        "timeout 5 s?h self.alias.example id",
        "xargs s?h self.alias.example",
        "s?h self.alias.example id",
        # The floor leaves the DNS check on unless the glob is positively a file
        # operand, so a runner the floor does not know still refuses an ssh verb
        # it launches (GPT/Opus BLOCK on an earlier head that keyed on "not a
        # known program").
        "sshpass -p x s?h self.alias.example id",
        "sudo -H find . -exec s?h self.alias.example id ;",
        "case x in x) find . -maxdepth 0 -exec /usr/bin/s?h self.alias.example id ; ;; esac",
        # tar is not a file-operand command (it runs -I/--rsh-command values),
        # so its operand glob keeps the DNS check -- the duplicate-list case.
        "tar czf out.tgz s?h self.alias.example",
        "tar --rsh-command /usr/bin/s?h -tf self.alias.example:/tmp/a.tar",
        # A wrapper that misreads an option must not land the file-operand
        # reading on a member program: git is required at the command start.
        "systemd-run --user --description cat s?h self.alias.example id",
        # A git global option whose value is a subcommand word, or a mixed-case
        # alias, must not be read as the built-in subcommand: the subcommand has
        # to be the word right after git, lowercase.
        "git --namespace add s?h self.alias.example id",
        "git -C /tmp add s?h self.alias.example id",
        "git --git-dir d add s?h self.alias.example id",
        "git --work-tree w add s?h self.alias.example id",
        "git -c alias.x=y add s?h self.alias.example id",
        "git ADD s?h self.alias.example id",
        # A substitution body is re-read from lowered text, so the git reading
        # (which needs the original case) does not apply inside one: a mixed-case
        # alias there keeps the DNS check.
        'echo "$(git ADD /usr/bin/s?h self.alias.example id)"',
        "echo `git ADD /usr/bin/s?h self.alias.example id`",
        # A separator glued inside an operand word keeps it in the git argv, but
        # bash runs the ssh verb after the separator as its own command: the git
        # operand scan must stop at the separator and leave that command's DNS
        # check on.
        "git add x;s?h self.alias.example id",
        "git add x|s?h self.alias.example id",
        "git add x& s?h self.alias.example id",
        "git add x &&s?h self.alias.example id",
        "git add x||s?h self.alias.example id",
        # A git command inside a leading substitution is not the program: bash
        # expands it to nothing and runs the ssh glob that follows.
        "$(git add y) /usr/bin/s?h self.alias.example id",
        "`git add y` /usr/bin/s?h self.alias.example id",
        "$(git add -A) env s?h self.alias.example id",
        "$(cd /tmp; git add x) /usr/bin/s?h self.alias.example id",
        "GIT add s?h self.alias.example id",
        # Only ``add`` is a file subcommand; the others keep the DNS check.
        "git rm --cached s?h self.alias.example id",
        "git submodule s?h self.alias.example id",
        "git bisect run s?h self.alias.example id",
        "git difftool s?h self.alias.example id",
        # The exemption is granted only to a line that is ONE simple ``git add``.
        # An earlier word on the line can change what ``git`` runs (a function,
        # an alias), so any separator, group, expansion or redirect keeps the
        # DNS check.
        'git(){ "${@:2}"; }; git add /usr/bin/s[s]h self.alias.example id',
        'git () { "${@:2}"; }; git add /usr/bin/s?h self.alias.example id',
        'function git { "${@:2}"; }; git add /usr/bin/s?h self.alias.example id',
        "alias git=env; git add s?h self.alias.example id",
        "git add s?h self.alias.example id $X",
        "git add ss* self.alias.example>out.log",
        # A prefix that chooses which program runs (``PATH``, ``GIT_EXEC_PATH``)
        # can make ``git`` something else, so only the repository-location
        # prefixes are accepted.
        "PATH=/tmp/x git add s?h self.alias.example id",
        "GIT_EXEC_PATH=/tmp/x git add s?h self.alias.example id",
        # Only bare git and the system install paths are git; any other path
        # could be a link to ssh. ``s?h`` names only ``ssh``; ``s*`` would make
        # the row follow the hash seed, as it does on main.
        "/tmp/x/git add s?h self.alias.example id",
    ],
)
def test_a_self_target_after_a_glob_is_still_refused(dns, cmd):
    dns["answers"]["self.alias.example"] = _LOOPBACK
    assert _denied_by(cmd) == _RULE, cmd


@pytest.mark.parametrize("route_proven, expected", [(True, None), (False, _RULE)])
def test_a_name_with_no_dns_record_gets_its_later_answer_on_the_first_check(
    dns, monkeypatch, route_proven, expected
):
    # No such name: the resolver worker asks ssh's effective config before it
    # publishes, so the wait covers that check too. A config that proves a remote
    # route allows on the first call; a clean config proves nothing and refuses
    # on every call. Either way the first answer is the answer later calls give.
    dns["answers"]["readme.md"] = socket.gaierror(socket.EAI_NONAME, "no such name")
    routed: "list[str]" = []

    def _route_is_not_self(host: str) -> bool:
        routed.append(host)
        return route_proven

    monkeypatch.setattr(_ssh_config_route, "route_is_not_self", _route_is_not_self)
    first = _denied_by("ss* README.md")
    assert first == expected
    assert routed == ["readme.md"]
    assert _denied_by("ss* README.md") == first


@pytest.mark.parametrize(
    "cmd",
    [
        "git add s* notes.txt",
        "git add ss* notes.txt",
        "git add s?h notes.txt",
    ],
)
def test_a_name_with_no_dns_record_after_an_operand_glob_is_allowed(dns, cmd):
    # The glob is an operand of ``git add``, so it is a file name: the word
    # after it is never looked up, and a name with no DNS record is not refused.
    dns["answers"]["notes.txt"] = socket.gaierror(socket.EAI_NONAME, "no such name")
    assert _denied_by(cmd) is None, cmd
    assert dns["asked"] == []


_SEED_SCRIPT = (
    "import json, sys\n"
    "from kiro_crew.security import argv_floor, is_denied\n"
    "argv_floor._OWN_HOST_NAMES_CACHE = frozenset()\n"
    "argv_floor._OWN_HOST_RESOLVE_DONE = True\n"
    "argv_floor._NETLINK_ADDRS_PUBLISHED = True\n"
    "argv_floor._OWN_HOST_RESOLVE_NEXT_TRY = float('inf')\n"
    # Every name that is looked up is refused, so a row is allowed only if the
    # floor never looks its file names up. Goes through the public gate (which
    # carries the original command text the git exemption needs), not the
    # text-only _is_ssh_to_self recursion entry.
    "argv_floor._resolved_host_verdict = lambda host, **_kw: True\n"
    "print(json.dumps([is_denied(c) is None for c in sys.argv[1:]]))\n"
)


def test_an_operand_glob_is_a_file_name_under_every_hash_seed(tmp_path):
    # Which verb ``s*`` names first follows PYTHONHASHSEED, which is fixed per
    # process, so only fresh interpreters with different seeds show it.
    cmds = ["git add s* notes.txt", "git add s?h notes.txt", "git add s* README.md"]
    for seed in range(6):
        result = subprocess.run(
            [sys.executable, "-c", _SEED_SCRIPT, *cmds],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=FALLBACK_TIMEOUT_SECS,
            cwd=str(tmp_path),
            env={**os.environ, "PYTHONHASHSEED": str(seed)},
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout.strip().splitlines()[-1]) == [True] * 3, seed
