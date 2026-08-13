"""Field ⑤ — draft a GitHub pull request, never publish-ready, never merged.

Satisfies the spine's :class:`..spine.profile.PRRecipe` protocol (``namespace`` +
``draft``). Replaces the internal-review-CLI recipe this app was ported from;
``spine/profile.py`` names ``gh pr create --draft`` as the intended substitution,
so this is the seam working as designed.

## Why this needs a push where the upstream recipe did not

The upstream review CLI uploaded the commit through a side channel that was not
the git remote — so it could draft a review from inside a push-disabled clone
without ever pushing. GitHub has no such side channel: a PR is *defined* as a
comparison between two refs that both exist on the remote, so the fix branch
must be pushed before ``gh pr create`` can reference it.

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
  * the PR is created as a **draft** and this module never passes
    ``--web``/``--fill-verbose``, never calls ``gh pr merge``, and never marks
    ready-for-review. Publishing stays a human action.

If the push is refused or fails, drafting degrades to the durable queue copy
exactly as the upstream recipe degraded when its CLI was absent — the verified commit
stays local and recoverable, and nothing escapes silently.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from pathlib import Path

from ...spine.git_safety import GIT_SAFE_CONFIG, require_pinned

logger = logging.getLogger(__name__)

#: Draft-only PR creation. ``--draft`` is load-bearing: it is the mechanical half
#: of the draft-only policy the spine enforces. Never add ``--web`` (opens a
#: browser, useless headless) and never add a merge/ready subcommand here.
DRAFT_CMD = ("pr", "create", "--draft")

#: Branch namespace for generated fix branches. Prefixed so a human scanning
#: ``git branch -r`` can see at a glance which refs this app created, and so the
#: protected-branch denylist can never match one.
BRANCH_PREFIX = "auto-improvement"

#: Shape of a real GitHub PR reference in ``gh`` stdout. ``gh pr create`` prints
#: the PR URL on success, but the clone can emit trailing chatter (git hooks, the
#: agent's own stdout) after it — the upstream app learned this the hard way when a
#: git hook's message got recorded as the review id. Scan every line for the
#: FIRST real PR URL rather than trusting the last line.
_PR_URL_RE = re.compile(r"https://(?:www\.)?github\.com/[^\s/]+/[^\s/]+/pull/(\d+)")

#: Push/network operations are bounded so a hung remote cannot wedge a run.
_PUSH_TIMEOUT_S = 120.0
_GH_TIMEOUT_S = 120.0


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
    most freely, and `gh pr create` publishes it to GitHub where a description cannot be
    un-published (it persists in the API's edit history even after an edit). Every other
    egress path in this app already fails closed for exactly this reason
    (`mcp_server._redact_result`, `routes._redact_for_display`); this was the one that
    did not. Raised by the GPT review of this branch.

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


def extract_pr_url(stdout: str) -> str | None:
    """Return the first real GitHub PR URL in ``stdout``, else None."""
    match = _PR_URL_RE.search(stdout or "")
    return match.group(0) if match else None


#: ``https://github.com/<owner>/<repo>[.git]`` → the owner/repo pair.
_HTTPS_REMOTE_RE = re.compile(
    r"^https://(?:www\.)?github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$"
)


def _gh_prefers_ssh() -> bool:
    """True iff the ``gh`` CLI uses SSH for git operations against github.com.

    Read via ``gh config get`` rather than by parsing hosts.yml, so the answer
    comes from the tool that owns the setting.

    The HOST-SCOPED value is what matters and it is checked first: this setting is
    commonly per-host, and reading only the global default gets it backwards. On
    the host this was developed against, the global default is ``https`` while
    github.com is explicitly ``ssh`` — trusting the global answer alone would keep
    pushing over a transport that cannot authenticate.
    """
    if shutil.which("gh") is None:
        return False
    for args in (
        ["gh", "config", "get", "git_protocol", "-h", "github.com"],
        ["gh", "config", "get", "git_protocol"],
    ):
        try:
            proc = subprocess.run(args, capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            return False
        value = (proc.stdout or "").strip().lower()
        if proc.returncode == 0 and value:
            return value == "ssh"
    return False


def _prefer_authenticated_remote(url: str) -> str:
    """Rewrite an HTTPS GitHub remote to SSH when that is what is authenticated.

    The clone is deliberately made over HTTPS (a validated, allowlisted URL —
    never raw user text), but the push has to actually authenticate. Observed
    live: an HTTPS push to github.com failed because git's global
    ``credential.helper`` on this host is bound to a different provider entirely,
    while ``gh`` was authenticated for SSH. Pushing over HTTPS in that setup can
    never succeed, so the PR silently degraded to the queue.

    Only ever rewrites the TRANSPORT of an already-validated github.com URL — the
    owner/repo come from the matched groups, so this cannot retarget the push.
    """
    match = _HTTPS_REMOTE_RE.match(url.strip())
    if not match or not _gh_prefers_ssh():
        return url
    return f"git@github.com:{match.group('owner')}/{match.group('repo')}.git"


class GitHubPRRecipe:
    """Draft a GitHub PR from a push-disabled clone. Never publishes, never merges."""

    def __init__(
        self,
        *,
        user: str,
        clone_path: Path,
        pr_queue_dir: Path,
        base_ref: str | None = None,
        fetch_url: str | None = None,
    ) -> None:
        #: Display/metadata only — the spine never parses it. For GitHub the
        #: meaningful "namespace" is the authenticated account that owns the PR.
        self.namespace = f"github/{user}" if user else "github"
        self.user = user
        self.clone_path = Path(clone_path)
        self.pr_queue_dir = Path(pr_queue_dir)
        self.base_ref = base_ref
        #: ``gh`` wants a plain branch name for ``--base``; strip the remote prefix.
        self.base_branch = (
            base_ref.split("/", 1)[1] if base_ref and base_ref.startswith("origin/") else base_ref
        )
        #: The real remote URL to push the one generated ref to. The clone's push
        #: remote stays DISABLED_NO_PUSH; see the module docstring.
        self.fetch_url = fetch_url

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
        return _prefer_authenticated_remote(url)

    def branch_name(self, *, kind: str, fingerprint: str) -> str:
        """Generated, app-namespaced branch for this fix. Never a human's branch."""
        safe_kind = re.sub(r"[^a-z0-9]+", "-", (kind or "fix").lower()).strip("-") or "fix"
        return f"{BRANCH_PREFIX}/{safe_kind}-{fingerprint}"

    def _authorize(self, branch: str) -> tuple[bool, str]:
        """Run the generated branch through the spine's non-overridable denylist.

        Belt-and-braces: the generated name is always prefixed so it cannot equal
        a protected name, but this is the authoritative gate and it is cheap.
        """
        from ...spine.push_policy import authorize_direct_push

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
        this guards against. Raised by the GPT review of this branch.
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

        from ...spine.push_policy import normalize_branch

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
        CONTENT, and a push to GitHub is unwipeable in the same way a commit message
        is — worse, the diff is agent-authored, which CLAUDE.md treats as untrusted.

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
            from ...spine.push_policy import describe_scan, scan_content_for_secrets
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
        """Create a DRAFT GitHub PR; return its URL, or ``QUEUED:<fp>`` on any failure.

        The durable queue copy (``pr_queue/<fp>.diff`` + ``.pr.md``) is written
        FIRST so the record survives even when pushing or ``gh`` is unavailable —
        the morning-collection workflow keeps working offline.
        """
        self.pr_queue_dir.mkdir(parents=True, exist_ok=True)
        (self.pr_queue_dir / f"{fingerprint}.diff").write_text(diff or "")
        body_path = self.pr_queue_dir / f"{fingerprint}.pr.md"
        # The title and body are agent-authored PROSE, so unlike the diff they can be
        # redacted without breaking anything the gate proved — a rewritten sentence is
        # still a valid sentence. `gh pr create` publishes both, and a PR description
        # cannot be un-published, so this happens before the queue copy is written.
        # Redact BEFORE the queue copy is written, so the on-disk record is scanned too.
        # A scanner that cannot RUN degrades to the queue rather than publishing unscanned
        # prose: the queue copy still gets written (from the raw text — it never leaves the
        # host, and a human needs to see what the agent actually wrote), but `gh pr create`
        # is not reached.
        try:
            summary = _redact_prose(summary)
            description = _strip_leading_h1(_redact_prose(description))
        except ProseRedactionUnavailable as exc:
            body_path.write_text(f"# {summary}\n\n{_strip_leading_h1(description)}\n")
            logger.warning(
                "PR draft degraded to queue for %s: %s — prose was not published",
                fingerprint,
                exc,
            )
            return f"QUEUED:{fingerprint}"
        body_path.write_text(f"# {summary}\n\n{description}\n")

        if shutil.which("gh") is None:
            logger.info("gh CLI not on PATH — PR queued at %s", body_path)
            return f"QUEUED:{fingerprint}"

        # A PR compares two refs that both exist on the remote, so the fix must be
        # pushed to its own generated branch first. See the module docstring for
        # why this is safe and how it is narrowed.
        branch = self.branch_name(kind=_kind_of(summary), fingerprint=fingerprint)
        pushed, note = self._push_fix_branch(branch=branch)
        if not pushed:
            logger.warning("PR draft degraded to queue for %s: %s", fingerprint, note)
            return f"QUEUED:{fingerprint}"

        cmd = [
            "gh",
            *DRAFT_CMD,
            "--title",
            summary,
            "--body-file",
            str(body_path),
            "--head",
            branch,
        ]
        if self.base_branch:
            cmd += ["--base", self.base_branch]
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(self.clone_path),
                capture_output=True,
                text=True,
                timeout=_GH_TIMEOUT_S,
            )
        except (FileNotFoundError, subprocess.SubprocessError) as exc:
            logger.warning("gh pr create failed to launch for %s: %s", fingerprint, exc)
            return f"QUEUED:{fingerprint}"
        if proc.returncode != 0:
            logger.warning(
                "gh pr create failed for %s: %s", fingerprint, (proc.stderr or "").strip()[:200]
            )
            return f"QUEUED:{fingerprint}"
        return extract_pr_url(proc.stdout or "") or f"QUEUED:{fingerprint}"


def _kind_of(summary: str) -> str:
    """Best-effort track label for the branch name, from the summary's prefix."""
    head = (summary or "").strip().lower()
    if head.startswith("fix") or "bug" in head[:24]:
        return "bug"
    if "perf" in head[:24] or "speed" in head[:24]:
        return "perf"
    return "fix"
