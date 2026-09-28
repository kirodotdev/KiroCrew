"""Sandbox rows of ``kirocrew doctor``: the backend verdict, and what refuses a spawn.

Each row reports only what this process can observe. The AppArmor branch in
particular tells a shell that cannot confirm the service's confinement apart from a
host where the sandbox is broken.
"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path

from kiro_crew import cli_doctor, platform_compat, sandbox
from kiro_crew.doctor_checks import render
from kiro_crew.service import apparmor
from kiro_crew.service import linux as service_linux


def _process_apparmor_confinement() -> str:
    """AppArmor confinement label of THIS process, ``""`` when unreadable.

    Reads the kernel's own answer, e.g. ``unconfined`` or
    ``kirocrew-userns (enforce)``. The per-LSM path is tried first; the bare
    ``attr/current`` covers older kernels (where it may also carry an SELinux
    context — which is fine, since callers only compare against a profile name).
    """
    for attr in ("/proc/self/attr/apparmor/current", "/proc/self/attr/current"):
        try:
            raw = Path(attr).read_text(encoding="utf-8")
        except OSError:
            continue
        return raw.replace("\x00", "").strip()
    return ""


def _read_linux_proc_self(name: str) -> str:
    """Read one Linux ``/proc/self`` file; callers gate on ``IS_LINUX``."""
    return (Path("/proc/self") / name).read_text(encoding="ascii")


def _process_userns_vantage_confined() -> bool | None:
    """Whether kernel signals identify Kiro Crew's confined agent shell.

    ``None`` means not applicable or unreadable, so diagnostics preserve their
    existing host-level verdict. Only one identity UID mapping of length one
    plus seccomp filtering identifies Kiro Crew's own agent-shell shape;
    container user-namespace mappings keep the host-level verdict.
    """
    if not platform_compat.IS_LINUX:
        return None
    try:
        uid_map_text = _read_linux_proc_self("uid_map")
        status_text = _read_linux_proc_self("status")
    except (OSError, UnicodeError):
        return None

    uid_map: list[tuple[int, int, int]] = []
    for line in uid_map_text.splitlines():
        fields = line.split()
        if len(fields) != 3:
            return None
        try:
            values = [int(field) for field in fields]
        except ValueError:
            return None
        uid_map.append((values[0], values[1], values[2]))
    if not uid_map:
        return None

    seccomp_mode: int | None = None
    for line in status_text.splitlines():
        key, separator, value = line.partition(":")
        if key != "Seccomp" or not separator:
            continue
        try:
            seccomp_mode = int(value.strip())
        except ValueError:
            return None
        break
    if seccomp_mode is None:
        return None

    return (
        len(uid_map) == 1
        and uid_map[0][0] == uid_map[0][1]
        and uid_map[0][2] == 1
        and seccomp_mode == 2
    )


def _service_profile_applies(profile_path: Path, profile_name: str) -> bool:
    """True when the installed profile is ATTACHED to the launcher script this
    host currently resolves.

    The confining mechanism is a path attachment, not a systemd
    ``AppArmorProfile=<name>`` directive: the profile is attached BY PATH to
    ``kirocrew_bin()`` (the same path ``ExecStart`` uses), and installing the
    directive alongside a path attachment makes the directive silently win,
    defeating the attachment. ``kirocrew service install`` therefore does not
    write it, and this check reads the profile's own attachment clause and
    compares it against the CURRENTLY resolved launcher path, the same
    comparison ``apparmor.launcher_status()`` already makes for the AppImage
    case.

    A moved or reinstalled launcher (a venv rebuilt at a new path, a symlink
    re-pointed) makes this False until ``kirocrew service install`` re-renders
    the profile against the new path — the same staleness
    ``kirocrew sandbox status`` already reports for the launcher profile.
    """
    attached = apparmor.installed_attachment(profile_path, profile_name)
    if attached is None:
        return False
    try:
        current = str(Path(service_linux.kirocrew_bin()).resolve(strict=True))
    except OSError:
        return False
    if attached != current:
        return False
    # A unit that still carries ``AppArmorProfile=`` — a hand-edited unit, a
    # systemd drop-in, an older install — silently WINS over the kernel's path
    # attachment, which is why the directive is not used, so an attachment that
    # matches is not enough: the service would run under the directive's
    # semantics, leaving its own probe unconfined, while a shell launch through
    # the same path probes green. Best-effort read — an
    # unreadable unit (or none installed) proves nothing and must not flip a
    # verified attachment to "broken".
    try:
        # errors="replace" for the same reason as installed_attachment(): a
        # unit with undecodable bytes must not crash the verdict —
        # UnicodeDecodeError is a ValueError, outside the OSError guard.
        unit_text = service_linux.UNIT_PATH.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return True
    for line in unit_text.splitlines():
        if line.strip().startswith("AppArmorProfile="):
            return False
    return True


def _doctor_sandbox_apparmor(reason: str, issues: list[str]) -> None:
    """Verdict for the Ubuntu AppArmor userns-restriction denial (EPERM on NEWNS).

    Three honest verdicts, decided from real signals rather than the happy path:

    * profile absent → broken, with the install command;
    * profile installed but not ATTACHED to the launcher script this host
      currently resolves (checking the systemd unit for an
      ``AppArmorProfile=`` directive instead would silently fail closed
      against a correctly-installed, correctly-attached profile), or the
      probe failed even though THIS
      process is confined by the profile → broken, with the repair command;
    * profile installed and attached to the resolved launcher script, and this
      process is unconfined → the probe's failure says nothing about the
      service, so the verdict is "cannot be verified from this shell" plus how
      to verify — NOT a claim that the sandbox works, and NOT counted as an
      issue.
    """
    if not apparmor.PROFILE_PATH.is_file():
        print(f"  backend:     ❌ none — {reason}")
        render._print_wrapped(
            f"This host restricts unprivileged user namespaces and the "
            f"{apparmor.PROFILE_NAME} AppArmor profile is not installed, so no context "
            f"on this host can build the sandbox. Run `kirocrew service install` to "
            f"install the profile and confine the gateway service with it."
        )
        issues.append("sandbox: AppArmor profile not installed")
        return

    confinement = _process_apparmor_confinement()
    if confinement and confinement.split(" ")[0] == apparmor.PROFILE_NAME:
        # The one context that SHOULD be able to build the sandbox refused to:
        # this is a genuine fault, not a vantage-point artifact.
        print(f"  backend:     ❌ broken — {reason}")
        render._print_wrapped(
            f"This process already runs confined by {apparmor.PROFILE_NAME}, which "
            f"should grant user namespaces, yet the probe still failed. Re-run "
            f"`kirocrew service install` to re-render and reload the profile."
        )
        issues.append("sandbox: probe failed under the AppArmor profile")
        return

    if not _service_profile_applies(apparmor.PROFILE_PATH, apparmor.PROFILE_NAME):
        print(f"  backend:     ❌ none — {reason}")
        render._print_wrapped(
            f"The {apparmor.PROFILE_NAME} AppArmor profile is installed, but it is not "
            f"attached to the kirocrew launcher script this host currently resolves — "
            f"or the systemd unit still carries the retired `AppArmorProfile=` "
            f"directive, which silently overrides a path attachment (#3463). Either "
            f"way nothing on this host runs confined by it. Run `kirocrew service "
            f"install` to re-render both the profile and the unit."
        )
        issues.append("sandbox: AppArmor profile installed but not attached")
        return

    # Unverifiable from here — deliberately NOT an issue, and deliberately NOT a
    # success claim either.
    print("  backend:     ⏭  cannot be verified from this shell")
    render._print_wrapped(
        f"The {apparmor.PROFILE_NAME} AppArmor profile is installed and attached to "
        f"the kirocrew launcher script this host resolves, but this process was not "
        f"invoked through that exact path (or this shell is otherwise unconfined) — "
        f"so this probe cannot confirm the service's confinement from here no matter "
        f"how healthy it actually is. To verify the sandbox in the confined context "
        f"the service uses, run:"
    )
    # The recipe execs the ATTACHED LAUNCHER PATH: a path-attached profile is
    # applied by the kernel at execve() of that exact file, and the sandbox
    # probe (a fork with no subsequent exec) inherits the confinement — the
    # same chain the service's ExecStart uses. The
    # ``systemd-run --property=AppArmorProfile=`` form must NOT be used here:
    # the directive labels only the unit's own top-level process, so a probe
    # under it stays unconfined.
    # The path is quoted for the shell: the recipe is meant to be pasted, so an
    # install path containing spaces or shell metacharacters must arrive as one
    # argument, not execute.
    try:
        launcher = str(Path(service_linux.kirocrew_bin()).resolve(strict=True))
    except OSError:
        launcher = service_linux.kirocrew_bin()
    print(f"{render._INDENT}  {shlex.quote(launcher)} doctor")
    render._print_wrapped(
        "and read its Sandbox section: launched through the attached path, the "
        "probe itself runs confined, so a healthy sandbox reports its backend "
        "as: namespace"
    )


def _doctor_kiro_internal_sandbox() -> None:
    """Report that kiro-cli's own sandbox — not the backend above — confines it.

    The backend verdict answers for Kiro Crew's wrapper. On macOS with kiro-cli's
    internal sandbox enabled that wrapper is deliberately skipped for kiro-cli
    spawns (mutual exclusion: exactly one layer can be active per spawn), so a
    lone ``backend: ✅ seatbelt`` describes a profile the session's tool backend
    never runs under. Without this line the operator's only signal is a denied
    read of a path outside the workspace, which presents as a macOS privacy (TCC)
    problem — sending them to Full Disk Access, which cannot affect a Seatbelt
    profile.

    Not an issue: delegation is a working, audited configuration, so it must not
    add to the ``issues`` list. It is reported because it changes which paths the
    agent can reach, not because anything is broken.

    The remedy is conditional on the tier Kiro Crew would ACTUALLY apply
    (:func:`sandbox.effective_sandbox_mode`, which includes the governance
    clamp). Telling an operator to disable kiro-cli's sandbox while
    ``agent.sandbox`` is ``"off"`` would remove the only layer confining the
    spawn and make ``~/.aws`` and ``~/.ssh`` readable, and the two settings
    correlate rather than being independently unlikely: ``"off"`` exists to defer
    isolation to kiro-cli. A tier that cannot be read gets the cautious wording,
    never the bare recommendation.
    """
    if sys.platform != "darwin":
        return
    try:
        delegated = sandbox.kiro_internal_sandbox_enabled()
    except Exception:  # noqa: BLE001 — doctor must survive an unreadable setting
        return
    if not delegated:
        return
    settings_path, key = sandbox.kiro_internal_sandbox_switch()
    try:
        own_tier = sandbox.effective_sandbox_mode(sandbox.configured_sandbox_mode())
    except Exception:  # noqa: BLE001 — an unreadable tier must not shape the advice
        own_tier = None
    if own_tier is not None and own_tier != "off":
        remedy = (
            f'Set "{key}" to false to hand isolation back to Kiro Crew\'s own profile '
            f'(agent.sandbox="{own_tier}"), which masks credential paths and leaves the '
            "rest of your home readable; the value is re-read per spawn, so no restart "
            "is needed."
        )
    elif own_tier == "off":
        # The remedy inverts here. Recommending the internal sandbox off while
        # agent.sandbox is also off would remove the ONLY layer confining the
        # spawn, and the two settings correlate: "off" exists precisely to defer
        # isolation to kiro-cli. Name the order that keeps a layer at all times.
        remedy = (
            f'Do NOT just set "{key}" to false here: agent.sandbox is "off" too, so Kiro '
            "Crew builds no profile and turning this key off would leave the spawn with "
            "no OS confinement at all, making credential paths such as ~/.aws and ~/.ssh "
            'readable. Set agent.sandbox to "auto" first, then either layer owns isolation.'
        )
    else:
        remedy = (
            f'Before setting "{key}" to false, check that agent.sandbox is not "off": with '
            "both off the spawn runs with no OS confinement at all and credential paths "
            "such as ~/.aws and ~/.ssh become readable. Kiro Crew's own tier could not be "
            "read from here."
        )
    print("  kiro-cli:    ⚠️  confined by kiro-cli's own sandbox, not the backend above")
    render._print_wrapped(
        f'{settings_path} sets "{key}": true, so kiro-cli confines itself and Kiro Crew '
        "skips its seatbelt wrap for these spawns — only one sandbox layer can be active "
        "per spawn. kiro-cli's profile owns file access from there, so reading a "
        "PRE-EXISTING file outside the session workspace (~/Desktop, a project elsewhere) "
        'can fail with "Operation not permitted" while files the session created stay '
        "readable. That is a Seatbelt profile, not macOS privacy: granting the app Full "
        f"Disk Access cannot restore those reads. {remedy}"
    )


def _doctor_sandbox(issues: list[str]) -> None:
    """Render the ``Sandbox`` section: Kiro Crew's backend, then who else confines.

    Two questions, because they have different answers: which sandbox Kiro Crew
    can build here (:func:`_doctor_sandbox_backend`), and whether a spawn is
    handed to a sandbox Kiro Crew did not build
    (:func:`_doctor_kiro_internal_sandbox`). Reporting only the first claims a
    profile that a delegated kiro-cli spawn never runs under.
    """
    print("\nSandbox")
    _doctor_sandbox_backend(issues)
    _doctor_kiro_internal_sandbox()


def _doctor_sandbox_backend(issues: list[str]) -> None:
    """Render the backend verdict — an honest answer about the agent sandbox.

    The hard rule: report only what THIS process can observe.
    :func:`sandbox.detect_backend` answers for the probing process, not for the
    gateway service — on a host that restricts unprivileged user namespaces the
    profile is applied by systemd to the SERVICE, so from an interactive shell
    the probe fails with EPERM no matter how healthy the service's sandbox is.
    Reporting that failure as the sandbox being broken is a false negative; the
    fix must not swing to the false positive of claiming the sandbox works when
    all that is known is that it cannot be checked from here.
    """
    try:
        # ONE probe decision: ``unavailable_kind()`` probes internally and
        # returns "" for a working backend. Probing twice (a detect_backend
        # read followed by a classifying call) would let a transient failure
        # heal between the two reads and report a now-working backend as
        # broken.
        kind = sandbox.unavailable_kind()
    except Exception as exc:  # noqa: BLE001 — doctor must survive a broken probe
        print(f"  backend:     ⚠️  could not probe ({exc})")
        return
    if not kind:
        # The probe just succeeded, so this read serves the cached positive
        # result rather than probing again.
        print(f"  backend:     ✅ {sandbox.detect_backend()}")
        return

    reason = sandbox.unavailable_reason() or "no probe detail recorded"
    if kind == "transient":
        print("  backend:     ⚠️  probe failed transiently — not cached; the next spawn re-probes")
        print(f"{render._INDENT}({reason})")
        return
    if kind == "foreign_sandbox":
        print("  backend:     ⚠️  an outer sandbox already confines this process")
        render._print_wrapped(
            "Kiro Crew cannot nest its own sandbox inside it. Launch the gateway "
            "outside that sandbox to hand isolation back to Kiro Crew's own profile."
        )
        return

    remedy = sandbox.unavailable_remedy()
    if remedy == sandbox.REMEDY_APPARMOR_USERNS:
        _doctor_sandbox_apparmor(reason, issues)
        return
    if remedy == sandbox.REMEDY_USERNS_DENIED and _process_userns_vantage_confined() is True:
        print("  backend:     ⏭  cannot be verified from this shell")
        render._print_wrapped(
            "This shell is already confined inside a child user namespace with "
            "seccomp filtering, so its nested CLONE_NEWUSER refusal cannot establish "
            "the host's support; run `kirocrew doctor` from an unconfined shell instead."
        )
        return
    if sys.platform.startswith("linux"):
        # A permanent, named kernel refusal (user.max_user_namespaces=0, a kernel
        # without CONFIG_USER_NS, ...) — genuinely broken, with the mechanism's
        # own guidance when the probe identified one.
        print(f"  backend:     ❌ none — {reason}")
        guidance = sandbox.remedy_guidance(remedy)
        if guidance:
            render._print_wrapped(guidance)
        issues.append("sandbox backend")
        return
    # Platforms with no OS-level backend to offer (Windows; macOS builds without
    # sandbox-exec) — a fact about the platform, not a fault of this install.
    # Report what that MEANS for spawns, not just that the backend is absent: with
    # no backend the configured posture is either "refuse every agent subprocess"
    # or "run them unconfined", and an operator reading this line needs to know
    # which.
    #
    # Stated as the POSTURE, not as what will happen. A governance
    # sandbox.min_level floor is resolved per spawn against the mode that spawn
    # requested, so it is not foldable into one host-level answer — and on a
    # governed host it makes wrap_argv refuse the very spawns a bare "they run
    # unconfined" would promise. Each permitting branch therefore names the floor
    # as the thing that overrides it.
    print("  backend:     ⏭  no OS-level sandbox backend on this platform")
    permitted_by = sandbox.unsandboxed_exec_permitted_by()
    if permitted_by == sandbox.UNSANDBOXED_BY_PLATFORM:
        print("  exec:        ⚠️  configured to run agent subprocesses UNCONFINED")
        render._print_wrapped(
            "This is the default for a platform with no backend to install: "
            "~/.aws, ~/.ssh and the rest of your home directory are readable by "
            "an agent subprocess, and only the bypassable app-level checks "
            "remain. Every such spawn is audited. To refuse them instead, set "
            "agent.sandbox_allow_unsandboxed_exec=false. A governance "
            "sandbox.min_level floor overrides this and makes such spawns fail "
            "closed, so a managed host refuses them despite this line."
        )
    elif permitted_by == sandbox.UNSANDBOXED_BY_OPERATOR:
        print("  exec:        ⚠️  unconfined by declaration — sandbox_allow_unsandboxed_exec=true")
        render._print_wrapped(
            "The operator declared this opt-in, so agent subprocesses run "
            "without OS-level isolation and every such spawn is audited. Remove "
            "the key to fall back to this platform's default. A governance "
            "sandbox.min_level floor overrides the declaration and makes such "
            "spawns fail closed."
        )
    else:
        print("  exec:        ⛔ agent subprocesses are REFUSED on this host")
        if cli_doctor.unsandboxed_exec_declared():
            render._print_wrapped(
                "agent.sandbox_allow_unsandboxed_exec is set to false, so MCP "
                "servers, app backends and the provider CLIs will report a "
                "sandbox error. Remove the key to accept this platform's "
                "default, or set it to true to allow unconfined execution."
            )
        else:
            # Undeclared AND fail-closed: a platform with no backend whose default
            # is still refuse (a macOS build without sandbox-exec). Telling this
            # operator the key "is set to false" would send them to change
            # something they never wrote.
            render._print_wrapped(
                "No backend is available and no opt-in is declared, so MCP "
                "servers, app backends and the provider CLIs will report a "
                "sandbox error. Set agent.sandbox_allow_unsandboxed_exec=true to "
                "allow unconfined execution, or run `kirocrew setup` to be walked "
                "through the decision."
            )


def _doctor_live_target_pointer(issues: list[str]) -> None:
    """Report a live-target pointer that will refuse the next agent spawn.

    SILENT on a healthy host, like the installer-residue and cron-health sections: a
    fit pointer is the normal state and a line for it every run would be noise.

    It has a section at all because this condition is otherwise invisible until it bites.
    ``sandbox._materialize_live_target_mask_target`` is fail-closed on every Linux spawn:
    the pointer names the checkout the gateway ``execve``s into, a bind mask covers a NAME
    rather than an inode, and a symlink or a second hard link therefore leaves a writable
    path to those bytes inside every agent namespace. So the launcher refuses instead. The
    refusal is correct and its text already names the remedy — but it reaches the operator
    as a failed spawn plus a ``logger.warning`` in the gateway log, and the shapes that
    trigger it are ORDINARY operation for something else on the host: ``cp -al``,
    rsnapshot and other hard-link snapshot tools raise link counts on config files, and a
    dotfile manager may keep the pointer as a link into its own tree. Nobody did anything
    wrong, and the first symptom is that every agent stops starting. This is the place an
    operator looks for that.

    Linux only. The refusal is on the namespace launcher's path; a macOS Seatbelt profile
    denies by path rule and never needs a mount target, so naming it there would report a
    spawn outage that will not happen.

    The sentence is the launcher's own (``sandbox.live_target_pointer_unfitness``), not a
    paraphrase, so an operator who sees this line and later hits the refusal reads one
    diagnosis rather than two.
    """
    if not sys.platform.startswith("linux"):
        return
    try:
        unfit = sandbox.live_target_pointer_unfitness()
    except Exception as exc:  # noqa: BLE001 — doctor must survive a broken probe
        print("\nLive Target Pointer")
        print(f"  pointer:     ⚠️  could not check ({render._safe_display(exc)})")
        return
    if unfit is None:
        return
    # The refusal lives on the namespace launcher's path, which ``wrap_argv`` reaches only
    # when it actually WRAPS the child. On a host where it hands back an unwrapped argv,
    # ``_materialize_live_target_mask_target`` never runs, so the pointer is unfit and
    # NOTHING is currently refused. Reporting "spawns will be REFUSED" there is the same
    # false promise of an outage as reporting this on macOS, which this section already
    # declines to make.
    #
    # ``credential_mask_applies`` rather than a mode comparison of this module's own: it
    # lives beside those branches precisely so a caller whose argument depends on the mask
    # cannot drift from them, and it already counts BOTH unwrapped outcomes -- the "off"
    # tier and a backend-less host -- where reading the mode alone sees only the first.
    #
    # Still reported when unwrapped, because it is a real latent outage that starts the
    # moment the host confines a spawn -- but not counted as an issue, since nothing is
    # broken yet and doctor's exit code answers "is this install healthy NOW".
    try:
        confined = sandbox.credential_mask_applies(sandbox.configured_sandbox_mode())
    except Exception:  # noqa: BLE001 — an unreadable mode must not hide the pointer
        confined = True
    print("\nLive Target Pointer")
    if confined:
        print(f"  pointer:     ❌ agent spawns will be REFUSED — {unfit.path}")
    else:
        print(f"  pointer:     ⚠️  unfit, and will refuse spawns once confined — {unfit.path}")
    # Whole tokens: the remedy names a path and a ``find`` invocation the operator copies,
    # and the default wrap splits both.
    render._print_wrapped(unfit.detail)
    if confined:
        render._print_wrapped(
            "Until this is fixed every agent spawn on this host fails closed, and the "
            "only other notice is a warning in the gateway log."
        )
        issues.append("live-target pointer")
    else:
        render._print_wrapped(
            "This pointer is not what stops a spawn on this host: the launcher reaches "
            "the mask it would break only when it WRAPS a child, and this host hands the "
            "command over unwrapped or refuses it for a different reason. Whether agents "
            "start at all is the Sandbox section's answer, not this one. Fix the pointer "
            "before the host starts confining spawns, or the first one that does fails "
            "closed."
        )


def _doctor_masked_credential_aliases(issues: list[str]) -> None:
    """Report a masked credential leaf that will refuse the next agent spawn.

    The same job :func:`_doctor_live_target_pointer` does for the live-target pointer, for
    the same shape on the leaves whose bytes are a credential: ``sandbox`` refuses a spawn
    when one of them has a second hard link, because a mask binds a path and the second name
    reaches the same bytes unmasked. A hard link on a file in the home is ordinary operation
    for ``cp -al``, rsnapshot and other hard-link snapshot tools, so the condition appears
    without anybody doing anything wrong and the first symptom is that agents stop starting.

    Both confined launch paths issue this refusal -- the namespace launcher through
    :func:`sandbox.namespace_argv` and the Seatbelt profile through
    :func:`sandbox.sandbox_exec_argv` -- so the probe runs on Linux and on macOS. A platform
    with no confined launch path is skipped: naming the condition there would report an
    outage that cannot arrive.

    The sentence is the launcher's own, not a paraphrase, so an operator who reads this line
    and later meets the refusal reads one diagnosis rather than two.
    """
    if not (sys.platform.startswith("linux") or sys.platform == "darwin"):
        return
    try:
        aliased = sandbox.masked_credential_leaf_aliases()
    except Exception as exc:  # noqa: BLE001 — doctor must survive a broken probe
        print("\nMasked Credential Leaves")
        print(f"  aliases:     ⚠️  could not check ({render._safe_display(exc)})")
        return
    if not aliased:
        return
    # ``credential_mask_applies`` rather than a mode comparison of this module's own, for
    # the reason the pointer's section states: it counts BOTH unwrapped outcomes, so a host
    # that hands the command over unwrapped is not told it is about to lose every spawn.
    try:
        confined = sandbox.credential_mask_applies(sandbox.configured_sandbox_mode())
    except Exception:  # noqa: BLE001 — an unreadable mode must not hide the leaf
        confined = True
    print("\nMasked Credential Leaves")
    try:
        live_home = str(cli_doctor.config_dir())
    except Exception:  # noqa: BLE001 — an unresolvable home must not hide the leaf
        live_home = ""
    refusing = False
    masked_any = False
    outside_live_any = False
    for path, links, root, accounted in aliased:
        # Only the live home refuses, and only when a name could NOT be located. A leaf whose
        # every other name is located is masked for the spawn and nothing is refused, so
        # saying "REFUSED" for it sends the operator after a failure that is not coming --
        # the same error in the other direction as reporting nothing at all. Every other
        # spelling is reported and the spawn proceeds, because an unused home is masked by
        # nothing while it is absent and a refusal there would be reachable from inside a
        # sandbox.
        in_live = bool(live_home) and root == live_home
        if accounted:
            masked_any = True
            print(f"  alias:       ⚠️  masked for each spawn — {path} ({links} links)")
        elif confined and in_live:
            refusing = True
            print(f"  alias:       ❌ agent spawns will be REFUSED — {path} ({links} links)")
        elif in_live:
            print(f"  alias:       ⚠️  will refuse spawns once confined — {path} ({links} links)")
        else:
            outside_live_any = True
            print(f"  alias:       ⚠️  reported, spawns proceed — {path} ({links} links)")
        # Whole tokens: the remedy names a path and a ``find`` invocation the operator
        # copies, and the default wrap splits both.
        # An accounted leaf gets the search command WITHOUT the refusal sentence. The full
        # detail opens with "cannot mask", which is what a spawn raises with and the direct
        # contradiction of the "masked for each spawn" line above it.
        if accounted:
            render._print_wrapped(sandbox._masked_leaf_alias_search_hint(path))
        else:
            render._print_wrapped(sandbox._masked_leaf_multilink_detail(path, links))
    if refusing:
        render._print_wrapped(
            "Until this is fixed every agent spawn on this host fails closed, and the "
            "only other notice is a warning in the gateway log."
        )
        issues.append("masked credential leaf alias")
    elif confined:
        # Each sentence is selected by what was actually printed. A single fixed trailer
        # claimed these leaves sit outside the live data home, which is false for an
        # accounted leaf in the live home -- the case this host reaches whenever the auth
        # store keeps its staging link.
        reasons = ["No spawn is refused for these."]
        if masked_any:
            reasons.append(
                "Where every other name was located, those names are masked for each spawn "
                "too, so the bytes are unreachable from inside one."
            )
        if outside_live_any:
            reasons.append(
                "Where a name could not be located, the leaf is outside the live data home, "
                "which the launcher reports rather than refusing on, so that a file inside a "
                "home this install does not use cannot stop every launch."
            )
        reasons.append(
            "Remove the extra link anyway: a name no mask covers leaves the bytes readable, "
            "and this report is the only notice."
        )
        render._print_wrapped(" ".join(reasons))
    else:
        render._print_wrapped(
            "This is not what stops a spawn on this host yet: the launcher reaches the "
            "mask only when it WRAPS a child, and this host hands the command over "
            "unwrapped or refuses it for a different reason. Remove the extra link before "
            "the host starts confining spawns, or the first one that does fails closed."
        )
