"""The crew source: which crew a name addresses, where it lives, and the read of its spec.

``resolve_crew`` asks two questions in order for both verbs -- can the name address a path
(``_validated_crew_name``), and can a launch use it (``_refuse_unless_launchable``) -- and
``read_agent_spec`` reads the spec under the same fences a prompt reference gets, because its
bytes ship as ``agent.json``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import pinned as _pinned
from . import sensitive as _sensitive
from .contract import _MAX_PROMPT_BYTES, ExportRefused


@dataclass(frozen=True)
class ResolvedCrew:
    name: str
    agent_spec_path: Path
    skills_root: Path


def _default_kiro_home() -> Path:
    override = os.environ.get("KIRO_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".kiro"


def _default_config_dir() -> Path:
    override = os.environ.get("KIROCREW_HOME")
    if override:
        return Path(override).expanduser()
    # The repo's real convention is ~/.kiro/crew, NOT ~/.kirocrew. Kiro Crew's
    # config_dir() defaults here (config/paths.py:44 CONFIG_DIR_NAME=".kiro/crew",
    # :93 "default data root: ~/.kiro/crew") and skills live at config_dir()/skills
    # (config/sections.py: "Local ~/.kiro/crew/skills/ takes precedence"). The
    # wrong default (~/.kirocrew) appeared nowhere else in the tree and, with
    # KIROCREW_HOME unset, made curation scan a directory that does not exist,
    # find no skills, and produce a bundle that silently omitted them.
    # ``_default_kiro_home`` above already uses ~/.kiro for the agent home; this agrees.
    return Path.home() / ".kiro" / "crew"


def _validated_crew_name(name: str) -> str:
    """A crew name is a NAME. Reject anything that can address a path.

    ``agent_spec_path`` was built as ``source / "agents" / f"{name}.json"``, and
    ``Path.__truediv__`` treats an absolute segment as a new root and a ``..`` segment as a
    parent step. So ``--crew ../../secrets`` read a JSON file outside the selected source
    and bundled its contents, and an absolute name discarded the source entirely.

    ``--crew`` is operator-supplied rather than attacker-supplied, so this is hardening
    rather than a breach: the value cannot be set by the untrusted crew content the rest of
    this module defends against. It is still worth refusing, because the operator's typo
    and the operator's paste are the same shape as the attack, and a name that resolves
    outside the source they named is never what they meant.

    Kept deliberately narrow: separators of either platform, parent steps, absolute paths,
    a Windows drive, and the empty name. THIS check asks only whether the name can address
    a path, and it deliberately does not ask whether a launch can use the name --
    ``_refuse_unless_launchable`` owns that, against a charset it imports rather than
    restates. So a name that clears this one is a name that stays inside the source
    directory, which is not the same thing as a name a bundle is allowed to carry.
    """
    if not name or name in {".", ".."}:
        raise ExportRefused(f"crew name {name!r} is empty or a directory reference.")
    if "/" in name or "\\" in name or "\x00" in name:
        raise ExportRefused(
            f"crew name {name!r} contains a path separator. A crew name addresses one file "
            f"inside the source's agents/ directory, so a name that can leave that "
            f"directory is refused."
        )
    if os.path.isabs(name) or (len(name) > 1 and name[1] == ":"):
        raise ExportRefused(
            f"crew name {name!r} is an absolute path. Joining it would discard the source "
            f"directory entirely, so the spec read would come from somewhere --source never "
            f"named."
        )
    return name


def _refuse_unless_launchable(name: str) -> None:
    """Refuse a crew name no launch can derive its AWS resources from.

    ``_validated_crew_name`` above asks whether the name can address a path. This
    asks the other question a bundle owes the operator who builds it: whether the
    crew it names can become the resources that run it. A bundle exists to be
    launched on Fargate -- the container supervisor is its only reader -- and the
    launch derives both IAM role names, the task-definition family, the secret
    namespace and the log group from the crew name. The charset those derivations
    need is therefore the charset a bundle has to satisfy, and a name outside it
    describes a bundle with no reachable future.

    Asking HERE is the point. Bundling is where the operator decides: it selects
    skills and MCP servers and prints the deny-by-default report, it exits 0, and
    it hands back a digest, all of which read as confirmation that the crew is
    deployable. The launch-side refusals land at a CloudFormation parameter error
    or a task-definition refusal, and neither mentions a bundle, so the remedy --
    rename the member and rebuild -- is not visible from either message.

    The charset is IMPORTED rather than restated. ``cloud/fargate/identity.py``
    owns it, and ``cloud/templates/kirocrew-fargate-crew.yaml`` mirrors it as a
    CloudFormation ``AllowedPattern`` only because YAML cannot import; a third copy
    spelled here is the drift this refusal exists to close. So an unimportable
    validator refuses the build, the same direction every other mandatory authority
    in this module fails: a build that cannot check the name cannot claim the
    bundle is launchable either.
    """
    try:
        from kiro_crew.cloud.fargate.identity import DocumentRefused, validated_crew_name
    except Exception as exc:
        raise ExportRefused(
            f"cannot check whether crew name {name!r} is one a launch can use: this "
            f"repository's own crew-name charset "
            f"(kiro_crew.cloud.fargate.identity.validated_crew_name) is not importable "
            f"here. The charset is owned there so the builder and the launch cannot "
            f"disagree about it, and restating it in this module is the drift that check "
            f"exists to prevent. Refusing rather than bundling a crew whose launchability "
            f"is unknown."
        ) from exc
    try:
        validated_crew_name(name, source="--crew")
    except DocumentRefused as exc:
        raise ExportRefused(
            f"crew name {name!r} cannot be launched: {exc}. Both IAM role names, the "
            f"task-definition family, the secret namespace and the log group are derived "
            f"from this name, and the per-crew CloudFormation stack constrains its own "
            f"Crew parameter to the same charset, so a bundle built under this name has "
            f"no deployment that accepts it. Rename the member to a conforming name and "
            f"build again."
        ) from exc


def resolve_crew(name: str, source: Path | None) -> ResolvedCrew:
    """Resolve a crew's agent spec and skills root.

    With ``--source`` (or ``$SMC_CREW_SOURCE``) the root holds ``agents/`` and
    ``skills/`` -- the shape a test fixture provides. Without it, the real
    locations are used: the agent spec under ``$KIRO_HOME``/``~/.kiro/agents``
    and skills under ``$KIROCREW_HOME``. Never a temp dir.
    """
    name = _validated_crew_name(name)
    # Two questions, asked in this order, because they refuse for different reasons: the
    # one above is about what a name can address on THIS filesystem, and this one is about
    # what a launch can derive from it. Both verbs come through here -- ``plan`` as much as
    # ``build`` -- because ``plan`` is the step that writes the review template the operator
    # fills in, and a review of a crew that can never launch is work spent on nothing.
    _refuse_unless_launchable(name)
    if source is not None:
        # ONE guard, not two. A containment assertion on the resolved spec path was here as
        # defence in depth, and it is unreachable: with the name check above in place no
        # value gets far enough to land outside ``agents/``, so no test could redden it. A
        # guard no test can fail is a comment claiming a property nobody verifies, so it is
        # gone rather than shipped. If the join ever changes shape, the check to add back is
        # one that can be tested against the new shape.
        return ResolvedCrew(
            name=name,
            agent_spec_path=source / "agents" / f"{name}.json",
            skills_root=source / "skills",
        )
    return ResolvedCrew(
        name=name,
        agent_spec_path=_default_kiro_home() / "agents" / f"{name}.json",
        skills_root=_default_config_dir() / "skills",
    )


def read_agent_spec(crew: ResolvedCrew) -> dict:
    _pinned._refuse_without_nofollow_primitive()
    path = crew.agent_spec_path
    # The same fence the prompt reference gets, on the same reasoning: the spec's bytes SHIP,
    # as ``agent.json`` inside the bundle, so this read reaches the customer just as directly
    # as an inlined prompt does. ``--source`` is the operator's flag and the crew name is
    # validated, so the shape ``<source>/agents/<name>.json`` is narrow -- but "narrow" was
    # the argument for the local denylist that three review passes each holed, so the answer
    # is to ask the shared question rather than to argue about reach.
    #
    # Unlike the prompt path this does NOT refuse outright when the fence is unimportable:
    # reading the agent spec is the tool's whole purpose and there is no inline alternative
    # to fall back to, so refusing would make the module unusable in the standalone mode it
    # documents. It does not SKIP the question either -- that made standalone the one
    # mode where a sensitive --source was read and bundled. The local list below answers a
    # coarser version of it, and runs in ADDITION to the shared validator, never instead.
    # A symlink at the spec IS refused, below, whatever either fence can say.
    # Spelled as a module import rather than ``from ... import is_sensitive_path``, which is
    # the mutation anchor a test uses to simulate the fence being unimportable at the PROMPT
    # site. Sharing that prefix once retargeted the mutation onto this line and broke the
    # module instead of testing the prompt fallback; ``load_build`` now refuses an anchor
    # that more than one builder file contains, so the two spellings stay distinct.
    # The UNC question comes FIRST, before the sensitive-path fence and before any stat.
    # ``hooks.validate_file_path`` states the reason: ``realpath`` on a UNC path IS the
    # outbound SMB probe, and a Windows SMB touch carries an NTLM exchange. A ``--source``
    # or ``--crew`` naming a share therefore leaks a credential exchange to that host
    # before anything about the path has been judged, and the sensitive-path fence below
    # cannot help -- it reads the NAME, and by the time its verdict matters the stat has
    # already gone out.
    #
    # nt-scoped, and fails CLOSED on an unavailable import, matching the prompt site's
    # gate. This is the one question in this function that is not answerable from a local
    # list: whether resolving a path reaches a host is not a property of its spelling.
    if os.name == "nt":
        try:
            from kiro_crew.hooks import is_unc_shape, unc_probe_allowed
        except ImportError as exc:
            raise ExportRefused(
                f"cannot judge whether the agent spec path {path} names a UNC path, "
                f"because kiro_crew.hooks is not importable here ({exc}). Reading it could "
                f"reach a host over SMB before any check runs, so it is refused rather than "
                f"read unchecked. Point --source at a local crew home."
            ) from exc

        _raw_spec = str(path)
        if is_unc_shape(_raw_spec) and not unc_probe_allowed(_raw_spec):
            raise ExportRefused(
                f"the agent spec path {path} is a UNC path outside the trusted roots. "
                f"Reading it would reach that host over SMB before this build could check "
                f"anything about it, and a Windows SMB touch carries an NTLM exchange. "
                f"Point --source at a local crew home."
            )

    # BEFORE the sensitive-path fence below, not after it. That fence RESOLVES: its own
    # contract is the "fully symlink-RESOLVED canonical target (realpath / Path.resolve --
    # follows every symlink in the chain)", and on Windows following a reparse point that
    # names a share IS the outbound SMB probe with its NTLM exchange. A local path leading
    # through a junction to a share therefore leaks during the fence's own resolution, and a
    # refusal computed afterwards arrives after the packet. The walk below judges each
    # component by lstat and follows nothing, so it is safe to run first and it is the only
    # one of the two that can be.
    # The WHOLE chain below the crew root, not just the final component.
    #
    # ``_is_redirecting_entry(path)`` was the check here and it only judges the last name, so a
    # redirect at the PARENT -- ``<source>/agents`` replaced by a junction -- was traversed by
    # the ``is_file()`` below it. That is the same mistake the prompt fence made in its first
    # version, and the same function fixes it: the walk judges each component by ``lstat`` and
    # never follows one, which is what keeps a Windows reparse point naming a share from being
    # probed before anything has been checked.
    #
    # Anchored at the crew root (``<source>`` or the default Kiro home), which is the operator's
    # own flag rather than crew content. Above that is not this build's business; below it is
    # exactly the part that may have arrived with a downloaded crew.
    _pinned._refuse_redirects_in_chain(
        path.parent.parent, f"{path.parent.name}/{path.name}", what="agent spec"
    )

    try:
        from kiro_crew import security as _sec

        _spec_fence: Callable[[str], bool] | None = _sec.is_sensitive_path
    except Exception:  # pragma: no cover - exercised by whichever branch the environment allows
        _spec_fence = None
    _posix = path.as_posix()
    if (_spec_fence is not None and _spec_fence(_posix)) or _sensitive._looks_sensitive_standalone(
        _posix
    ):
        raise ExportRefused(
            f"the agent spec path {path} is one this repository treats as sensitive. Its "
            f"bytes ship inside the bundle as agent.json, so it is read under the same fence "
            f"a prompt reference gets. Check --crew / --source."
        )
    # No separate ``is_file()`` before the read: that stat opened a check/read window a
    # concurrent writer could win by loop-swapping the spec between the two. The read goes
    # through ``hooks.safe_read_file_bytes_nolink``, the one authority that owns the
    # sensitive-path, descriptor-fstat and HARD-LINK refusals -- the spec's bytes ship inside
    # the bundle as ``agent.json``, so a hard link giving a credential file a second innocent
    # name at ``agents/<name>.json`` clears the chain check above (a hard link is not a
    # redirect) while its bytes are the credential, and ``st_nlink > 1`` on the opened
    # descriptor is the identity neither the chain walk nor the sensitive-path fence can see.
    # It opens the leaf ``O_NOFOLLOW`` and fstats the descriptor it opened, and confirms the
    # opened inode resolves inside ``anchor`` and is not sensitive. The chain check stays as
    # the readable refusal for a pre-planted redirect; the authority is what closes the RACE
    # the chain check cannot and adds the hard-link refusal on the same descriptor.
    #
    # ``anchor`` is the crew root (``agents/`` parent's parent), the same directory the chain
    # check above anchors at and the same one the openat walk used, so the containment answer
    # is unchanged. A missing file, a link, a FIFO, a directory or a hard-linked name all
    # surface as ``None``; the branches below keep the "nothing to deploy" case distinguishable
    # from an unreadable one via a non-following ``lstat``. The refusals name the AGENT SPEC,
    # because this is the spec read and its wording reaches the operator verbatim.
    anchor = path.parent.parent
    try:
        from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes_nolink
    except ImportError as exc:
        raise ExportRefused(
            f"cannot read the agent spec {path} safely, because kiro_crew.hooks is not "
            f"importable here ({exc}). That module holds the sensitive-path and hard-link "
            f"rules this read has to satisfy, and a local approximation of them is not the "
            f"same check. Its bytes ship inside the bundle as agent.json, so it cannot be "
            f"certified clean without the authority. Check --crew / --source."
        ) from exc

    # TWO refusal channels that mean different things: None is "the guard rejected this",
    # while the size cap RAISES. Catching only one lets a FileTooLargeError out of a function
    # whose contract is ExportRefused, reaching the CLI as a traceback.
    try:
        data = safe_read_file_bytes_nolink(str(path), str(anchor), max_bytes=_MAX_PROMPT_BYTES)
    except FileTooLargeError as exc:
        raise ExportRefused(
            f"agent spec {path} exceeds the {_MAX_PROMPT_BYTES} byte ceiling ({exc}). A spec "
            f"that large is not a crew's agent definition; check --crew / --source."
        ) from None
    if data is None:
        # Three outcomes, each refused where it is detected rather than through a sentinel the
        # branch below re-reads: absent, present-but-uninspectable, present-but-unreadable (a
        # link, a hard-linked name, a special file, a directory, sensitive, or outside the
        # anchor). Reporting the middle one as "nothing to deploy" would send the operator
        # looking for a missing file while the spec sits there refused.
        try:
            os.lstat(path)
        except FileNotFoundError:
            raise ExportRefused(
                f"no agent spec for crew {crew.name!r} at {path}. There is nothing to "
                f"deploy; check --crew / --source."
            ) from None
        except OSError as exc:
            raise ExportRefused(
                f"agent spec {path} exists but could not be inspected ({exc}), so whether "
                f"there is anything to deploy is unknown. Fix its permissions."
            ) from None
        raise ExportRefused(
            f"agent spec {path} was refused by the repository's file-read guard. It is a "
            f"link, hard-linked to another name, a special file, a directory, sensitive, or "
            f"outside {anchor}; refusing rather than shipping bytes that cannot be certified "
            f"clean. Check --crew / --source."
        )
    # Decode the guarded bytes exactly as they sit on disk: no newline translation and no
    # re-encode, so the read is byte-faithful. A non-UTF-8 body is refused, not shipped.
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ExportRefused(
            f"agent spec {path} could not be read as UTF-8 (it may be a link, a special "
            f"file, or reached through a redirected parent); refusing rather than following it."
        ) from None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ExportRefused(f"agent spec {path} is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ExportRefused(f"agent spec {path} must be a JSON object")
    return parsed
