"""KiroCrew pod — isolated, throwaway, full-stack test instances per worktree.

A *pod* is an ephemeral KiroCrew gateway booted from one feature worktree's own
``.venv``, on its own deterministic port, with its own ``KIROCREW_HOME`` (own DB /
sessions / memory), no Slack tunnel, ``--no-crons``, resource-capped, and
``rm -rf``'d on stop. It lets you test a worktree's full stack (backend ``/api/*``
+ the SPA bundle the gateway serves on the same port) **without touching the live
gateway or the shared ``~/.kiro/crew`` data**. Think ``kubectl`` for local worktree
test rigs.

This is the *test line* (multi-active, burn-on-evict). It is orthogonal to the
*live line* (a single gateway serving real data on the canonical port) and refuses
to ever bind the live port.

The user-facing surface is ``kirocrew pod <verb>`` (see :mod:`kiro_crew.pod.cli`):

    up <wt>        schedule an isolated pod for a worktree  -> {base_url, token}
    down <wt>      evict it (zero residue)
    ls             list running pods                         (kubectl get pods)
    status <wt>    up/down + health
    token <wt>     (re)mint a dashboard token for a running pod
    url <wt>       print its base_url
    logs <wt>      tail its journal
    provision <wt> build the worktree's venv + dist so it can be podded
    install        lay down the systemd template unit (once per machine)

A friendly worktree *name* is resolved to a checkout path git-natively (see
:func:`kiro_crew.pod.runtime.resolve_checkout`) and pinned so the systemd-booted
gateway never re-resolves. Mechanism, per platform:

* **Linux (``systemd --user``)**: one template unit ``kirocrew-pod@<wt>.service``
  whose ``ExecStart`` re-enters ``kirocrew pod _run <wt>`` (boots the worktree's own
  gateway) and whose ``ExecStopPost`` ``rm -rf``'s the pod's isolated HOME. cgroup
  ``MemoryMax``/``CPUQuota`` cap the pod.
* **macOS (``launchd``)**: one agent plist per pod under the pod plane's own directory (not
  ``~/Library/LaunchAgents`` — that would auto-resurrect pods at login)
  (launchd has no template units), bootstrapped into ``gui/<uid>``. Two capabilities
  have no equivalent: there is no post-stop hook, so ``pod down`` performs the HOME
  teardown and ``pod ls`` reports HOMEs left by a crash (reclaimed via ``pod down <name>``); and there are no
  cgroups, so **the resource ceiling is not enforced** — see
  :mod:`kiro_crew.pod.launchd` for why a weaker key is deliberately not emitted in
  its place. Logs go to files instead of the journal.

Nothing is shipped outside this Python package.
"""

from __future__ import annotations

from kiro_crew.pod.config import PodConfig
from kiro_crew.pod.runtime import (
    PodError,
    derive_port,
    pod_home,
    pod_unit,
    resolve_checkout,
)

__all__ = [
    "PodConfig",
    "PodError",
    "derive_port",
    "pod_home",
    "pod_unit",
    "resolve_checkout",
]
