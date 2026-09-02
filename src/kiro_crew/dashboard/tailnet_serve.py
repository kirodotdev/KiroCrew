"""Publish (and unpublish) the dashboard on this machine's tailnet.

The write half of tailnet access. Until this module existed, Kiro Crew never ran
``tailscale serve`` anywhere: the config switch made the gateway *trust* the
tailnet origin, but actually putting the dashboard on the tailnet was a command
the operator had to know and type. So the promised one-command experience was
really two commands, one of them undocumented in the UI, and skipping it produced
a working-looking switch that changed nothing observable.

Deliberately a **separate module from** :mod:`kiro_crew.dashboard.tailnet`, whose
documented contract is the opposite of what a write path needs:

* ``tailnet`` is read-only enrichment and **swallows every failure** — a missing
  binary, a stopped daemon and a timeout all collapse to ``None``, because its
  caller only wants "a name or nothing" and must not be able to break startup.
* Here a failure is the **entire point of the call**. ``tailscale serve`` refuses
  for reasons the operator can act on and cannot guess — most often because
  changing serve config needs root or an ``--operator`` grant — so collapsing
  those to a bare "failed" would reproduce the unexplained-refusal problem this
  feature exists to remove.

Two consequences of never having seen this daemon's real output on a live tailnet
(this repo's dev host has no Tailscale) shape the code, and both are deliberate
rather than provisional:

**The daemon's own output is always passed through verbatim.** ``code`` is a
best-effort classification for the UI to branch on; ``detail`` carries what
Tailscale actually said — stderr first, but stdout too, because upstream prints
some of its most actionable messages there (the Serve enablement URL among
them), and a timeout hands over whatever was captured before the deadline. If
the classification is wrong or the phrasing changes upstream, the operator
still sees the real reason instead of our guess at it.

**Published-state detection does not depend on the JSON schema.** Rather than
reading key paths from ``tailscale serve status --json`` that are unverified here,
:func:`serve_state` searches the parsed document for a proxy target naming our own
port. That is robust to a schema this code has never observed, and an
unrecognisable document reports ``unknown`` rather than ``not published`` — the
difference matters, because "not published" invites a publish the node may not
need while ``unknown`` says plainly that we could not tell.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from typing import Any, Literal

from kiro_crew.dashboard.tailnet import _cli_path, is_governance_pinned_off
from kiro_crew.sandbox import scrub_env

logger = logging.getLogger(__name__)

#: A publish/unpublish is a daemon round trip, not a local read, and ``--bg``
#: returns as soon as the config is accepted. Longer than the read path's ceiling
#: because this one is operator-initiated and a premature timeout would be
#: reported as a failure of an action that actually succeeded.
_WRITE_TIMEOUT_SECS = 15.0

#: Read of the current serve config. Local, so the read path's ceiling is right.
_READ_TIMEOUT_SECS = 5.0

#: The HTTPS port ``tailscale serve`` fronts. 443 is Tailscale's own default for
#: ``serve`` and the reason the derived origin carries no port component — a
#: browser omits ``:443`` from ``Origin``, so ``build_allowed_origins`` adds a
#: bare ``https://<name>``. Changing this would silently stop the origin from
#: matching, so it is pinned here rather than parameterised.
SERVE_HTTPS_PORT = 443

#: The mount our handler lives at. ``publish`` passes no ``--set-path``, and
#: upstream defaults the mount to ``/`` in that case (``serve_v2.go`` →
#: ``cleanURLPath(e.setPath)``).
#:
#: Withdrawal passes it **explicitly**, and that is load-bearing rather than
#: cosmetic. Upstream's ``unsetServe`` treats an absent ``--set-path`` as "every
#: mount under this port": it collects all of them and removes them all — so a
#: port-wide ``off`` deletes sibling handlers an operator added by hand. With the
#: mount named, upstream removes exactly that one. It also avoids a second
#: hazard: the multi-mount path prompts interactively
#: (``prompt.YesNo("Are you sure you want to delete N handlers…")``) unless
#: ``--yes`` is passed, and this command has no TTY to answer with.
SERVE_MOUNT = "/"

ResultCode = Literal[
    "ok",
    "governance_pinned",
    "no_cli",
    "no_permission",
    "daemon_unavailable",
    "timeout",
    "not_ours",
    "failed",
]


@dataclass(frozen=True)
class ServeResult:
    """Outcome of a publish/unpublish attempt.

    ``code`` is for branching, ``detail`` is for the human. ``detail`` includes
    the daemon's own output whenever there was any — stderr first, stdout when it
    carries the reason — because this module's whole reason to exist separately
    from the read path is that it must not invent a reason or hide the real one.
    """

    ok: bool
    code: ResultCode
    detail: str


@dataclass(frozen=True)
class ServeState:
    """What the daemon currently reports about serve, as far as we can tell.

    Both flags are deliberately three-valued (``True`` / ``False`` / ``None``):
    ``None`` means we could not determine it — no CLI, daemon not answering, or a
    status document whose shape this code does not recognise. Rendering that as
    ``False`` would be the "checked-but-never-ran shown as a clean result" defect
    this repo already has a lesson about.

    The two are separate because the negatives are NOT interchangeable, and
    :func:`unpublish` has to tell them apart before it removes anything:

    * ``published`` — serve is fronting **this dashboard's** port.
    * ``configured`` — serve has **some** configuration, whoever it belongs to.

    ``published=False, configured=True`` is the dangerous middle: something is
    served here and it is not ours, so a blind withdrawal could delete a mapping
    the operator set up by hand.
    """

    published: bool | None
    configured: bool | None
    detail: str


def _stream_text(stream: str | bytes | None) -> str:
    """Normalize a ``TimeoutExpired`` stream attribute to ``str``.

    ``subprocess.run(text=True)`` decodes the streams on the success path, but a
    ``TimeoutExpired`` carries whatever ``communicate`` had at the deadline:
    ``None`` when nothing was captured, ``bytes`` on POSIX (the exception is
    raised below the text layer), ``str`` on Windows (``run`` re-reads the pipes
    after killing the child). Decoded with ``errors="replace"`` because the
    deadline can split a multibyte sequence, and a mangled character beats a
    dropped reason.
    """
    if stream is None:
        return ""
    if isinstance(stream, bytes):
        return stream.decode("utf-8", errors="replace")
    return stream


#: Ceiling on how much recovered daemon output is spliced into a ``detail``
#: string. The interesting part (a refusal, the Serve enablement URL) leads the
#: stream, while the pathological case — a status read that timed out mid-way
#: through a multi-KB JSON document — would otherwise turn a one-line refusal
#: into a raw dump in the CLI and the mobile handler's JSON response.
_DETAIL_MAX_CHARS = 1000


def _daemon_output(*, out: str, err: str) -> str:
    """Whatever the daemon said, wherever it said it.

    stderr leads because that is where failure text belongs, but upstream prints
    some of its most actionable messages to STDOUT — on a tailnet where Serve is
    not enabled, ``tailscale serve`` prints the enablement URL there and then
    blocks — so stdout is kept too: it follows stderr when both carry text and
    stands alone when stderr is empty. Building ``detail`` from stderr alone
    dropped exactly that URL. Keyword-only, because with two same-typed string
    parameters a swapped call site would silently invert the precedence.
    """
    err = (err or "").strip()
    out = (out or "").strip()
    text = f"{err}\n{out}" if err and out else (err or out)
    if len(text) > _DETAIL_MAX_CHARS:
        return text[:_DETAIL_MAX_CHARS] + " …"
    return text


def _run(args: list[str], timeout: float) -> tuple[int, str, str]:
    """Run the CLI. Returns ``(returncode, stdout, stderr)``.

    Three synthetic return codes stand for the three ways this can fail before the
    CLI produces an exit status, and they are kept **distinct** because each needs
    different words to the operator:

    * ``-1`` — no binary at any vetted path. "Tailscale is not installed here."
    * ``-2`` — timed out, with whatever the child wrote before the deadline in
      ``stdout``/``stderr``. On a Serve-disabled tailnet the command prints the
      enablement URL and then blocks forever, so the timeout is the only
      reachable outcome and the captured output IS the diagnosis.
    * ``-3`` — the binary exists but could not be launched (``OSError``), with the
      OS's own message in ``stderr``. Collapsing this into ``-1`` told the operator
      "tailscale was not found" about a binary that is right there — the misleading
      diagnostic this module exists to avoid. A Windows CI run produced exactly
      that, so it is a real path, not a hypothetical.

    Binary resolution and environment scrubbing are **shared with the read path**
    rather than re-implemented: ``_cli_path`` accepts only vetted absolute paths
    and never consults ``PATH`` (a writable ``PATH`` entry would make the binary
    attacker-selectable), and ``scrub_env`` keeps the gateway's credentials out of
    the child. A second, subtly different copy of either is how one spawn comes to
    be hardened and its sibling not.
    """
    cli = _cli_path()
    if not cli:
        return -1, "", ""
    try:
        proc = subprocess.run(  # noqa: S603 - vetted absolute binary, fixed argv, no shell
            [cli, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=scrub_env(),
        )
    except subprocess.TimeoutExpired as exc:
        return -2, _stream_text(exc.stdout), _stream_text(exc.stderr)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("tailscale %s failed to run: %s", " ".join(args), exc)
        return -3, "", str(exc)
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _classify(output: str) -> ResultCode:
    """Best-effort code for a non-zero exit. Never the only thing reported.

    Matching on message text is inherently fragile — upstream owns this wording
    and can change it — so this only ever *adds* a hint on top of the verbatim
    output the caller also surfaces. Callers feed it the LEADING stream only
    (stderr, or stdout when stderr is empty), never the concatenation: with both
    streams in view, incidental stdout text containing "operator" would flip a
    daemon-down failure to ``no_permission`` and attach a confidently wrong
    remedy. The two codes worth separating are the ones with different remedies:
    a permission problem needs ``sudo`` or an ``--operator`` grant, while an
    unreachable daemon needs it started or logged in.
    """
    low = output.lower()
    if any(
        s in low
        for s in ("access denied", "permission denied", "must be run as root", "operator")
    ):
        return "no_permission"
    if any(
        s in low
        for s in (
            "not running",
            "cannot connect",
            "connection refused",
            "logged out",
            "not logged in",
            # `tailscale serve` against a stopped daemon (`tailscale down`)
            # fails with exactly "Tailscale is stopped." (issue #7244). The
            # needle keeps the product name so an unrelated message that merely
            # ends "... is stopped" is not handed the start-Tailscale remedy.
            "tailscale is stopped",
        )
    ):
        return "daemon_unavailable"
    return "failed"


def _find_proxy_target(node: Any, needles: tuple[str, ...]) -> bool:
    """Whether any string anywhere in *node* is one of *needles*.

    A structure-agnostic search, for the reason the module docstring gives: the
    exact schema of ``tailscale serve status --json`` is unverified here, so
    reading a key path would be a guess that fails silently (reporting "not
    published" for a node that is). Walking values instead only requires that the
    proxy target appear *somewhere* in the document, which is true of any shape
    that records it at all.
    """
    if isinstance(node, str):
        return node.rstrip("/") in needles
    if isinstance(node, dict):
        return any(_find_proxy_target(v, needles) for v in node.values())
    if isinstance(node, list):
        return any(_find_proxy_target(v, needles) for v in node)
    return False


def _port_scoped_subtrees(node: Any, port: int) -> list[Any]:
    """Subtrees whose own dict key names *port*, at any depth.

    Needed because "is this dashboard served *anywhere*" is the wrong question for
    :func:`unpublish`, and answering it was a real defect: a dashboard published on
    HTTPS 8443 with an unrelated service on 443 made a document-wide search say
    "ours", and the withdrawal then removed the unrelated 443 mapping.

    Still deliberately key-*shape*-agnostic rather than key-*path*-aware: it
    accepts ``"443"`` and any key ending ``":443"`` (``"desk.tail.ts.net:443"``,
    ``"*:443"``) wherever they appear, instead of hardcoding container names this
    code has never seen in a real document. What it does assume is that a mapping
    on 443 records 443 in a key somewhere — which holds for anything we published,
    since :func:`publish` passes ``--https=443``.

    When that assumption does not hold the result is an empty list, which the
    caller turns into ``unknown`` and therefore a REFUSED withdrawal. That is the
    safe direction: the cost is one copy-pasted command, not a deleted mapping.
    """
    found: list[Any] = []
    if isinstance(node, dict):
        for k, v in node.items():
            key = str(k)
            if key == str(port) or key.endswith(f":{port}"):
                found.append(v)
            found.extend(_port_scoped_subtrees(v, port))
    elif isinstance(node, list):
        for v in node:
            found.extend(_port_scoped_subtrees(v, port))
    return found


def _mount_subtrees(node: Any, mount: str) -> list[Any]:
    """Subtrees whose own dict key is *mount*, at any depth.

    The mount-level twin of :func:`_port_scoped_subtrees`, and needed for the same
    reason at one level deeper: withdrawal removes a single handler, so ownership
    has to be decided for that handler rather than for the port. Keyed on the exact
    mount string because upstream stores handlers in a map keyed by the cleaned URL
    path (``ServeConfig.Web[host:port].Handlers["/"]``), so ``"/"`` is a literal
    key rather than something to pattern-match.

    An empty list means we could not find it, which the caller turns into
    ``unknown`` and therefore a refused withdrawal.
    """
    found: list[Any] = []
    if isinstance(node, dict):
        for k, v in node.items():
            if str(k) == mount:
                found.append(v)
            found.extend(_mount_subtrees(v, mount))
    elif isinstance(node, list):
        for v in node:
            found.extend(_mount_subtrees(v, mount))
    return found


def serve_state(port: int) -> ServeState:
    """Whether the dashboard on *port* is currently published via serve."""
    rc, out, err = _run(["serve", "status", "--json"], _READ_TIMEOUT_SECS)
    if rc == -1:
        return ServeState(
            None, None, "The tailscale CLI was not found in a standard install location."
        )
    if rc == -2:
        said = _daemon_output(out=out, err=err)
        detail = "The tailscale CLI did not respond in time."
        if said:
            detail += f" Before the deadline it printed: {said}"
        return ServeState(None, None, detail)
    if rc == -3:
        return ServeState(
            None, None, f"The tailscale CLI could not be launched: {(err or '').strip()}"
        )
    if rc != 0:
        return ServeState(
            None, None, _daemon_output(out=out, err=err) or f"tailscale serve status exited {rc}"
        )
    try:
        doc = json.loads(out or "")
    except ValueError:
        # An empty document is what a node with no serve config returns, and that
        # is a genuine "nothing configured" rather than an unknown. Anything else
        # unparseable is unknown.
        if not (out or "").strip():
            return ServeState(False, False, "No serve configuration is active.")
        # ``configured=True``: the daemon DID answer, we just cannot read its shape.
        # That distinction is load-bearing for the publish/withdraw guards — it
        # separates "something is there that this build cannot attribute" (dangerous,
        # refuse) from "the daemon never answered" (a different failure, whose real
        # reason the write call itself will report).
        return ServeState(
            None, True, "tailscale serve status returned output this build cannot read."
        )
    if doc in (None, {}, []):
        return ServeState(False, False, "No serve configuration is active.")
    needles = (f"http://127.0.0.1:{port}", f"http://localhost:{port}")
    # Two narrowings, and each closes a way the previous predicate was wrong.
    #
    # Port: "is the dashboard served anywhere" answered yes for a dashboard on
    # 8443 while something else held 443, and the withdrawal then removed the
    # other thing.
    #
    # Mount: within 443, "is our target somewhere under here" answered yes when
    # ours was at `/foo` and a stranger's handler was at `/` — the mount we
    # actually remove. So the question is narrowed to exactly what withdrawal
    # touches: is the handler at SERVE_MOUNT, on SERVE_HTTPS_PORT, ours?
    scoped = _port_scoped_subtrees(doc, SERVE_HTTPS_PORT)
    if not scoped:
        return ServeState(
            None,
            True,
            f"Serve is configured, but this build could not identify what is on "
            f"port {SERVE_HTTPS_PORT}.",
        )
    mounts = [m for sub in scoped for m in _mount_subtrees(sub, SERVE_MOUNT)]
    if not mounts:
        return ServeState(
            None,
            True,
            f"Serve is configured on port {SERVE_HTTPS_PORT}, but this build could "
            f"not identify the handler at {SERVE_MOUNT}.",
        )
    if any(_find_proxy_target(m, needles) for m in mounts):
        return ServeState(
            True,
            True,
            f"Serve is proxying {SERVE_HTTPS_PORT}{SERVE_MOUNT} to the dashboard "
            f"on port {port}.",
        )
    return ServeState(
        False,
        True,
        f"Serve is configured on {SERVE_HTTPS_PORT}{SERVE_MOUNT}, but not for "
        f"this dashboard.",
    )


def publish(port: int, *, audit_tool: str = "tailnet_publish") -> ServeResult:
    """Publish the dashboard on this machine's tailnet over HTTPS.

    Runs ``tailscale serve --bg --https=<443> http://127.0.0.1:<port>``. The
    upstream target is **loopback on purpose**: serve runs on this same host, so
    nothing needs to listen on a non-loopback interface, and the gateway's bind
    is left exactly as it was. Publishing does not widen the socket — it hands the
    tailnet-facing TLS terminator a local address.

    Governed: this is the chokepoint for the *action*, alongside the derivation
    and the two config write paths. A ceiling pinning ``capabilities.tailnet_origin``
    off means the fleet forbids putting this host's dashboard on a tailnet, so the
    CLI is not spawned at all — refusing after publishing would be theatre.
    """
    if is_governance_pinned_off(audit_tool=audit_tool):
        return ServeResult(
            False,
            "governance_pinned",
            "Your administrator's security policy pins tailnet access off "
            "(capabilities.tailnet_origin). Nothing was published.",
        )
    # Symmetric to the withdrawal guard, and needed for the same reason: `serve
    # --bg --https=443 <target>` REPLACES whatever handler sits at that mount, so
    # publishing over an operator's own service loses their configuration exactly as
    # a port-wide `off` would have deleted it. Guarding only the removal side was an
    # asymmetry, not a decision.
    #
    # No binary at all: say so before the occupancy guard, or "tailscale is not
    # installed" would be reported as "could not confirm the mount is free".
    if _cli_path() is None:
        return ServeResult(
            False,
            "no_cli",
            "The tailscale CLI was not found in a standard install location, so "
            "nothing was published. Install Tailscale, or publish the dashboard "
            "yourself and set dashboard.url instead.",
        )
    # Proceed ONLY when the mount is explicitly free or already ours. Anything else
    # — including a state we could not determine — refuses, because the costs are not
    # symmetric: overwriting destroys configuration the operator rebuilds from
    # memory, while refusing costs one copy-pasted command, which the refusal prints.
    #
    # An earlier revision keyed this on ``configured is True``, reasoning that when
    # the daemon gives no usable answer the publish call would fail anyway and report
    # the authoritative error. That reasoning does not hold for a **timeout**: the
    # status read has a 5s ceiling and the write 15s, so a daemon slow enough to time
    # out the read can still accept the write — and then replace an existing handler.
    # "No answer" is not "no daemon", and the permissive branch existed largely
    # because it made this module's own failure-mode tests simpler.
    state = serve_state(port)
    _free = state.published is False and state.configured is False
    if not (state.published is True or _free):
        return ServeResult(
            False,
            "not_ours",
            f"{state.detail} Refusing to publish, because `tailscale serve` would "
            f"REPLACE whatever is at {SERVE_HTTPS_PORT}{SERVE_MOUNT} and this check "
            f"could not confirm it is free or already this dashboard. If you are "
            f"sure, run `tailscale serve --bg --https={SERVE_HTTPS_PORT} "
            f"http://127.0.0.1:{port}` yourself.",
        )
    rc, out, err = _run(
        ["serve", "--bg", f"--https={SERVE_HTTPS_PORT}", f"http://127.0.0.1:{port}"],
        _WRITE_TIMEOUT_SECS,
    )
    if rc == -1:
        return ServeResult(
            False,
            "no_cli",
            "The tailscale CLI was not found in a standard install location, so "
            "nothing was published. Install Tailscale, or publish the dashboard "
            "yourself and set dashboard.url instead.",
        )
    if rc == -2:
        # The most common way to land here is a Serve-disabled tailnet: the CLI
        # prints the enablement URL to stdout and then blocks waiting for the
        # capability, so the captured output carries the one thing the operator
        # needs and the timeout heading alone would hide it.
        said = _daemon_output(out=out, err=err)
        detail = (
            "tailscale serve did not respond in time. It may still have applied — "
            "check `kirocrew tailnet status` before retrying."
        )
        if said:
            detail += f" Before the deadline it printed: {said}"
        return ServeResult(False, "timeout", detail)
    if rc == -3:
        return ServeResult(
            False,
            "failed",
            "The tailscale CLI is installed but could not be launched: "
            + (err or "").strip(),
        )
    if rc != 0:
        said = _daemon_output(out=out, err=err)
        code = _classify(err.strip() or out.strip())
        hint = ""
        if code == "no_permission":
            # The single most likely refusal on Linux, and the one an operator
            # cannot guess: serve config is daemon state, so it needs root or a
            # standing grant for this user.
            hint = (
                " Changing serve configuration needs root or a standing grant: try "
                "`sudo tailscale serve …`, or grant this user once with "
                "`sudo tailscale set --operator=$USER`."
            )
        elif code == "daemon_unavailable":
            hint = " Check `tailscale status`; the daemon may be stopped or logged out."
        return ServeResult(False, code, (said or f"tailscale serve exited {rc}") + hint)
    return ServeResult(
        True,
        "ok",
        f"The dashboard is published on this machine's tailnet over HTTPS "
        f"(port {SERVE_HTTPS_PORT} → 127.0.0.1:{port}).",
    )


def revoke_if_governance_now_pins_off(port: int) -> None:
    """Withdraw a published tailnet origin when the ceiling has come to forbid it.

    Registered as a post-install hook on the central-distribution refresher, because the
    ``capabilities.tailnet_origin`` gate fires when :func:`publish` is CALLED — it is a
    chokepoint on the action, not a condition re-checked while serving. That was sound
    while the ceiling could only change at boot. With a live refresh it is not: a fleet
    that pins the capability off mid-flight would otherwise leave every already-published
    host serving its dashboard on the tailnet until someone restarted it, with the policy
    reporting the capability as denied the whole time.

    Narrow on purpose. It does nothing unless governance now denies the scope AND
    :func:`serve_state` confirms the handler is OURS, so a mapping an operator added by
    hand is never touched — the same ownership test :func:`unpublish` makes, for the same
    reason. Best-effort and never raises: it runs on the refresher thread, and a
    withdrawal that fails must not stop an installed ceiling being reported as installed.
    """
    # A PURE read first, with no ``audit_tool``: this runs on every confirming poll, and
    # ``is_governance_pinned_off``'s own contract is that auditing a mere inspection appends
    # HMAC-chained SEL rows at a multiple of the decisions that actually govern anything.
    if not is_governance_pinned_off():
        return
    state = serve_state(port)
    if state.published is not True:
        # False (not ours) or None (could not tell). Neither is a mandate to remove
        # something: the first is someone else's mapping, the second is the
        # checked-but-never-ran case this module already refuses to render as a result.
        return
    # Now that there IS something to withdraw, ask again THROUGH the audited seam. This is
    # the decision that does something, and it needs a forensic record more than a
    # human-driven one does: nobody typed it, so the SEL row is the only place a reviewer
    # can see that the fleet's policy — not an operator — took this host off the tailnet.
    # ``unpublish`` cannot supply it: withdrawal is deliberately never gated there, so it
    # discards its own ``audit_tool``. One extra evaluation, on the acting path only, so the
    # per-poll cost the pure read above exists to avoid is unaffected.
    is_governance_pinned_off(audit_tool="tailnet_governance_revoke")
    logger.warning(
        "the security policy now pins capabilities.tailnet_origin off; withdrawing this "
        "host's published dashboard origin"
    )
    result = unpublish(port, audit_tool="tailnet_governance_revoke")
    if not result.ok:
        logger.error(
            "could not withdraw the published tailnet origin after a policy tightening "
            "(%s); it is still served until this host is restarted",
            result.code,
        )


def unpublish(port: int, *, audit_tool: str = "tailnet_unpublish") -> ServeResult:
    """Stop serving the dashboard on the tailnet — **only if 443 is ours**.

    Narrow in two ways that earlier revisions of this function only *claimed* to be.

    **The removal names its mount.** Upstream's ``unsetServe`` treats an absent
    ``--set-path`` as "every mount under this port" — it collects all handlers and
    deletes them — so a port-wide ``off`` destroys sibling handlers an operator
    added by hand, and prompts interactively when there is more than one (which a
    CLI with no TTY cannot answer). Passing ``--set-path`` removes exactly the
    handler we created.

    **Ownership is decided for that mount, not for the port.** ``serve_state`` must
    confirm the handler at ``SERVE_MOUNT`` on ``SERVE_HTTPS_PORT`` is this
    dashboard; "ours is somewhere under 443" was true while a stranger's handler sat
    at the mount actually being removed.

    An **undetermined** state refuses too, and that is the deliberate half. This
    code has never seen a real ``tailscale serve status --json``, so "I could not
    tell" must not be treated as "go ahead": the two costs are not symmetric —
    wrongly proceeding destroys configuration the operator has to rebuild from
    memory, while wrongly refusing costs one copy-pasted command, which the
    refusal prints.

    Still **not gated on governance**, unchanged and for the unchanged reason:
    ``is_governance_pinned_off`` returns true both for a real deny and for a
    ceiling it could not evaluate, so gating withdrawal would let a transient
    policy-read failure leave a dashboard published with no supported way to take
    it down — a fail-closed control failing open in effect.
    """
    del audit_tool  # accepted for call-site symmetry; withdrawal is never gated
    state = serve_state(port)
    if state.published is False and state.configured is False:
        # Idempotent no-op: nothing is served at all, so there is nothing to
        # withdraw and nothing to endanger. Reported as success because the
        # caller's goal ("not published") already holds.
        return ServeResult(
            True, "ok", "Nothing is published — no serve configuration is active."
        )
    if state.published is not True:
        return ServeResult(
            False,
            "not_ours",
            f"{state.detail} Refusing to withdraw, because this check could not "
            f"confirm that {SERVE_HTTPS_PORT}{SERVE_MOUNT} is this dashboard. If "
            f"you are sure, run `tailscale serve --https {SERVE_HTTPS_PORT} "
            f"--set-path={SERVE_MOUNT} off` yourself.",
        )
    rc, out, err = _run(
        [
            "serve",
            "--https",
            str(SERVE_HTTPS_PORT),
            f"--set-path={SERVE_MOUNT}",
            "off",
        ],
        _WRITE_TIMEOUT_SECS,
    )
    if rc == -1:
        return ServeResult(False, "no_cli", "The tailscale CLI was not found; nothing to do.")
    if rc == -2:
        said = _daemon_output(out=out, err=err)
        detail = "tailscale serve did not respond in time."
        if said:
            detail += f" Before the deadline it printed: {said}"
        return ServeResult(False, "timeout", detail)
    if rc == -3:
        return ServeResult(
            False, "failed", "The tailscale CLI could not be launched: " + (err or "").strip()
        )
    if rc != 0:
        said = _daemon_output(out=out, err=err)
        code = _classify(err.strip() or out.strip())
        # Mirrors the publish path's hint branch. It matters more here: a failed
        # withdrawal's verbatim output can read like a status line ("Tailscale
        # is stopped.") rather than like a failure, so without the appended hint
        # the operator has no way to tell that nothing was withdrawn (issue
        # #7244). Hints are appended to — never replace — the daemon's words.
        hint = ""
        if code == "no_permission":
            hint = (
                " Nothing was withdrawn. Changing serve configuration needs root "
                "or a standing grant: try `sudo tailscale serve …`, or grant this "
                "user once with `sudo tailscale set --operator=$USER`."
            )
        elif code == "daemon_unavailable":
            hint = (
                " Nothing was withdrawn. Check `tailscale status`; the daemon may "
                "be stopped or logged out — bring Tailscale back up, then turn "
                "this off again."
            )
        return ServeResult(False, code, (said or f"tailscale serve exited {rc}") + hint)
    return ServeResult(
        True, "ok", "The dashboard is no longer published on this machine's tailnet."
    )
