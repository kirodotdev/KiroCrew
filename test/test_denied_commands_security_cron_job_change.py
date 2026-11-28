"""Tests that an agent's shell cannot delete or change a scheduled job.

``cron remove``, ``update``, ``pause`` and ``resume`` on the product CLI act on any job
in the store, whichever chat owns it, while the MCP cron tools reach only the calling
chat's jobs. The ``self-protection-cron-remove`` argv floor refuses those four verbs when
the product CLI is the argv's own program; the verbs that leave every existing job as it
is stay allowed, and so does a command that merely contains the words.
"""

from __future__ import annotations

import pytest
from denied_commands_security_helpers import _own_address_probe_stays_local  # noqa: F401
from denied_commands_security_helpers import _HYPH, _NAME

from kiro_crew import security
from kiro_crew.security import BUILTIN_DENIED_RULES, is_denied

# Each test starts with the macOS libc and libproc handle caches unbuilt and puts them
# back after, as every test of the test_denied_commands_security family does (see conftest.py).
pytestmark = pytest.mark.usefixtures("fresh_darwin_libc_handles")

_RULE = "self-protection-cron-remove"
_MODULE = "kiro_" + "crew"
#: The cron verbs that delete or change an existing job.
_VERBS = ("pause", "remove", "resume", "update")

#: One command per spelling an agent's shell accepts; ``{verb}`` is the cron verb.
_SPELLINGS = {
    "bare": _NAME + " cron {verb} job-1",
    "hyphenated-name": _HYPH + " cron {verb} job-1",
    "path-qualified": "/usr/local/bin/" + _NAME + " cron {verb} job-1",
    "upper-case": _NAME.upper() + " CRON {verb} job-1",
    "verbose-flag": _NAME + " -v cron {verb} job-1",
    "no-jail-flag": _NAME + " --no-jail cron {verb} job-1",
    "two-flags": _NAME + " -v --no-jail cron {verb} job-1",
    "quoted-verb": _NAME + ' cron "{verb}" job-1',
    "single-quoted-words": _NAME + " 'cron' '{verb}' job-1",
    "escaped-verb-letter": _NAME + " cron \\{verb} job-1",
    "escaped-flag": _NAME + " -\\v cron {verb} job-1",
    "redirect-between": _NAME + " cron >/dev/null {verb} job-1",
    "redirect-before": _NAME + " 2>&1 cron {verb} job-1",
    "redirect-glued": _NAME + " cron {verb}>/tmp/log job-1",
    "module-form": "python -m " + _MODULE + " cron {verb} job-1",
    "module-form-attached": "python3 -m" + _MODULE + " cron {verb} job-1",
    "module-form-interpreter-flag": "python3.12 -X dev -m " + _MODULE + " cron {verb} job-1",
    "nested-shell": "bash -c '" + _NAME + " cron {verb} job-1'",
    "after-and": "cd /tmp && " + _NAME + " cron {verb} job-1",
    "after-semicolon": "true; " + _NAME + " cron {verb} job-1",
    "in-a-loop": "for j in a b; do " + _NAME + " cron {verb} $j; done",
    "env-wrapper": "env " + _NAME + " cron {verb} job-1",
    "xargs-wrapper": _NAME + " cron list | xargs -n1 " + _NAME + " cron {verb}",
    "help-form": _NAME + " cron {verb} --help",
}


def _floor_id(cmd: str) -> "str | None":
    """The id on the first line of ``cmd``'s refusal, or ``None`` when it is allowed."""
    reason = is_denied(cmd)
    if reason is None:
        return None
    head = reason.splitlines()[0]
    assert head.startswith(security.DENY_REASON_PREFIX), reason
    return head[len(security.DENY_REASON_PREFIX) :]


class TestCronJobChangeIsRefused:
    def test_the_floor_reads_exactly_these_verbs(self):
        from kiro_crew.security import argv_floor_self

        assert argv_floor_self._SELF_CRON_JOB_CHANGE_VERBS == frozenset(_VERBS)

    @pytest.mark.parametrize("verb", _VERBS)
    @pytest.mark.parametrize("spelling", sorted(_SPELLINGS))
    def test_each_verb_is_refused_in_each_spelling(self, verb, spelling):
        cmd = _SPELLINGS[spelling].format(verb=verb)
        assert _floor_id(cmd) == _RULE, cmd

    def test_remove_by_managed_key_is_refused(self):
        assert _floor_id(_NAME + " cron remove --managed-by nightly") == _RULE

    @pytest.mark.parametrize("verb", _VERBS)
    def test_the_predicate_fires_on_the_plain_command(self, verb):
        assert security._is_self_cron_job_change(f"{_NAME} cron {verb} job-1")

    @pytest.mark.parametrize("verb", _VERBS)
    def test_the_refusal_has_no_opt_out(self, verb):
        """No catalog row, so no toggle: refused with every built-in rule disabled."""
        assert is_denied(f"{_NAME} cron {verb} job-1", denied_regexes=[]) is not None
        assert _RULE in security._SELF_PROTECTION_UNGATED_FLOOR_IDS
        assert _RULE not in {r.id for r in BUILTIN_DENIED_RULES}

    def test_the_refusal_names_the_cron_tools(self):
        """The second line is handed to the model, so it says what to use instead."""
        reason = is_denied(f"{_NAME} cron remove job-1")
        assert reason is not None
        head, note = reason.split("\n")[:2]
        assert head == f"{security.DENY_REASON_PREFIX}{_RULE}"
        assert note.startswith("Matched structurally on the command's argv")
        for tool in ("cron_remove", "cron_update", "cron_pause", "cron_resume"):
            assert tool in note, tool


class TestOtherCronCommandsStayAllowed:
    @pytest.mark.parametrize(
        "cmd",
        [
            # The verbs that leave every existing job as it is.
            _NAME + " cron list",
            _NAME + " cron list --folder update",
            _NAME + " cron history job-1 --status error",
            _NAME + " cron history --all",
            _NAME + " cron trigger job-1",
            _NAME + " cron preview ~/.kiro/crew/crons/f.py:run",
            _NAME + " cron",
            _NAME + " cron --help",
            _NAME + " -v cron list",
            "python -m " + _MODULE + " cron list",
            # A new job whose name or message holds one of the four words.
            _NAME + ' cron add tidy "remove old logs and update the doc" --every 600',
            _NAME + ' cron add pause "resume the build" --every 600',
            # The verb word belongs to a later command, not to the cron call.
            _NAME + " cron list && echo remove",
            _NAME + " cron list; echo update",
            _NAME + " cron list | grep pause",
            # The words as another program's data, a path or a title.
            "echo " + _NAME + " cron remove job-1",
            "grep -rn '" + _NAME + " cron remove' docs/",
            "ls ~/" + _NAME + "/cron/update.py",
            "git -C ~/" + _NAME + ' commit -m "fix(cron): update the pause path"',
            "gh pr create --repo org/" + _NAME + ' --title "fix(cron): resume a job"',
            "cat ~/" + _NAME + "/docs/cron.md  # remove later",
        ],
    )
    def test_allowed(self, cmd):
        assert is_denied(cmd) is None, cmd

    @pytest.mark.parametrize(
        "cmd",
        [
            _NAME + " cron list",
            _NAME + " cron add remove 'say hi' --every 60",
            _NAME + " cron trigger job-1",
            "echo " + _NAME + " cron remove job-1",
            _NAME + " > remove cron",
        ],
    )
    def test_the_predicate_does_not_fire(self, cmd):
        assert not security._is_self_cron_job_change(cmd)
