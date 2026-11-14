import { AlertTriangle, ExternalLink, RefreshCw } from 'lucide-react'
import { Btn } from './ui'
import RemoteCrewSetupSkip from './RemoteCrewSetupSkip'
import { SetupShell } from './KiroPrerequisiteSetupShell'
import { CopyCommand } from './agentHarness'
import { i18nT } from '../i18n/t'

// The sandbox-unavailable screen of the Kiro prerequisite gate: the body that
// names the failing mechanism, and the remedy steps for each `sandbox_remedy`
// token. Its strings stay under the gate's catalog namespace.

// Docs section that explains every user-namespace denial mechanism and the
// AppArmor profile `service install` writes. Linked from the gate so the screen
// is a starting point rather than a dead end (issue #1660).
const SANDBOX_DOCS_URL =
  'https://github.com/kirodotdev/KiroCrew/blob/main/docs/guides/install.md' +
  '#linux-the-agent-sandbox-and-unprivileged-user-namespaces'

/**
 * The remedy for one `sandbox_remedy` token.
 *
 * The backend probe knows WHICH step failed — either unshare, or the mount that
 * makes the new namespace private — and with which errno, and those identify
 * the host mechanism — so the gate can name the actual fix instead of showing
 * `errno 1 (EPERM)` and a retry button. An unrecognised or empty token renders
 * nothing, and the screen falls back to the doctor pointer, which is still
 * strictly more than the bare errno it replaced.
 *
 * Exactly ONE command per mechanism, deliberately. The AppArmor case previously
 * also offered `aa-exec -p kirocrew-userns` for a hand-started gateway, which is
 * worse than no advice: entering a named profile is not permitted for an
 * unconfined user, and `aa-exec` execs the command anyway instead of failing, so
 * the user gets a remedy that looks applied and changes nothing. The profile is
 * attached by systemd (`AppArmorProfile=`), so installing the service is the
 * only path that actually applies it — and the desktop app reuses an existing
 * gateway on the port, so the service covers that install too. The container
 * mount case shows the one switch twice only because Docker and Kubernetes
 * spell it differently; both blocks apply the same change.
 *
 * Each command sits directly inside a `<pre>` rather than in a data structure:
 * a shell command is not copy, and `pre` is the i18n gate's documented
 * exemption for a literal that must not be translated.
 */
