"""Field ⑤ shared machinery — the provider-independent half of a draft recipe.

The GitHub and GitLab recipes differ only in the CLI they drive (``gh`` vs ``glab``),
the argv that opens a draft, the URL shape they parse back, and the SSH rewrite of an
authenticated remote. Everything safety-relevant — the durable queue copy, prose
redaction, branch naming, the push-policy gate, the credential scan of the pushable
range, and the narrowed one-ref push — is provider-independent and lives HERE, so the
two recipes cannot drift apart on the half that guards egress.

## Why the recipes need a push where the upstream recipe did not

The upstream review CLI uploaded the commit through a side channel that was not
the git remote — so it could draft a review from inside a push-disabled clone
without ever pushing. GitHub and GitLab have no such side channel: a PR/MR is
*defined* as a comparison between two refs that both exist on the remote, so the
fix branch must be pushed before the draft command can reference it.

That is a real relaxation of the app's #1 safety control, so it is narrowed the
same way the spine's F10 direct-commit mode narrows it (see
``spine/driver.py::_direct_push``):

  * the push targets a **generated, app-namespaced branch**
    (``auto-improvement/<kind>-<fingerprint>``) that no human works on — never
    the base branch, never a branch the operator named;
  * it pushes to the clone's **fetch URL for that one ref**, leaving the push
    remote pinned at ``DISABLED_NO_PUSH`` so the global push-disable still holds
    for every other ref (identical to the F10 mechanism);
  * the target branch is run through the spine's non-overridable
    :func:`..spine.push_policy.authorize_direct_push` denylist, so a crafted
    config cannot aim it at ``main``;
  * the draft is created as a **DRAFT** and no recipe ever passes a publish,
    merge, or ready-for-review verb. Publishing stays a human action.

If the push is refused or fails, drafting degrades to the durable queue copy
exactly as the upstream recipe degraded when its CLI was absent — the verified
commit stays local and recoverable, and nothing escapes silently.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from pathlib import Path

from ..spine.git_safety import GIT_SAFE_CONFIG, require_pinned

logger = logging.getLogger(__name__)

#: Branch namespace for generated fix branches. Prefixed so a human scanning
#: ``git branch -r`` can see at a glance which refs this app created, and so the
#: protected-branch denylist can never match one.
BRANCH_PREFIX = "auto-improvement"

#: Push/network operations are bounded so a hung remote cannot wedge a run.
_PUSH_TIMEOUT_S = 120.0
_CLI_TIMEOUT_S = 120.0


def _strip_leading_h1(text: str) -> str:
    """Drop a leading ``# …`` heading from a description body.

    The description builders already lead with their own H1, and the summary is
    rendered as the PR title, so keeping both produced a doubled title upstream.
    Same fix applies here.
    """
    lines = (text or "").lstrip().splitlines()
    if lines and lines[0].lstrip().startswith("# "):
        return "\n".join(lines[1:]).lstrip()
    return text or ""


class ProseRedactionUnavailable(RuntimeError):
    """The prose scanner could not run, so nothing may be PUBLISHED.

    Distinct from "the prose was scanned and had a hit": a hit is redacted in place and
    ships. This is the "we do not know what is in it" case, and publishing is the one
    action that cannot be undone.
    """


def _redact_prose(text: str) -> str:
    """Strip credentials / exfiltration URLs from agent-authored PR prose.

    Applies to the title and description only, never the diff: prose survives being
    rewritten, whereas redacting a code diff would corrupt the fix the gate proved
    (that content is DETECTED and refused instead — see ``_scan_pushable_content``).

    FAILS CLOSED by raising :class:`ProseRedactionUnavailable`. This was previously
    best-effort — it returned the text unscanned — on the reasoning that the diff beside
    it had passed a fail-closed scan and the PR is only a draft. That reasoning does not
    hold: the prose is a SEPARATE artifact from the diff, it is the part the agent wrote
    most freely, and the provider CLI publishes it where a description cannot be
    un-published (it persists in the API's edit history even after an edit). Every other
    egress path in this app already fails closed for exactly this reason
    (`mcp_server._redact_result`, `routes._redact_for_display`); this was the one that
    did not. Raised by the GPT review.

    The caller degrades to the durable queue, so a verified fix is never lost — it waits
    on disk for a human instead of being published unscanned.
    """
    try:
        from kiro_crew.security import redact
    except Exception as exc:  # noqa: BLE001 - the scanner itself is unavailable
        raise ProseRedactionUnavailable(f"redaction tooling unavailable: {exc}") from exc
    try:
        return redact(text or "")
    except Exception as exc:  # noqa: BLE001 - a scan that cannot run is not a clean scan
        raise ProseRedactionUnavailable(f"prose scan failed: {exc}") from exc


def _kind_of(summary: str) -> str:
    """Best-effort track label for the branch name, from the summary's prefix."""
    head = (summary or "").strip().lower()
    if head.startswith("fix") or "bug" in head[:24]:
        return "bug"
    if "perf" in head[:24] or "speed" in head[:24]:
        return "perf"
    return "fix"


class ProviderPRRecipe:
    """Draft a review from a push-disabled clone. Never publishes, never merges.

    Subclasses supply only the provider seams: :attr:`provider`, :attr:`cli`,
    :meth:`_authenticated_remote`, :meth:`_create_cmd`, and :meth:`_extract_ref`.
    """

    #: Provider slug — feeds the display ``namespace`` and log lines.
    provider = ""
    #: The provider CLI binary that opens the draft (``gh`` / ``glab``).
    cli = ""

    def __init__(
        self,
        *,
        user: str,
        clone_path: Path,
        pr_queue_dir: Path,
        base_ref: str | None = None,
        fetch_url: str | None = None,
    ) -> None:
        #: Display/metadata only — the spine never parses it. The meaningful
        #: "namespace" is the authenticated account that owns the draft.
        self.namespace = f"{self.provider}/{user}" if user else self.provider
        self.user = user
        self.clone_path = Path(clone_path)
        self.pr_queue_dir = Path(pr_queue_dir)
        self.base_ref = base_ref
        #: The provider CLIs want a plain branch name for the target base; strip the
        #: remote prefix.
        self.base_branch = (
            base_ref.split("/", 1)[1] if base_ref and base_ref.startswith("origin/") else base_ref
        )
        #: The real remote URL to push the one generated ref to. The clone's push
        #: remote stays DISABLED_NO_PUSH; see the module docstring.
        self.fetch_url = fetch_url

    # ── provider seams (subclass responsibilities) ────────────────────────────

    def _authenticated_remote(self, url: str) -> str:
        """Rewrite ``url`` onto the transport this provider's CLI authenticates.

        Default: unchanged. A subclass overrides this with its host-specific
        HTTPS→SSH rewrite; the rewrite may only ever change the TRANSPORT of an
        already-validated URL, never its owner or project path.
        """
        return url

    def _create_cmd(self, *, summary: str, body_path: Path, branch: str) -> list[str]:
        """The full draft-creation argv. MUST create a DRAFT and MUST NOT publish."""
        raise NotImplementedError

    def _extract_ref(self, stdout: str) -> str | None:
        """The first real review URL in the CLI's stdout, else ``None``."""
        raise NotImplementedError

    # ── internals ────────────────────────────────────────────────────────────

    #: Trusted git config on every host-side git over the agent-writable clone — same as the
    #: driver/gate/commit `_GIT_SAFE_CONFIG`. This helper runs `git push` (and checkout/reset) on
    #: the HOST as the gateway user in the tree the sandboxed agent edited, so a repo-planted hook
    #: (`core.hooksPath`, incl. `pre-push`) or a `core.fsmonitor` program would execute host-side.
    #: `-c` overrides on OUR argv beat the repo config. Raised by the GPT review.
    _GIT_SAFE_CONFIG = GIT_SAFE_CONFIG

    def _git(self, *args: str, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
        require_pinned(self.clone_path)
        return subprocess.run(
            ["git", *self._GIT_SAFE_CONFIG, *args],
            cwd=str(self.clone_path),
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def _resolve_fetch_url(self) -> str | None:
        """The clone's origin FETCH url — the push destination for the one ref."""
        if self.fetch_url:
            return self.fetch_url
        try:
            proc = self._git("remote", "get-url", "origin")
        except (OSError, subprocess.SubprocessError):
            return None
        url = (proc.stdout or "").strip()
        # A clone whose fetch url was also neutralized (the per-PR watcher clones
        # do this) cannot be pushed from — degrade to the queue.
        if proc.returncode != 0 or not url or "DISABLED" in url.upper():
            return None
        return self._authenticated_remote(url)

    def branch_name(self, *, kind: str, fingerprint: str) -> str:
        """Generated, app-namespaced branch for this fix. Never a human's branch."""
        safe_kind = re.sub(r"[^a-z0-9]+", "-", (kind or "fix").lower()).strip("-") or "fix"
        return f"{BRANCH_PREFIX}/{safe_kind}-{fingerprint}"

    def _authorize(self, branch: str) -> tuple[bool, str]:
        """Run the generated branch through the spine's non-overridable denylist.

        Belt-and-braces: the generated name is always prefixed so it cannot equal
        a protected name, but this is the authoritative gate and it is cheap.
        """
        from ..spine.push_policy import authorize_direct_push

        return authorize_direct_push(direct_commit=True, branch=branch)

    def _scannable_base(self) -> str | None:
        """The base ref to diff HEAD against, or ``None`` when it cannot be resolved.

        Returns ``""`` when no base is configured (the caller then scans HEAD's own patch).

        `base_ref` is `config["branch"]`, which an operator may set to a plain LOCAL name.
        `git diff work...HEAD` when HEAD *is* `work` yields an EMPTY diff, so
        `scan_content_for_secrets("")` reports clean and an agent-authored credential is
        pushed unscanned. Measured on a real bare repo: with `base_ref="work"` the diff was
        **0 bytes** and the planted `AKIAIOSFODNN7EXAMPLE` was invisible; with
        `base_ref="origin/work"` it was 132 bytes and caught.

        This is the same self-diffing failure already fixed in `driver._direct_push` (which
        moved to `HEAD~1..HEAD` for the same reason) — the recipe had its own copy. Resolution
        order: use the ref as given if it already names something OTHER than HEAD, else try the
        remote-tracking form, else refuse. Refusing beats falling back to the single-commit
        scan, because a narrower range that happens to pass is exactly the silent downgrade
        this guards against. Raised by the GPT review.
        """
        base = (self.base_ref or "").strip()
        if not base:
            return ""  # no base configured — caller scans HEAD's own patch

        # A raising `git` here must REFUSE, not propagate: `_scan_pushable_content` is
        # fail-closed, and an unresolvable base is exactly the case this returns None for.
        try:
            head = (self._git("rev-parse", "HEAD").stdout or "").strip()
        except (OSError, subprocess.SubprocessError):
            logger.warning("could not resolve HEAD — refusing the push", exc_info=True)
            return None

        def _resolves_apart(ref: str) -> bool:
            try:
                proc = self._git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
            except (OSError, subprocess.SubprocessError):
                return False
            if proc.returncode != 0:
                return False
            return (proc.stdout or "").strip() != head

        from ..spine.push_policy import normalize_branch

        for candidate in (base, f"origin/{normalize_branch(base)}"):
            if _resolves_apart(candidate):
                return candidate
        logger.warning(
            "the configured base does not resolve to a commit distinct from HEAD — "
            "refusing the push rather than scanning an empty range"
        )
        return None

    def _scan_pushable_content(self) -> tuple[bool, str]:
        """Refuse the push when the content about to leave the host carries a credential.

        The driver redacts the commit MESSAGE, but nothing scanned the committed
        CONTENT, and a push to the provider is unwipeable in the same way a commit
        message is — worse, the diff is agent-authored, which CLAUDE.md treats as
        untrusted.

        DETECT, never rewrite: redacting a code diff would corrupt the very fix the
        gate proved, so a hit refuses the push and the change degrades to the durable
        local queue where an operator can look at it. That is why this returns
        ``(False, note)`` instead of a cleaned diff.

        Scans the whole commit range being pushed rather than the caller's ``diff``
        string: HEAD may carry earlier accepted commits that this draft would also
        publish, which is exactly the case a single-candidate check would miss.

        FAIL-CLOSED: if the scanners cannot be imported or the diff cannot be read,
        the push is refused. An unscannable push is indistinguishable from an
        unscanned one, and the queue keeps the work safe either way.
        """
        try:
            from ..spine.push_policy import describe_scan, scan_content_for_secrets
        except Exception:  # noqa: BLE001 - no scanner, no push
            # Message deliberately dropped, not interpolated: `note` is LOGGED by the
            # caller, and an exception text can carry a filesystem path.
            logger.warning("credential scanners unavailable — refusing the push", exc_info=True)
            return False, "credential scanners unavailable"
        # `base_ref` is optional on this recipe, and "None...HEAD" would make git error
        # out. With no base, scan the working commit's own patch instead.
        base = self._scannable_base()
        if base is None:
            # `_scannable_base` already logged why. An unresolvable base is refused rather
            # than silently downgraded to the single-commit scan: the caller configured a
            # base, and quietly scanning a narrower range is how a self-diff slips through.
            return False, "could not resolve the base to scan against"
        try:
            if base:
                # The full range this push would publish, against the base the PR targets.
                proc = self._git("diff", f"{base}...HEAD", timeout=_PUSH_TIMEOUT_S)
            else:
                # `--format=` prints the commit's PATCH and nothing else.
                proc = self._git("show", "--format=", "HEAD", timeout=_PUSH_TIMEOUT_S)
        except (OSError, subprocess.SubprocessError):
            logger.warning("could not read the pushable diff — refusing the push", exc_info=True)
            return False, "could not read the pushable diff"
        if proc.returncode != 0:
            # git's stderr can echo repository CONTENT, and this note is logged by the
            # caller. The detail goes to the log with exc-style context; the returned
            # note stays a literal so no scanned text can ride along.
            logger.warning(
                "could not read the pushable diff (git exit %s) — refusing the push",
                proc.returncode,
            )
            return False, "could not read the pushable diff"
        clean, code = scan_content_for_secrets(proc.stdout or "")
        # A fixed code -> a fixed literal, so the note the caller LOGS carries nothing
        # derived from the scanned diff.
        return clean, ("" if clean else describe_scan(code))

    def _push_fix_branch(self, *, branch: str) -> tuple[bool, str]:
        """Push HEAD to ``branch`` on the fetch url. Returns (ok, note)."""
        url = self._resolve_fetch_url()
        if not url:
            return False, "no pushable origin fetch url (clone fully push-disabled)"
        ok, reason = self._authorize(branch)
        if not ok:
            return False, f"branch refused by push policy: {reason}"
        scanned, scan_note = self._scan_pushable_content()
        if not scanned:
            return False, scan_note
        try:
            proc = self._git(
                "push",
                "--force-with-lease",
                url,
                f"HEAD:refs/heads/{branch}",
                timeout=_PUSH_TIMEOUT_S,
            )
        except (OSError, subprocess.SubprocessError):
            logger.warning("push failed for %s", branch, exc_info=True)
            return False, "push failed"
        if proc.returncode != 0:
            # git's stderr can echo repository CONTENT, and the caller LOGS this note.
            # The detail goes to the log here (where it is not returned upward); the note
            # itself stays a literal so nothing from the push output can ride along.
            logger.warning(
                "push failed for %s (git exit %s): %s",
                branch,
                proc.returncode,
                (proc.stderr or "").strip()[:200],
            )
            return False, "push failed"
        return True, branch

    # ── the seam ─────────────────────────────────────────────────────────────

    def draft(
        self,
        *,
        summary: str,
        description: str,
        diff: str,
        fingerprint: str,
        parent_ref: str | None = None,
    ) -> str:
        """Create a DRAFT review; return its URL, or ``QUEUED:<fp>`` on any failure.

        The durable queue copy (``pr_queue/<fp>.diff`` + ``.pr.md``) is written
        FIRST so the record survives even when pushing or the provider CLI is
        unavailable — the morning-collection workflow keeps working offline.
        """
        self.pr_queue_dir.mkdir(parents=True, exist_ok=True)
        (self.pr_queue_dir / f"{fingerprint}.diff").write_text(diff or "")
        body_path = self.pr_queue_dir / f"{fingerprint}.pr.md"
        # The title and body are agent-authored PROSE, so unlike the diff they can be
        # redacted without breaking anything the gate proved — a rewritten sentence is
        # still a valid sentence. The provider CLI publishes both, and a description
        # cannot be un-published, so this happens before the queue copy is written.
        # Redact BEFORE the queue copy is written, so the on-disk record is scanned too.
        # A scanner that cannot RUN degrades to the queue rather than publishing unscanned
        # prose: the queue copy still gets written (from the raw text — it never leaves the
        # host, and a human needs to see what the agent actually wrote), but the create
        # command is not reached.
        try:
            summary = _redact_prose(summary)
            description = _strip_leading_h1(_redact_prose(description))
        except ProseRedactionUnavailable as exc:
            body_path.write_text(f"# {summary}\n\n{_strip_leading_h1(description)}\n")
            logger.warning(
                "draft degraded to queue for %s: %s — prose was not published",
                fingerprint,
                exc,
            )
            return f"QUEUED:{fingerprint}"
        body_path.write_text(f"# {summary}\n\n{description}\n")

        if shutil.which(self.cli) is None:
            logger.info("%s CLI not on PATH — draft queued at %s", self.cli, body_path)
            return f"QUEUED:{fingerprint}"

        # A PR/MR compares two refs that both exist on the remote, so the fix must be
        # pushed to its own generated branch first. See the module docstring for
        # why this is safe and how it is narrowed.
        branch = self.branch_name(kind=_kind_of(summary), fingerprint=fingerprint)
        pushed, note = self._push_fix_branch(branch=branch)
        if not pushed:
            logger.warning("draft degraded to queue for %s: %s", fingerprint, note)
            return f"QUEUED:{fingerprint}"

        cmd = self._create_cmd(summary=summary, body_path=body_path, branch=branch)
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(self.clone_path),
                capture_output=True,
                text=True,
                timeout=_CLI_TIMEOUT_S,
            )
        except (FileNotFoundError, subprocess.SubprocessError) as exc:
            logger.warning("%s draft failed to launch for %s: %s", self.cli, fingerprint, exc)
            return f"QUEUED:{fingerprint}"
        if proc.returncode != 0:
            logger.warning(
                "%s draft failed for %s: %s",
                self.cli,
                fingerprint,
                (proc.stderr or "").strip()[:200],
            )
            return f"QUEUED:{fingerprint}"
        return self._extract_ref(proc.stdout or "") or f"QUEUED:{fingerprint}"
