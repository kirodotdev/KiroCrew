"""KiroCrew pod — isolated, throwaway, full-stack test instances per worktree.

A *pod* is an ephemeral KiroCrew gateway booted from one feature worktree's own
``.venv``, on its own deterministic port, with its own ``KIROCREW_HOME`` (own DB /
sessions / memory), no Slack tunnel, ``--no-crons``, resource-capped, and
reclaimed by ``pod down``. It lets you test a worktree's full stack (backend ``/api/*``
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
  gateway). cgroup ``MemoryMax``/``CPUQuota`` cap the pod.
* **macOS (``launchd``)**: one agent plist per pod under the pod plane's own directory (not
  ``~/Library/LaunchAgents`` — that would auto-resurrect pods at login)
  (launchd has no template units), bootstrapped into ``gui/<uid>``. One capability
  has no equivalent: there are no cgroups, so **the resource ceiling is not
  enforced** — see :mod:`kiro_crew.pod.launchd` for why a weaker key is
  deliberately not emitted in its place. Logs go to files instead of the journal.

Reclaiming a pod's isolated HOME belongs to ``pod down`` on both platforms rather
than to a post-stop service hook, which on systemd ran before the final kill of
the pod's own cgroup and also fired on the stop half of a ``Restart=``. ``down``
stops the service, waits for its process tree to drain, deletes, and verifies;
``pod ls`` reports the HOMEs left by a pod that went away without one.

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