function remedySteps(remedy: string): React.ReactNode {
  switch (remedy) {
    case 'apparmor_userns':
      return (
        <ul className="mt-2 list-none space-y-3">
          <li className="text-sm leading-relaxed text-muted">
            {i18nT('components.kiroPrerequisiteGate.remedy_apparmor_service_install')}
            <CopyCommand>
              <code>kirocrew service install</code>
            </CopyCommand>
          </li>
        </ul>
      )
    case 'max_user_namespaces':
      return (
        <ul className="mt-2 list-none space-y-3">
          <li className="text-sm leading-relaxed text-muted">
            {i18nT('components.kiroPrerequisiteGate.remedy_max_user_namespaces')}
            <CopyCommand>
              <code>sudo sysctl -w user.max_user_namespaces=15000</code>
            </CopyCommand>
          </li>
        </ul>
      )
    case 'userns_cap_zero':
      // The probe read the cap as 0, so the fix is certain: raise it now, and
      // persist it so a reboot does not put the host back at 0.
      return (
        <ul className="mt-2 list-none space-y-3">
          <li className="text-sm leading-relaxed text-muted">
            {i18nT('components.kiroPrerequisiteGate.remedy_userns_cap_zero')}
            <CopyCommand>
              <code>sudo sysctl -w user.max_user_namespaces=15000</code>
            </CopyCommand>
            <CopyCommand>
              <code>{"echo 'user.max_user_namespaces=15000' | sudo tee /etc/sysctl.d/99-userns.conf"}</code>
            </CopyCommand>
          </li>
        </ul>
      )
    case 'userns_denied':
      return (
        <ul className="mt-2 list-none space-y-3">
          <li className="text-sm leading-relaxed text-muted">
            {i18nT('components.kiroPrerequisiteGate.remedy_userns_denied')}
            <CopyCommand>
              <code>sudo sysctl -w kernel.unprivileged_userns_clone=1</code>
            </CopyCommand>
          </li>
        </ul>
      )
    case 'mount_denied':
      // Both namespaces were granted and the launcher's first mount was refused:
      // a container runtime's default AppArmor profile (`deny mount`), which
      // Kubernetes applies on AppArmor nodes with no seccomp filter at all. The
      // fix is the container's policy, not the host, and needs no privilege —
      // the process already owns every capability inside its own namespace.
      // Two blocks, one switch: Docker and the Pod field are the same change
      // spelled for the two runtimes an operator can be on, and each pastes as
      // something usable on its own: the two flags drop into any `docker run`
      // line the operator already has, and the Pod block is real nested YAML,
      // not a dotted path, so it drops into a manifest as-is. The
      // `kirocrew-seccomp.json` profile the Docker flags name is explained by
      // the Linux sandbox guide linked below, which the now-shorter remedy
      // keeps in view. Each block carries a one-word caption so the reader
      // knows which of the two is theirs before copying.
      return (
        <ul className="mt-2 list-none space-y-3">
          <li className="text-sm leading-relaxed text-muted">
            {i18nT('components.kiroPrerequisiteGate.remedy_mount_denied')}
            <p className="mt-2 text-[11px] font-semibold uppercase tracking-[0.14em] text-muted">
              {i18nT('components.kiroPrerequisiteGate.remedy_mount_denied_docker')}
            </p>
            <CopyCommand>
              <code>--security-opt apparmor=unconfined --security-opt seccomp=kirocrew-seccomp.json</code>
            </CopyCommand>
            <p className="mt-2 text-[11px] font-semibold uppercase tracking-[0.14em] text-muted">
              {i18nT('components.kiroPrerequisiteGate.remedy_mount_denied_pod')}
            </p>
            <CopyCommand>
              <code className="whitespace-pre">{'securityContext:\n  appArmorProfile:\n    type: Unconfined'}</code>
            </CopyCommand>
          </li>
        </ul>
      )
    case 'no_user_ns':
      return (
        <p className="mt-2 text-sm leading-relaxed text-muted">
          {i18nT('components.kiroPrerequisiteGate.remedy_no_user_ns')}
        </p>
      )
    default:
      return null
  }
}

function SandboxRemedy({ remedy, transient }: { remedy: string; transient: boolean }) {
  const steps = remedySteps(remedy)
  return (
    <div className="mt-4 w-full max-w-lg text-left">
      {/* The heading only appears when there IS a fix to show. Over a section
          holding nothing but the diagnostic command it would promise a remedy it
          does not deliver. On the transient path the steps are conditional — the
          host may simply be busy — so the heading says so rather than asserting a
          fix the user may not need. */}
      {steps ? (
        <>
          <p className="text-[11px] font-semibold uppercase tracking-[0.14em] text-muted">
            {transient
              ? i18nT('components.kiroPrerequisiteGate.if_this_keeps_happening')
              : i18nT('components.kiroPrerequisiteGate.how_to_fix')}
          </p>
          {steps}
        </>
      ) : null}
      <p className="mt-2 text-sm leading-relaxed text-muted">
        {i18nT('components.kiroPrerequisiteGate.run_kirocrew_doctor_on_the_gateway_host_for_a_ful')}
      </p>
      <CopyCommand>
        <code>kirocrew doctor</code>
      </CopyCommand>
      <a
        className="mt-2 inline-flex items-center gap-1.5 text-[13px] font-medium text-accent hover:underline focus-ring"
        href={SANDBOX_DOCS_URL}
        rel="noopener noreferrer"
        target="_blank"
      >
        {i18nT('components.kiroPrerequisiteGate.linux_sandbox_guide')}
        <ExternalLink className="lucide-inline" />
      </a>
    </div>
  )
}

