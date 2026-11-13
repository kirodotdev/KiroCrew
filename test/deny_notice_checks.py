"""Checks the eval-subagent and task-runner deny-notice test files share, written once.

Each check lives here as a mixin. A test class in ``test_eval_subagent_deny_notice.py``
or ``test_taskrunner_deny_notice.py`` takes the mixin as a base, so the node id stays
``<file>::<Class>::<test>``, the class keeps its own setup, and pytest resolves the
test's fixtures from that file. A check that reads the file's own constants or helpers
does so through :func:`_file_module`.
"""

from __future__ import annotations

import sys
from types import ModuleType


def _file_module(test: object) -> ModuleType:
    """The test file the class running this check is defined in."""
    return sys.modules[type(test).__module__]


class EveryHostDenyIsPrecededByTheSharedSteer:
    def test_every_host_deny_is_preceded_by_the_shared_steer(self):
        here = _file_module(self)
        lines = here._lines(self.MODULE)
        bare: list[int] = []
        for i, site in self._walk(lines):
            assert site.verdict == "host"
            steers = [j for j in range(max(0, i - self.WINDOW), i) if here._STEER.match(lines[j])]
            if not steers:
                bare.append(i + 1)
        assert not bare, (
            "these host denies hand the model kiro-cli's generic 'user denied' with "
            f"nothing to correct it -- await _steer_host_deny(...) first: lines {bare}"
        )


class EveryRejectAuditsBeforeTheSteer:
    def test_every_reject_audits_before_the_steer(self):
        here = _file_module(self)
        lines = here._lines(self.MODULE)
        late: list[int] = []
        previous = -1
        for i, _site in self._walk(lines):
            floor = max(0, i - self.AUDIT_WINDOW, previous + 1)
            steers = [j for j in range(max(0, i - self.WINDOW), i) if here._STEER.match(lines[j])]
            audits = [j for j in range(floor, i) if here._AUDIT.match(lines[j])]
            if not audits or not steers or not audits[-1] < steers[-1]:
                late.append(i + 1)
            previous = i
        assert not late, (
            "the SEL row must be written before the steer and the reject, or a "
            f"stalled pipe cancels the coroutine with the decision unaudited: lines {late}"
        )


class EverySteerNamesItsCauseExplicitly:
    def test_every_steer_names_its_cause_explicitly(self):
        here = _file_module(self)
        lines = here._lines(self.MODULE)
        unnamed = [
            i + 1
            for i in here._matches(here._STEER, lines)
            if "cause=DENY_CAUSE_" not in "\n".join(lines[i : i + self.WINDOW])
        ]
        assert not unnamed, unnamed