export function SandboxUnavailable({
  failureKind,
  detail,
  remedy,
  retrying,
  onRetry,
  allowRemoteSetup,
}: {
  failureKind: string
  detail: string
  remedy: string
  retrying: boolean
  onRetry: () => void
  allowRemoteSetup: boolean
}) {
  // One honest title for every kind — the CLI is installed, verification is
  // what failed — with the body carrying the mechanism, because the remedies
  // diverge sharply. A transient failure clears on retry and must NOT push the
  // user toward disabling their own isolation; a foreign outer sandbox means
  // this host's sandbox is fine; only 'no_backend' is a host-level verdict.
  //
  // The generic no_backend sentence ("this host provides no OS-level sandbox")
  // is FALSE under the Ubuntu AppArmor restriction: user namespaces work, the
  // kernel just denied the second step. It is equally false for a container
  // that granted both namespaces and then refused the launcher's first mount.
  // Those two mechanisms therefore override the body. The other tokens leave
  // it alone — for them the host genuinely offers no usable namespace, and
  // their remedy step carries the specifics.
  //
  // A cap the probe read as 0 wins for every kind: the ENOSPC verdict stays
  // transient (never cached), but its cause is certain, so neither the
  // temporary-limit body nor the generic no-sandbox one describes it.
  const capZero = remedy === 'userns_cap_zero'
  const body = capZero
    ? i18nT('components.kiroPrerequisiteGate.userns_cap_zero_body')
    : failureKind === 'transient'
      ? i18nT('components.kiroPrerequisiteGate.the_check_hit_a_temporary_limit_and_was_not_cach')
      : failureKind === 'foreign_sandbox'
        ? i18nT('components.kiroPrerequisiteGate.another_sandbox_already_confines_kiro_crew_so_it')
        : remedy === 'apparmor_userns'
          ? i18nT('components.kiroPrerequisiteGate.this_host_allows_user_namespaces_but_the_kernel_d')
          : remedy === 'mount_denied'
            ? i18nT('components.kiroPrerequisiteGate.this_container_grants_namespaces_but_refuses_mount')
            : i18nT('components.kiroPrerequisiteGate.this_host_provides_no_os_level_sandbox_so_kiro_c')
  // A momentary failure that clears on retry should not be dressed in the same
  // alarm red as a host-level verdict — the body immediately walks that back. A
  // disabled cap is a host-level verdict whatever the kind, so it is not one.
  const transient = failureKind === 'transient' && !capZero
  const tone = transient ? 'bg-accent-subtle text-accent' : 'bg-danger/10 text-danger-text'
  const eyebrowTone = transient ? 'text-accent' : 'text-danger'
  return (
    <SetupShell
      asideHeadline={i18nT('components.kiroPrerequisiteGate.sandbox_unavailable')}
      asideBody={i18nT('components.kiroPrerequisiteGate.kiro_crew_isolates_the_agent_in_an_os_level_sand')}
      footer={
        <Btn type="button" disabled={retrying} onClick={onRetry}>
          <RefreshCw className={`lucide-inline ${retrying ? 'animate-spin' : ''}`} />
          {i18nT('components.kiroPrerequisiteGate.check_again')}
        </Btn>
      }
    >
      <>
        <div className={`flex h-11 w-11 items-center justify-center rounded-xl ${tone}`}>
          <AlertTriangle className="lucide-inline" />
        </div>
        <p className={`mt-6 text-[12px] font-bold uppercase tracking-[0.16em] ${eyebrowTone}`}>
          {i18nT('components.kiroPrerequisiteGate.sandbox_unavailable')}
        </p>
        <h1 className="mt-2 text-3xl font-bold tracking-tight text-text-strong">
          {i18nT('components.kiroPrerequisiteGate.kiro_cli_is_installed_but_could_not_be_verified')}
        </h1>
        <p className="mt-3 max-w-lg text-sm leading-relaxed text-muted">{body}</p>
        {/* A foreign outer sandbox means this host is fine, so host remedies
            there would be advice to break a working setup. A transient failure
            still shows one when the probe named a mechanism: the cap case is
            reported transient forever, so suppressing it here was the difference
            between a fixable host and a dead end. */}
        {(failureKind === 'no_backend' || (transient && remedy) || capZero) && (
          <SandboxRemedy remedy={remedy} transient={transient} />
        )}
        {detail ? (
          <div className="mt-5 w-full max-w-lg text-left">
            <p className="text-[11px] font-semibold uppercase tracking-[0.14em] text-muted">
              {i18nT('components.kiroPrerequisiteGate.technical_detail')}
            </p>
            <pre className="mt-1 overflow-x-auto whitespace-pre-wrap break-words rounded-lg bg-bg-elevated p-3 text-xs text-muted">
              {detail}
            </pre>
          </div>
        ) : null}
        {allowRemoteSetup && <RemoteCrewSetupSkip />}
      </>
    </SetupShell>
  )
}
