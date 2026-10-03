import { useEffect, useRef, useState, type ReactNode } from 'react'
import { type QueryClient, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle,
  ArrowRight,
  Boxes,
  CheckCircle2,
  ChevronDown,
  Download,
  ExternalLink,
  Package,
  RefreshCw,
  ShieldCheck,
  Sparkles,
} from 'lucide-react'
import {
  ApiError,
  api,
  type AcpBackendProbe,
  type KiroPrerequisiteStatus,
} from '../api/client'
import { ACP_BACKEND_KAS, ACP_BACKEND_KIRO, acpBackendName, acpProbeConfirmsUse, agentChoiceSaved, setupMarkerErrorBody, setupMarkerErrorMessage } from '../api/acpBackend'
import { clearCachedModels } from '../providers/adapters/acp'
import {
  PANEL_CLASS,
  PINNED_FOOTER_CLASS,
  SCRIM_CLASS,
  SECTION_CLASS,
  ShellAside,
} from './OnboardingChapterShell'
import { safeGetItem, safeSetItem } from '../utils/safeStorage'
import { useScrollEdgesY } from '../hooks/useScrollEdges'
import { Badge, Btn, Card, SendBtn } from './ui'
import ErrorNotice from './ErrorNotice'
import {
  AgentDetailActions,
  AgentInstallDetail,
  AgentPickerRow,
  AgentStatusBadge,
  CopyCommand,
} from './agentHarness'

import { Trans } from 'react-i18next'
import { i18nT } from '../i18n/t'
const QUERY_KEY = ['kiro-prerequisite'] as const

export function kiroPrerequisiteRefetchInterval(
  status: KiroPrerequisiteStatus | undefined,
): number | false {
  if (status?.ready) return 30_000
  if (status && status.setup_allowed === false) return 3_000
  if (kiroPrerequisiteIsBlocking(status)) return 5_000
  return 30_000
}

// True while the full-screen first-run gate is the only thing the user can see.
// Two behaviors key off it: the faster poll above, and forcing that poll to probe
// the HOST rather than read the boot-time latch.
//
// Forcing matters because Kiro Crew no longer performs setup — the user installs
// Kiro CLI from kiro.dev and may sign in from a terminal. Neither of those
// touches the gateway, and the latched status is refreshed only at boot or on an
// explicit request, so a latch-reading poll can never observe them and the gate
// would hold forever behind a Check again button. Bounded deliberately: it costs
// two short `kiro-cli` spawns per interval, runs ONLY on this blocking screen,
// and stops the moment `ready` flips. A returning user never reaches it.
export function kiroPrerequisiteIsBlocking(
  status: KiroPrerequisiteStatus | undefined,
): boolean {
  if (!status || status.ready) return false
  // A non-owner cannot probe and is shown the "owner must finish setup" screen.
  if (status.setup_allowed === false) return false
  return !status.initial_setup_complete
}


// renders them as the first sentence of a paragraph — terminate them so the
// next sentence does not read as one run-on line.
export function asSentence(message: string): string {
  const trimmed = message.trim()
  if (!trimmed) return trimmed
  return /[.!?:;…]$/.test(trimmed) ? trimmed : `${trimmed}.`
}

// Shared full-screen chrome for every gate state. This is the SAME container the
// first-run onboarding chapters use (Import setup / Customize): the identical
// scrim, panel geometry, and accent aside with the identical mascot positions,
// imported from OnboardingChapterShell rather than re-declared here. Only the
// copy in the aside and the right-column content differ. `cardLabel` names the
// region for assistive tech.
function SetupShell({
  children,
  footer,
  cardLabel,
  asideHeadline,
  asideBody,
}: {
  children: ReactNode
  // Rendered OUTSIDE the scroll region, so a state whose content overflows the
  // fixed panel height still shows its closure action whole. A half-clipped
  // primary button reads as a rendering defect rather than a scroll affordance.
  footer?: ReactNode
  cardLabel?: string
  // The default aside says "Install Kiro CLI, sign in once…", which contradicts
  // a state whose headline is "already installed" and which deliberately offers
  // no install action. States like that pass their own copy so the two columns
  // of the same screen do not disagree.
  asideHeadline?: string
  asideBody?: string
}) {
  const label = cardLabel || i18nT('components.kiroPrerequisiteGate.your_crew_is_almost_ready')
  // A scroll cue on the internally-scrolling column: the panel height is fixed,
  // so the tallest states clip below the footer divider, and that divider then
  // reads as the end of the content rather than a fold. `attachContent` follows
  // the body because it swaps with the gate's state, changing the overflow.
  const [attachScroll, edges, , attachScrollContent] = useScrollEdgesY<HTMLDivElement>()
  return (
    <main className={SCRIM_CLASS} aria-label={label}>
      <div className={PANEL_CLASS}>
        <ShellAside
          copy={{
            ariaLabel: label,
            panelHeadline:
              asideHeadline || i18nT('components.kiroPrerequisiteGate.your_crew_is_almost_ready'),
            panelBody:
              asideBody
              || i18nT('components.kiroPrerequisiteGate.install_kiro_cli_sign_in_once_and_kiro_crew_will'),
            panelFootnote: i18nT(
              'components.kiroPrerequisiteGate.secure_setup_on_your_gateway_host',
            ),
          }}
        />
        {/* Same scroll structure as the chapters: the panel height is fixed and
            the right column scrolls internally. `my-auto` keeps the short states
            (status error / non-owner) optically centered without breaking the
            scroll on the tall two-step setup. The edge cues exist because that
            fixed height clips the tallest states, and the footer divider below
            otherwise reads as the end of the content rather than a fold — the
            overlay scrollbar fades when idle and leaves no standing sign. */}
        <section className={SECTION_CLASS}>
          <div className="relative flex min-h-0 flex-1 flex-col">
            <div
              ref={attachScroll}
              data-testid="gate-scroll-region"
              className="flex min-h-0 flex-1 flex-col overflow-y-auto"
            >
              <div ref={attachScrollContent} className="my-auto w-full px-6 py-8 sm:px-10 sm:py-10">{children}</div>
            </div>
            {/* `from-card` matches SECTION_CLASS's own `bg-card`, so the fade
                dissolves into the column rather than onto a mismatched surface.
                `bottom-0`/`top-0` of this relative wrapper are the footer divider
                and the top edge of the scroll region. */}
            {edges.top && (
              <div
                aria-hidden="true"
                data-testid="gate-scroll-cue-top"
                className="pointer-events-none absolute inset-x-0 top-0 z-10 h-8 bg-gradient-to-b from-card to-transparent"
              />
            )}
            {edges.bottom && (
              <div
                aria-hidden="true"
                data-testid="gate-scroll-cue-bottom"
                className="pointer-events-none absolute inset-x-0 bottom-0 z-10 h-8 bg-gradient-to-t from-card to-transparent"
              />
            )}
          </div>
          {footer ? (
            <div data-testid="gate-footer" className={`${PINNED_FOOTER_CLASS} shrink-0 border-t border-border px-6 pt-4 pb-[max(1rem,env(safe-area-inset-bottom))] sm:px-10 sm:pb-4`}>{footer}</div>
          ) : null}
        </section>
      </div>
    </main>
  )
}

function StepStatus({
  complete,
  current,
}: {
  complete: boolean
  current: boolean
}) {
  if (complete) {
    return <Badge variant="ok"><CheckCircle2 className="lucide-inline" /> {i18nT('components.kiroPrerequisiteGate.complete')}</Badge>
  }
  return <Badge variant={current ? 'aim' : 'muted'}>{current ? i18nT('components.kiroPrerequisiteGate.required') : i18nT('components.kiroPrerequisiteGate.waiting')}</Badge>
}


function OwnerSetupRequired({
  retrying,
  onRetry,
}: {
  retrying: boolean
  onRetry: () => void
}) {
  // When the re-auth banner is up, this viewer IS the owner on a session that
  // predates the configured owner id — "ask the owner" would tell them they
  // cannot fix what one sign-in fixes. Swap the body for the banner's own
  // remedy so the two surfaces give ONE instruction. Same event pair App.tsx
  // subscribes to; the seed probes the banner ELEMENT rather than importing
  // api/client's isAuthBannerShown, because this gate renders in suites that
  // mock that module wholesale — one boolean is not worth coupling the gate's
  // module graph (and every such mock factory) to the full client.
  const [authRequired, setAuthRequired] = useState<boolean>(
    () => typeof document !== 'undefined' && document.getElementById('mc-session-expired') !== null,
  )
  useEffect(() => {
    const onRequired = () => setAuthRequired(true)
    const onCleared = () => setAuthRequired(false)
    window.addEventListener('mc-auth-required', onRequired)
    window.addEventListener('mc-auth-cleared', onCleared)
    return () => {
      window.removeEventListener('mc-auth-required', onRequired)
      window.removeEventListener('mc-auth-cleared', onCleared)
    }
  }, [])
  return (
    <SetupShell>
      <>
        <div className="flex h-11 w-11 items-center justify-center rounded-xl bg-accent-subtle text-accent">
          <ShieldCheck className="lucide-inline" />
        </div>
        <p className="mt-6 text-[12px] font-bold uppercase tracking-[0.16em] text-accent">
          {authRequired
            ? i18nT('components.kiroPrerequisiteGate.sign_in_required')
            : i18nT('components.kiroPrerequisiteGate.gateway_setup_required')}
        </p>
        <h1 className="mt-2 text-3xl font-bold tracking-tight text-text-strong">
          {authRequired
            ? i18nT('components.kiroPrerequisiteGate.sign_in_again_to_continue')
            : i18nT('components.kiroPrerequisiteGate.the_gateway_owner_needs_to_finish_setup')}
        </h1>
        <p className="mt-3 max-w-lg text-sm leading-relaxed text-muted">
          {authRequired
            ? i18nT('api.client.stale_owner_session_sign_in_again')
            : i18nT('components.kiroPrerequisiteGate.ask_the_kiro_crew_owner_to_install_kiro_cli_and')}
        </p>
        {/* "Check again" re-probes readiness, which cannot change for this
            viewer until they sign back in — a retry that cannot succeed is a
            false affordance, so the re-auth state carries no button and the
            banner remains the single action. */}
        {!authRequired && (
          <div className="mt-6">
            <Btn type="button" disabled={retrying} onClick={onRetry}>
              <RefreshCw className={`lucide-inline ${retrying ? 'animate-spin' : ''}`} />
              {i18nT('components.kiroPrerequisiteGate.check_again')}
            </Btn>
          </div>
        )}
      </>
    </SetupShell>
  )
}

// Local memory of first-run completion, so a COLD load (empty React Query
// cache) can tell a returning user from a genuine first run before — or
// without — a successful status response. The gateway remains the authority:
// this only ever suppresses first-run setup chrome for someone the gateway
// already confirmed had completed setup, and it never grants session
// readiness (that stays server-driven via `ready`).
const SETUP_COMPLETE_KEY = 'kirocrew:kiro-setup-complete'

function rememberedSetupComplete(): boolean {
  return safeGetItem(SETUP_COMPLETE_KEY) === '1'
}

function SetupStatusError({
  message,
  retrying,
  onRetry,
}: {
  message: string
  retrying: boolean
  onRetry: () => void
}) {
  return (
    <SetupShell>
      <>
        <div className="flex h-11 w-11 items-center justify-center rounded-xl bg-danger/10 text-danger">
          <AlertTriangle className="lucide-inline" />
        </div>
        <p className="mt-6 text-[12px] font-bold uppercase tracking-[0.16em] text-danger">
          {i18nT('components.kiroPrerequisiteGate.setup_check_unavailable')}
        </p>
        <h1 className="mt-2 text-3xl font-bold tracking-tight text-text-strong">
          {i18nT('components.kiroPrerequisiteGate.we_could_not_check_kiro_cli')}
        </h1>
        {/* No hand-off: this gate stands between the user and the chat the
            hand-off would navigate to, and the failure is that kiro-cli — the
            agent runtime — could not even be checked, so there is no agent to
            hand it to. Try again is the remedy. */}
        <ErrorNotice
          className="mt-3 max-w-lg text-left"
          message={asSentence(message)}
          testId="kiro-gate-status-error"
        />
        <p className="mt-3 max-w-lg text-sm leading-relaxed text-muted">
          {i18nT('components.kiroPrerequisiteGate.retry_the_gateway_check_before_starting_a_sessio')}
        </p>
        <div className="mt-6">
          <SendBtn type="button" disabled={retrying} onClick={onRetry}>
            <RefreshCw className={`lucide-inline ${retrying ? 'animate-spin' : ''}`} />{' '}
            {i18nT('components.kiroPrerequisiteGate.try_again')}
          </SendBtn>
        </div>
      </>
    </SetupShell>
  )
}

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
/**
 * Split two sign-in commands into the run they share and the tails that differ.
 *
 * On a desktop install the resolved kiro-cli is the app's bundled copy, so both
 * commands open with the same ~100-character quoted absolute path and differ
 * only after `login`. Rendered as two full lines they read as one command shown
 * twice; rendering the shared run muted and only the tail at full weight puts
 * the choice where the eye lands. The cut falls on the last space inside the
 * common prefix so a tail always starts at a word boundary (`login` /
 * `login --use-device-flow ...`). The copied text is still the whole command,
 * read back from the DOM.
 */
function splitSharedCommandPrefix(a: string, b: string): [string, string, string] {
  let i = 0
  while (i < a.length && i < b.length && a[i] === b[i]) i++
  const cut = a.lastIndexOf(' ', i - 1) + 1
  return [a.slice(0, cut), a.slice(cut), b.slice(cut)]
}

/**
 * The two sign-in commands, rendered VERBATIM from the backend constants, never
 * catalog values: a translated command cannot be typed. Shown only once a CLI
 * exists to sign into — before that the install step owns the screen. Kiro Crew
 * does not run them; the footer's Check again reads the result.
 *
 * BOTH tiers are offered, because the sign-in page the bare command opens
 * presents a free Builder ID as a peer of organization SSO: a user on an SSO
 * plan who picks the wrong one authenticates successfully and only discovers
 * the mismatch later, as missing models. Naming the tier here makes it a
 * decision instead of a guess. Kiro Crew does not detect which one applies —
 * that would mean inspecting the host's identity configuration — so the copy
 * describes the choice and lets the user make it.
 *
 * Both commands are click-to-copy, like every other command on this screen: the
 * desktop app's bundled kiro-cli is served as a quoted absolute path that nobody
 * should have to retype into a terminal, and one typo restarts the loop. That
 * path is the same in both boxes, so on a bundled install it is rendered muted,
 * the differing tail carries the weight, and ONE hint before both boxes explains
 * what the path is before a first-time reader meets it.
 */
function SignInCommands({ status }: { status: KiroPrerequisiteStatus }) {
  const [shared, personalTail, ssoTail] = status.bundled_cli
    ? splitSharedCommandPrefix(status.login_command, status.sso_login_command)
    : ['', status.login_command, status.sso_login_command]
  const prefix = shared ? <span className="text-muted">{shared}</span> : null
  return (
    <div className="mt-3 space-y-3">
      {status.bundled_cli && (
        <p className="text-[12px] leading-relaxed text-muted">
          {i18nT('components.kiroPrerequisiteGate.sign_in_bundled_hint')}
        </p>
      )}
      <div>
        <p className="text-[13px] font-medium text-text">
          {i18nT('components.kiroPrerequisiteGate.sign_in_personal_label')}
        </p>
        <CopyCommand>
          <code>
            {prefix}
            {personalTail}
          </code>
        </CopyCommand>
      </div>
      <div>
        <p className="text-[13px] font-medium text-text">
          {i18nT('components.kiroPrerequisiteGate.sign_in_sso_label')}
        </p>
        <CopyCommand>
          <code>
            {prefix}
            {ssoTail}
          </code>
        </CopyCommand>
        <p className="mt-1.5 text-[12px] leading-relaxed text-muted">
          {i18nT('components.kiroPrerequisiteGate.sign_in_sso_hint')}
        </p>
      </div>
      <p className="text-[12px] leading-relaxed text-muted">
        {i18nT('components.kiroPrerequisiteGate.sign_in_method_note')}
      </p>
    </div>
  )
}

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

function SandboxUnavailable({
  failureKind,
  detail,
  remedy,
  retrying,
  onRetry,
}: {
  failureKind: string
  detail: string
  remedy: string
  retrying: boolean
  onRetry: () => void
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
  const body =
    failureKind === 'transient'
      ? i18nT('components.kiroPrerequisiteGate.the_check_hit_a_temporary_limit_and_was_not_cach')
      : failureKind === 'foreign_sandbox'
        ? i18nT('components.kiroPrerequisiteGate.another_sandbox_already_confines_kiro_crew_so_it')
        : remedy === 'apparmor_userns'
          ? i18nT('components.kiroPrerequisiteGate.this_host_allows_user_namespaces_but_the_kernel_d')
          : remedy === 'mount_denied'
            ? i18nT('components.kiroPrerequisiteGate.this_container_grants_namespaces_but_refuses_mount')
            : i18nT('components.kiroPrerequisiteGate.this_host_provides_no_os_level_sandbox_so_kiro_c')
  // A momentary failure that clears on retry should not be dressed in the same
  // alarm red as a host-level verdict — the body immediately walks that back.
  const transient = failureKind === 'transient'
  const tone = transient ? 'bg-accent-subtle text-accent' : 'bg-danger/10 text-danger'
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
        {(failureKind === 'no_backend' || (transient && remedy)) && (
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
      </>
    </SetupShell>
  )
}

function CliOutdated({
  updateCommand,
  bundledCli,
  updateError,
  updating,
  retrying,
  onUpdate,
  onRetry,
}: {
  updateCommand: string
  /** The desktop app's own copy, which the gateway refuses to self-update. */
  bundledCli: boolean
  updateError: string
  updating: boolean
  retrying: boolean
  onUpdate: () => void
  onRetry: () => void
}) {
  // The CLI is installed but too old to expose the `acp` subcommand Kiro Crew
  // launches every session through — so it would fail at session-create rather
  // than here. The probe runs for a signed-out CLI too, so this card says
  // nothing about sign-in: a "signed in" claim here would be false for that
  // reader. The remedy is an UPDATE in place, not a reinstall, and unlike the
  // install/sign-in steps Kiro Crew CAN run this one for the user (it is the
  // CLI's own self-update). We therefore offer a button that runs it AND show
  // the command for anyone who would rather run it on the host themselves.
  return (
    <SetupShell
      asideHeadline={i18nT('components.kiroPrerequisiteGate.kiro_cli_update_needed')}
      asideBody={i18nT('components.kiroPrerequisiteGate.this_kiro_cli_is_too_old_for_the_acp_command')}
      footer={
        <div className="flex items-start justify-between gap-4">
          <div className="min-w-0">
            <SendBtn type="button" disabled={updating || retrying} onClick={onUpdate}>
              <Download className={`lucide-inline ${updating ? 'animate-pulse' : ''}`} />
              {updating
                ? i18nT('components.kiroPrerequisiteGate.updating_kiro_cli')
                : i18nT('components.kiroPrerequisiteGate.update_kiro_cli')}
            </SendBtn>
            {/* The button really runs the update: `update_cli` spawns the CLI's
                own `update` subcommand on the gateway host. Not said for the
                desktop app's bundled copy, which the gateway refuses to update
                in place (it ships inside the signed app), so the claim would be
                false there. The command is verbatim in a <code>, interpolated
                rather than a catalog value: a translated command cannot be run. */}
            {!bundledCli && (
              <p className="mt-2 mb-0 text-[12px] leading-relaxed text-muted" data-testid="kiro-gate-update-runs-on-host">
                <Trans
                  i18nKey="components.kiroPrerequisiteGate.update_kiro_cli_runs_on_host"
                  values={{ command: updateCommand }}
                  components={[<code key="command" />]}
                />
              </p>
            )}
          </div>
          <Btn type="button" disabled={updating || retrying} onClick={onRetry}>
            <RefreshCw className={`lucide-inline ${retrying ? 'animate-spin' : ''}`} />
            {i18nT('components.kiroPrerequisiteGate.check_again')}
          </Btn>
        </div>
      }
    >
      <>
        <div className="flex h-11 w-11 items-center justify-center rounded-xl bg-accent-subtle text-accent">
          <Download className="lucide-inline" />
        </div>
        <p className="mt-6 text-[12px] font-bold uppercase tracking-[0.16em] text-accent">
          {i18nT('components.kiroPrerequisiteGate.kiro_cli_update_needed')}
        </p>
        <h1 className="mt-2 text-3xl font-bold tracking-tight text-text-strong">
          {i18nT('components.kiroPrerequisiteGate.your_kiro_cli_is_out_of_date')}
        </h1>
        <p className="mt-3 max-w-lg text-sm leading-relaxed text-muted">
          {i18nT('components.kiroPrerequisiteGate.kiro_cli_is_installed_but_too_old')}
        </p>
        {/* Kiro Crew runs the update for the user via the button below, but the
            command is shown too — some hosts prefer to run it themselves, and it
            is the one thing a support conversation needs. Verbatim in a <code>,
            never a catalog value: a translated command cannot be run. */}
        <div className="mt-5 w-full max-w-lg text-left">
          <p className="text-[11px] font-semibold uppercase tracking-[0.14em] text-muted">
            {i18nT('components.kiroPrerequisiteGate.update_command_label')}
          </p>
          <CopyCommand>
            <code>{updateCommand}</code>
          </CopyCommand>
        </div>
        {/* Verbatim and untranslated: it names why the self-update did not
            complete (ErrorNotice's body is whitespace-pre-wrap, so the CLI
            output keeps its shape). It appears in place after the button press
            with no route change, which is what role="alert" is for. */}
        {/* No hand-off: kiro-cli — the agent runtime — is the thing that is
            outdated here, and this gate hides the chat the hand-off would open. */}
        <ErrorNotice
          className="mt-4 w-full max-w-lg text-left text-xs"
          messageClassName="font-mono"
          title={i18nT('components.kiroPrerequisiteGate.the_update_attempt_failed')}
          message={updateError || null}
          testId="kiro-gate-update-error"
        />
        {updateError ? (
          <p className="mt-2 max-w-lg text-left text-[13px] leading-relaxed text-muted">
            {i18nT('components.kiroPrerequisiteGate.attempt_failed_remedy', { action: i18nT('components.kiroPrerequisiteGate.update_kiro_cli') })}
          </p>
        ) : null}
      </>
    </SetupShell>
  )
}

function AgentSpecsMissing({
  specs,
  repairError,
  retrying,
  onRepair,
}: {
  specs: string[]
  repairError: string
  retrying: boolean
  onRepair: () => void
}) {
  return (
    <SetupShell
      asideHeadline={i18nT('components.kiroPrerequisiteGate.agent_specs_missing')}
      asideBody={i18nT('components.kiroPrerequisiteGate.kiro_crew_installs_the_agent_specs_kiro_cli_load')}
    >
      <>
        <div className="flex h-11 w-11 items-center justify-center rounded-xl bg-danger/10 text-danger">
          <AlertTriangle className="lucide-inline" />
        </div>
        <p className="mt-6 text-[12px] font-bold uppercase tracking-[0.16em] text-danger">
          {i18nT('components.kiroPrerequisiteGate.agent_specs_missing')}
        </p>
        <h1 className="mt-2 text-3xl font-bold tracking-tight text-text-strong">
          {i18nT('components.kiroPrerequisiteGate.kiro_crew_s_agent_specs_are_not_installed')}
        </h1>
        <p className="mt-3 max-w-lg text-sm leading-relaxed text-muted">
          {i18nT('components.kiroPrerequisiteGate.kiro_crew_writes_its_own_agent_specs_where_kiro')}
        </p>
        <div className="mt-5 w-full max-w-lg text-left">
          <p className="text-[11px] font-semibold uppercase tracking-[0.14em] text-muted">
            {i18nT('components.kiroPrerequisiteGate.missing')}
          </p>
          <pre className="mt-1 overflow-x-auto whitespace-pre-wrap break-words rounded-lg bg-bg p-3 text-xs text-muted">
            {specs.join('\n')}
          </pre>
        </div>
        {/* Verbatim and untranslated: it names the failing install step, which is
            the one thing a support conversation actually needs. Its absence is
            also informative — it means no repair has been attempted yet.
            `role="alert"` because it appears in place after the button press with
            no route change, so a screen reader would otherwise get nothing. */}
        {/* No hand-off: the agent specs kiro-cli refused are what the agent runs
            on, and this gate hides the chat the hand-off would open. */}
        <ErrorNotice
          className="mt-4 w-full max-w-lg text-left text-xs"
          messageClassName="font-mono"
          title={i18nT('components.kiroPrerequisiteGate.the_repair_attempt_failed')}
          message={repairError || null}
          testId="kiro-gate-repair-error"
        />
        {/* Plain-language next step: the verbatim output above is for a bug
            report, not something a user can act on by itself. */}
        {repairError ? (
          <p className="mt-2 max-w-lg text-left text-[13px] leading-relaxed text-muted">
            {i18nT('components.kiroPrerequisiteGate.attempt_failed_remedy', { action: i18nT('components.kiroPrerequisiteGate.check_again') })}
          </p>
        ) : null}
        {/* The self-diagnosis dead end: `kiro-cli diagnostic` is the first command
            anyone reaches for, and it refuses with "Kiro CLI app is not running"
            until the app is launched — which reads as the cause and is not. */}
        <p className="mt-5 max-w-lg text-[13px] leading-relaxed text-muted">
          {i18nT('components.kiroPrerequisiteGate.if_you_are_diagnosing_this_from_a_terminal_kiro')}
        </p>
        <div className="mt-6">
          <Btn type="button" disabled={retrying} onClick={onRepair}>
            <RefreshCw className="lucide-inline" />
            {i18nT('components.kiroPrerequisiteGate.check_again')}
          </Btn>
        </div>
      </>
    </SetupShell>
  )
}

function AgentSpecsRejected({
  specs,
  reason,
  repairError,
  retrying,
  onRepair,
}: {
  specs: string[]
  reason: string
  repairError: string
  retrying: boolean
  onRepair: () => void
}) {
  return (
    <SetupShell
      asideHeadline={i18nT('components.kiroPrerequisiteGate.agent_specs_rejected')}
      asideBody={i18nT('components.kiroPrerequisiteGate.kiro_crew_installs_the_agent_specs_kiro_cli_load')}
    >
      <>
        <div className="flex h-11 w-11 items-center justify-center rounded-xl bg-danger/10 text-danger">
          <AlertTriangle className="lucide-inline" />
        </div>
        <p className="mt-6 text-[12px] font-bold uppercase tracking-[0.16em] text-danger">
          {i18nT('components.kiroPrerequisiteGate.agent_specs_rejected')}
        </p>
        <h1 className="mt-2 text-3xl font-bold tracking-tight text-text-strong">
          {i18nT('components.kiroPrerequisiteGate.kiro_cli_will_not_load_kiro_crew_s_agent_specs')}
        </h1>
        <p className="mt-3 max-w-lg text-sm leading-relaxed text-muted">
          {i18nT('components.kiroPrerequisiteGate.the_files_are_on_disk_but_kiro_cli_refuses_them')}
        </p>
        <div className="mt-5 w-full max-w-lg text-left">
          <p className="text-[11px] font-semibold uppercase tracking-[0.14em] text-muted">
            {i18nT('components.kiroPrerequisiteGate.rejected')}
          </p>
          <pre className="mt-1 overflow-x-auto whitespace-pre-wrap break-words rounded-lg bg-bg p-3 text-xs text-muted">
            {specs.join('\n')}
          </pre>
        </div>
        {/* Kiro CLI's own words, verbatim and untranslated. It names the file and
            the construct it refused, which is the difference between "my agents
            stopped working" and a report someone can act on. */}
        {reason ? (
          <div className="mt-4 w-full max-w-lg text-left">
            <p className="text-[11px] font-semibold uppercase tracking-[0.14em] text-muted">
              {i18nT('components.kiroPrerequisiteGate.kiro_cli_s_reason')}
            </p>
            <pre className="mt-1 overflow-x-auto whitespace-pre-wrap break-words rounded-lg bg-bg p-3 text-xs text-muted">
              {reason}
            </pre>
          </div>
        ) : null}
        {/* No hand-off: the agent specs kiro-cli refused are what the agent runs
            on, and this gate hides the chat the hand-off would open. */}
        <ErrorNotice
          className="mt-4 w-full max-w-lg text-left text-xs"
          messageClassName="font-mono"
          title={i18nT('components.kiroPrerequisiteGate.the_repair_attempt_failed')}
          message={repairError || null}
          testId="kiro-gate-repair-error"
        />
        {/* Plain-language next step: the verbatim output above is for a bug
            report, not something a user can act on by itself. */}
        {repairError ? (
          <p className="mt-2 max-w-lg text-left text-[13px] leading-relaxed text-muted">
            {i18nT('components.kiroPrerequisiteGate.attempt_failed_remedy', { action: i18nT('components.kiroPrerequisiteGate.check_again') })}
          </p>
        ) : null}
        {/* Deliberately does not promise a rewrite. The button re-asks kiro-cli
            rather than regenerating the spec: the file is already on disk, and a
            rebuild would discard a concurrent MCP toggle's tools/allowedTools
            grant. The leading cause is a kiro-cli upgrade, which re-checking
            cannot fix, so the two remedies get their own labelled lines rather
            than sitting mid-sentence in a muted paragraph. The commands live in
            <code> outside the catalog: a translator must not be able to alter a
            string the user pastes into a shell, and prose cannot be copied. */}
        <p className="mt-4 max-w-lg text-[13px] leading-relaxed text-muted">
          {i18nT('components.kiroPrerequisiteGate.repair_rewrites_the_specs_kiro_crew_owns')}
        </p>
        <ul className="mt-3 w-full max-w-lg list-none space-y-2 text-left">
          <li className="text-sm leading-relaxed text-muted">
            {i18nT('components.kiroPrerequisiteGate.remedy_spec_rejected_update')}
          </li>
          <li className="text-sm leading-relaxed text-muted">
            {i18nT('components.kiroPrerequisiteGate.remedy_spec_rejected_rewrite')}
            <CopyCommand>
              <code>kirocrew setup --agent-only --clean</code>
            </CopyCommand>
          </li>
        </ul>
        {/* Tightened deliberately: the remedy lines and the copy block added
            enough height to push this button under the fold at a 1280x800
            viewport, and the card's primary action must stay on screen. */}
        <div className="mt-4">
          <Btn type="button" disabled={retrying} onClick={onRepair}>
            <RefreshCw className="lucide-inline" />
            {i18nT('components.kiroPrerequisiteGate.check_again')}
          </Btn>
        </div>
      </>
    </SetupShell>
  )
}

// ── Agent choice ─────────────────────────────────────────────────────────────
//
// Kiro CLI is the DEFAULT agent, not the only one: the gateway can drive any
// selectable ACP harness (Settings → Agent). This gate used to check Kiro CLI
// alone, so an operator who runs Claude Code (or Codex, …) and has no Kiro CLI
// was held on "Set up Kiro" forever. The gate now asks the configured backend:
// Kiro checks apply only while Kiro CLI is the configured agent.

/** The config field Settings → Agent owns; the same key is written here. */
const ACP_BACKEND_CONFIG_KEY = 'agent.acp_backend'
/**
 * The core spells the Kiro CLI backend as the empty string — a real value. The
 * one definition lives in acpBackend.ts, beside `isKiroBackend`, so this gate
 * and Settings → Agent cannot drift on what "kiro" is spelled as.
 */
const KIRO_BACKEND = ACP_BACKEND_KIRO
/** Shared with Settings → Agent so a switch made here is what that panel reads. */
const CONFIG_QUERY_KEY = ['kirocrewConfig'] as const
const BACKENDS_QUERY_KEY = ['acpBackends'] as const
/**
 * Poll while the picker is on screen, so an install is noticed without a click.
 * Matched to the server's probe cache (`backend_install.CACHE_TTL_SECONDS`, 30s)
 * like Settings → Agent's `PROBE_REFRESH_MS`: the endpoint serves that cache, so
 * polling faster only returns the same bytes. The per-agent Check again drops
 * the cache for a user who cannot wait.
 */
const BACKENDS_POLL_MS = 30_000

/**
 * Seed the cache with ONE re-probed row and re-read the prerequisite status,
 * which is where the recheck's marker write (`initial_setup_complete`) and the
 * sandbox verdict land. Shared by the picker's Check again and the gate's own
 * marker-persisting recheck so both leave the caches in the same state.
 */
function applyRecheckedBackend(qc: QueryClient, backend: AcpBackendProbe): void {
  qc.setQueryData<{ backends: AcpBackendProbe[] }>(BACKENDS_QUERY_KEY, prev =>
    prev
      ? {
          backends: prev.backends.some(b => b.id === backend.id)
            ? prev.backends.map(b => (b.id === backend.id ? backend : b))
            : [...prev.backends, backend],
        }
      : { backends: [backend] },
  )
  void qc.invalidateQueries({ queryKey: QUERY_KEY })
}

/**
 * First-run readiness matches record_independent_backend_setup's durable marker
 * rule for independent harnesses: installation must be confirmed, not unknown.
 * The gate, polling and picker share this verdict so only a selectable, active
 * harness whose session could start — the host has an OS sandbox backend, or it
 * permits unsandboxed exec — can finish setup. KAS additionally
 * needs kiro-cli ACP support rather than independent setup eligibility.
 */
function configuredBackendCanStart(
  status: KiroPrerequisiteStatus | undefined,
  probe: AcpBackendProbe | undefined,
): boolean {
  // The probe row's own verdict is the one Settings > Agent Harness reads too
  // (`acpProbeConfirmsUse`); only the host and setup checks below are this gate's.
  if (!status || !probe || !acpProbeConfirmsUse(probe) || status.sandbox_unavailable
    // A session can start when the host has an OS backend OR it permits
    // unsandboxed exec (platform default / operator opt-in). Fail closed on an
    // older gateway that sends neither field: `=== true` reads undefined as not
    // permitted. An enforced harness is still held back by the
    // `sandbox_blocked_backends` check below whichever way this resolves.
    || !(status.sandbox_backend_available === true
      || status.unsandboxed_exec_permitted === true)) return false
  if (status.sandbox_blocked_backends?.includes(probe.id)) return false
  // KAS is kiro-cli's relay: it completes first-run setup on the kiro-cli
  // ACP-support rule, the SAME rule record_independent_backend_setup enforces
  // (by the same id) before it writes the marker. KAS also loads Kiro Crew's
  // agent specs, so a missing or rejected spec keeps the gate on its repair card.
  if (probe.id === ACP_BACKEND_KAS) {
    return status.acp_supported !== false
      && (status.missing_agent_specs ?? []).length === 0
      && (status.rejected_agent_specs ?? []).length === 0
  }
  return probe.independent_setup === true
}

/**
 * The sandbox is off by CHOICE, not by host failure.
 *
 * `sandbox_facts` (kiro_prerequisite.py) answers two questions at once:
 * `sandbox_backend_available` is whether this host can build an OS sandbox at
 * all, and `sandbox_blocked_backends` is every runtime-enforced harness whose
 * credential mask would not apply at the operator's configured tier. With
 * `agent.sandbox: off` on a host whose sandbox works, the first is true and the
 * second lists every enforced harness — so "this host cannot provide that
 * sandbox" would be false, and Check again / `kirocrew doctor` re-measure a host
 * that is fine. The remedy is the setting, and only the setting.
 */
function sandboxOffFor(
  status: KiroPrerequisiteStatus | undefined,
  probe: AcpBackendProbe | undefined,
): boolean {
  return !!status && !!probe
    && !status.sandbox_unavailable
    && status.sandbox_backend_available === true
    && (status.sandbox_blocked_backends?.includes(probe.id) ?? false)
}

/**
 * When the harness probe re-polls. Only while a setup screen is what the user
 * sees: once the dashboard is open — Kiro ready, a returning user, a usable
 * configured agent — the answer that let them through is not re-litigated, and
 * a configured agent that is NOT usable on an established install is reported
 * by its first turn, not by a poll behind a screen nobody is looking at.
 */
export function acpBackendsRefetchInterval(
  status: KiroPrerequisiteStatus | undefined,
  otherBackendConfigured: boolean,
  configured: AcpBackendProbe | undefined,
): number | false {
  if (!kiroPrerequisiteIsBlocking(status)) return false
  if (otherBackendConfigured && configuredBackendCanStart(status, configured)) return false
  return BACKENDS_POLL_MS
}

/** The backends this screen offers as alternatives to Kiro CLI. */
function otherCodingAgents(backends: AcpBackendProbe[]): AcpBackendProbe[] {
  return backends.filter(
    b => b.independent_setup === true && b.selectable !== false,
  )
}

/**
 * The platform label when the gateway named one this screen can NAME, else ''.
 *
 * The gateway reports one of these three for a host it recognises, and something
 * else when it does not or will not say: `"gateway"` for a non-owner, `"Unknown"`
 * for an OS it has no label for, or the raw `sys.platform` value. Anything
 * outside the set renders as no platform, so the intro never reads "Unknown
 * gateway host" while the Kiro card, one screen region below, admits it does not
 * know the OS. Written as comparisons rather than a lookup table: these are the
 * gateway's wire labels, and the i18n gate reads a string table as copy.
 */
export function knownPlatform(platform: string | undefined): string {
  return platform === 'Windows' || platform === 'macOS' || platform === 'Linux' ? platform : ''
}

/**
 * The install command for Kiro CLI, for the gateway's platform.
 *
 * Kiro Crew still does not RUN it: the command is shown for the user to paste on
 * the gateway host, and Kiro's own setup page stays one click away for every
 * other install route (RPM, AppImage, musl). The commands are `<code>` children,
 * never catalog values — a translated command cannot be typed.
 */
function KiroInstallCommands({ platform }: { platform: string }) {
  const windows = platform === 'Windows'
  const unix = platform === 'macOS' || platform === 'Linux'
  const known = knownPlatform(platform) !== ''
  return (
    <div className="space-y-3">
      <p className="text-[13px] leading-relaxed text-text">
        {known
          ? i18nT('components.kiroPrerequisiteGate.install_kiro_cli_run_on_host', { platform })
          : i18nT('components.kiroPrerequisiteGate.install_kiro_cli_run_for_platform')}
      </p>
      {!windows && (
        <div>
          {!known && (
            <p className="text-[12px] font-medium text-muted">
              {i18nT('components.kiroPrerequisiteGate.install_command_macos_linux')}
            </p>
          )}
          <CopyCommand>
            <code>curl -fsSL https://cli.kiro.dev/install | bash</code>
          </CopyCommand>
        </div>
      )}
      {!unix && (
        <div>
          <p className="text-[12px] font-medium text-muted">
            {i18nT('components.kiroPrerequisiteGate.install_command_windows')}
          </p>
          <CopyCommand>
            <code>irm &apos;https://cli.kiro.dev/install.ps1&apos; | iex</code>
          </CopyCommand>
        </div>
      )}
    </div>
  )
}

/**
 * "Use other coding agents": a collapsed alternative to Kiro CLI, modelled on
 * Settings → Agent (same probe, same config key, same install commands).
 *
 * Collapsed by default because Kiro CLI is the recommended path; opened by
 * default when the config already names another agent, because then this
 * section — not the Kiro cards — is what stands between the user and the app.
 * Choosing an agent writes the config; the gate then re-reads it and opens the
 * dashboard as soon as that agent is usable. Nothing is installed from here.
 */
function OtherCodingAgents({
  status,
  configured,
  backends,
  loading,
  failed,
  onRetryBackends,
}: {
  status: KiroPrerequisiteStatus
  configured: string
  backends: AcpBackendProbe[]
  loading: boolean
  failed: boolean
  onRetryBackends: () => void
}) {
  const qc = useQueryClient()
  const others = otherCodingAgents(backends)
  const configuredOther = configured !== KIRO_BACKEND ? configured : ''
  const [open, setOpen] = useState(() => configuredOther !== '')
  const [picked, setPicked] = useState<string | null>(null)
  // Resolved every render, like Settings → Agent's highlight: the list arrives
  // after first paint, so seeding state would pin the choice to a guess.
  const shownId =
    picked !== null && others.some(b => b.id === picked)
      ? picked
      : others.some(b => b.id === configuredOther)
        ? configuredOther
        : (others.find(b => configuredBackendCanStart(status, b))?.id ?? others[0]?.id ?? '')
  const shown = others.find(b => b.id === shownId)
  const configuredProbe = backends.find(b => b.id === configuredOther)

  const switchMut = useMutation({
    mutationFn: (value: string) => api.patchConfig(ACP_BACKEND_CONFIG_KEY, value),
    // Runs on a failure too, when the gateway reports the choice as saved: the
    // config on disk names the new agent whatever the status code says, and a
    // gate that keeps reading the old one holds a usable agent behind setup.
    onSettled: (_data, error) => {
      if (error && !agentChoiceSaved(error)) return
      // Same invalidations as Settings → Agent: a model list cached for the
      // previous agent would otherwise offer models the new one cannot run.
      clearCachedModels()
      void qc.resetQueries({ queryKey: ['available-models'] })
      return qc.invalidateQueries({ queryKey: CONFIG_QUERY_KEY })
    },
  })
  // Re-take ONE verdict with the gateway's cached absence dropped first — the
  // only way an install done after boot stops reading "restart needed". The
  // sandbox verdict rides on the prerequisite status, not on the probe, so that
  // is re-read too: a row blocked by the sandbox offers this same button.
  const recheckMut = useMutation({
    onMutate: () => qc.cancelQueries({ queryKey: BACKENDS_QUERY_KEY }),
    mutationFn: (value: string) => api.acpBackendRecheck(value),
    onSuccess: ({ backend }) => applyRecheckedBackend(qc, backend),
    // A 503 `setup_marker_write_failed` carries the fresh probe row: the re-probe
    // itself succeeded and only the marker write did not, so the row is applied
    // exactly as on success. The marker-write message still surfaces below, in
    // place of the generic recheck-failed text.
    onError: error => {
      const row = setupMarkerErrorBody(error)?.backend
      if (row) applyRecheckedBackend(qc, row)
    },
  })

  const panelId = 'other-coding-agents-panel'
  const name = shown ? acpBackendName(shown) : ''
  const busy = switchMut.isPending || recheckMut.isPending
  const markerWriteError = setupMarkerErrorMessage(switchMut.error)
  // What failed is the save, not the agent: the row above the notice can be
  // reporting that same agent installed and ready, and both are true. Each
  // notice names the button that retries it — "Use {{name}}" or "Use Kiro CLI
  // instead" — the way the recheck notice names Check again: a bare "Try
  // again" pointed at a control this screen does not have.
  const switchErrorFor = (backend: string): string | null =>
    switchMut.isError && switchMut.variables === backend
      ? (markerWriteError
        ?? (backend === KIRO_BACKEND
          ? i18nT('components.kiroPrerequisiteGate.could_not_switch_to_kiro')
          : i18nT('components.kiroPrerequisiteGate.could_not_save_agent_choice', { name })))
      : null
  // The yellow notice below says the configured agent is not ready, and it
  // carries the "switch agents later in Settings > Agent Harness" line with
  // its Use Kiro CLI instead button. The list footer says that same line;
  // while the notice is up the footer would say it twice on one screen.
  const configuredNotReady = !!(
    configuredOther && configuredProbe && !configuredBackendCanStart(status, configuredProbe)
  )

  return (
    <div className="mb-5 rounded-xl border border-border bg-bg-elevated/40" data-testid="other-coding-agents">
      <button
        type="button"
        aria-expanded={open}
        aria-controls={panelId}
        onClick={() => setOpen(v => !v)}
        className="flex w-full cursor-pointer items-center justify-between gap-3 rounded-xl border-none bg-transparent px-4 py-3 text-left hover:bg-bg-hover focus-ring"
      >
        <span className="flex items-center gap-2 text-sm font-semibold text-text-strong">
          <Boxes className="lucide-inline text-muted" />
          {i18nT('components.kiroPrerequisiteGate.use_other_coding_agents')}
        </span>
        <ChevronDown
          aria-hidden="true"
          strokeWidth={2.5}
          className={`h-6 w-6 shrink-0 text-muted transition-transform ${open ? 'rotate-180' : ''}`}
        />
      </button>
      {open && (
        <div id={panelId} className="space-y-4 border-t border-border px-4 py-4">
          {configuredNotReady && configuredProbe && (
            <div className="rounded-lg border border-warn/40 bg-warn-subtle px-3 py-2.5 text-[13px] leading-relaxed text-text">
              <p>
                {i18nT('components.kiroPrerequisiteGate.configured_agent_not_ready', {
                  name: acpBackendName(configuredProbe),
                })}
              </p>
              <p className="mt-1">{i18nT('components.kiroPrerequisiteGate.switch_agent_later_in_settings')}</p>
              <Btn
                type="button"
                className="mt-2 min-h-9 rounded-lg border-border-strong bg-card px-3 text-[13px] shadow-sm"
                disabled={busy}
                onClick={() => switchMut.mutate(KIRO_BACKEND)}
              >
                {i18nT('components.kiroPrerequisiteGate.use_kiro_cli_instead')}
              </Btn>
              {/* No hand-off: the switch back to Kiro CLI did not save, and this
                  setup gate hides the chat the hand-off would open. The button
                  above is the retry, so the notice sits beside it. */}
              <ErrorNotice
                className="mt-2 text-xs"
                message={switchErrorFor(KIRO_BACKEND)}
                testId="use-kiro-instead-error"
              />
            </div>
          )}
          {/* The intro ends "Pick one that is installed on the gateway host",
              so it renders only over a list there is something to pick from.
              Over the empty states — still checking, the probe failed, this
              build offers none — that instruction has nothing to point at, and
              above "offers no other coding agents" it contradicted the line
              below it. Each empty state's own line carries the whole message
              (the failure notice names Settings > Agent Harness itself). */}
          {others.length > 0 && (
            <p className="text-[13px] leading-relaxed text-muted">
              {i18nT('components.kiroPrerequisiteGate.other_agents_intro')}
            </p>
          )}
          {loading && others.length === 0 ? (
            <p className="text-[13px] text-muted" aria-live="polite">
              {i18nT('components.kiroPrerequisiteGate.other_agents_checking')}
            </p>
          ) : failed && others.length === 0 ? (
            <>
              {/* No hand-off: this prerequisite gate also blocks /chat. With
                  no confirmed ready harness, navigating there cannot reach an
                  agent to diagnose the probe failure. */}
              <ErrorNotice
                message={i18nT('components.kiroPrerequisiteGate.other_agents_unavailable')}
                testId="other-agents-probe-error"
                footer={<Btn type="button" onClick={onRetryBackends}>{i18nT('components.kiroPrerequisiteGate.try_again')}</Btn>}
              />
            </>
          ) : others.length === 0 ? (
            <p className="text-[13px] text-muted">
              {i18nT('components.kiroPrerequisiteGate.other_agents_none')}
            </p>
          ) : (
            <>
              <fieldset className="mx-0 space-y-2 border-none p-0">
                <legend className="sr-only">
                  {i18nT('components.kiroPrerequisiteGate.other_agents_list_label')}
                </legend>
                {others.map(b => {
                  const selected = b.id === shownId
                  // The detail opens directly under the row it describes, inside
                  // the same outline, so it reads as that agent's panel rather
                  // than a card detached at the foot of the list.
                  const detailId = `other-agent-detail-${b.id}`
                  return (
                    <AgentPickerRow
                      key={b.id}
                      id={b.id}
                      group="other-coding-agent"
                      label={acpBackendName(b)}
                      selected={selected}
                      onSelect={() => setPicked(b.id)}
                      icon={<Sparkles className="lucide-inline shrink-0 text-muted" aria-hidden="true" />}
                      status={
                        <AgentStatusBadge
                          probe={b}
                          blocked={!configuredBackendCanStart(status, b)}
                          sandboxOff={sandboxOffFor(status, b)}
                        />
                      }
                      detailId={detailId}
                      detailTestId="other-agent-detail"
                    >
                      {selected && shown && (
                        <>
                          {shown.installed === 'missing' ? (
                            <AgentInstallDetail name={name} probe={shown} />
                          ) : shown.restart_required ? (
                            <p className="text-[13px] leading-relaxed text-text">
                              {i18nT('components.kiroPrerequisiteGate.agent_restart_required_detail', { name })}
                            </p>
                          ) : shown.installed === 'unknown' ? (
                            <p className="text-[13px] leading-relaxed text-text">
                              {i18nT('components.kiroPrerequisiteGate.agent_unverified_detail', { name })}
                            </p>
                          ) : sandboxOffFor(status, shown) ? (
                            // Before the host-verdict branch below: same blocked
                            // row, different cause. No doctor command and (below)
                            // no Check again — neither can turn a setting back on.
                            <p className="text-[13px] leading-relaxed text-text" role="status">
                              {i18nT('components.kiroPrerequisiteGate.sandbox_off_agent_detail', { name })}
                            </p>
                          ) : !configuredBackendCanStart(status, shown) ? (
                            <div className="space-y-2 text-[13px] leading-relaxed text-text" role="status">
                              <p>
                                {i18nT('components.kiroPrerequisiteGate.sandbox_blocked_agent_detail', { name })}
                              </p>
                              <p className="text-muted">
                                {i18nT('components.kiroPrerequisiteGate.run_kirocrew_doctor_on_the_gateway_host_for_a_ful')}
                              </p>
                              <CopyCommand><code>kirocrew doctor</code></CopyCommand>
                            </div>
                          ) : (
                            <p className="text-[13px] leading-relaxed text-text">
                              {i18nT('components.kiroPrerequisiteGate.agent_ready_to_use', { name })}
                            </p>
                          )}
                          {/* Server-owned sentence, rendered verbatim like Settings → Agent
                              does: it names this harness's own sign-in, which Kiro Crew
                              neither performs nor can check. */}
                          {shown.auth?.signs_in_separately && shown.auth.sign_in_remedy ? (
                            <p className="text-[12px] leading-relaxed text-muted">{shown.auth.sign_in_remedy}</p>
                          ) : null}
                          {/* Nothing to re-check on an agent that is installed, ready
                              and allowed to start: the only action left is to use it.
                              Nothing to re-check either when the sandbox is off by
                              setting: the host measured fine, so a re-measure cannot
                              change the verdict. */}
                          <AgentDetailActions
                            name={name}
                            onUse={() => switchMut.mutate(shown.id)}
                            useDisabled={!configuredBackendCanStart(status, shown)}
                            busy={busy}
                            switching={switchMut.isPending && switchMut.variables === shown.id}
                            onRecheck={
                              configuredBackendCanStart(status, shown) || sandboxOffFor(status, shown)
                                ? undefined
                                : () => recheckMut.mutate(shown.id)
                            }
                            rechecking={recheckMut.isPending}
                          />
                          {/* No hand-off: the agent choice failed to save, and this
                              setup gate hides the chat the hand-off would open. The
                              Use button above is the retry, so the notice sits in
                              this row beside it; at the foot of the list it would
                              read as a verdict on whichever row is open. */}
                          <ErrorNotice
                            className="text-xs"
                            message={switchErrorFor(shown.id)}
                            testId="other-agent-switch-error"
                          />
                          {/* No hand-off: preserve the unsaved agent choice; this
                              setup gate also hides the chat the hand-off would open. */}
                          <ErrorNotice
                            className="text-xs"
                            message={
                              recheckMut.isError && recheckMut.variables === shown.id
                                ? setupMarkerErrorMessage(recheckMut.error)
                                  ?? i18nT('components.kiroPrerequisiteGate.agent_recheck_failed', { name })
                                : null
                            }
                            testId="other-agent-recheck-error"
                          />
                        </>
                      )}
                    </AgentPickerRow>
                  )
                })}
              </fieldset>
              {!configuredNotReady && (
                <p className="text-[12px] leading-relaxed text-muted">
                  {i18nT('components.kiroPrerequisiteGate.switch_agent_later_in_settings')}
                </p>
              )}
            </>
          )}
        </div>
      )}
    </div>
  )
}

/**
 * The automatic recheck POST (`persistMarkerMut` below) writes the durable
 * `initial_setup_complete` marker when the configured non-Kiro agent flips to
 * ready. This browser is ALREADY admitted to the dashboard when it fires — the
 * gate is rendering `children` — so a failed write (503
 * `setup_marker_write_failed`) is otherwise completely silent to the operator,
 * yet it means every OTHER browser and every non-owner stays stuck on first-run
 * setup until the marker is written. This surfaces it as a notice that floats
 * OVER the running app rather than a full-screen gate — the dashboard is usable
 * and must stay so — with a Retry that re-fires the SAME recheck for the
 * configured backend. The effect never auto-retries; only this button does, so
 * the one-POST-per-flip behaviour is preserved.
 */
function SetupMarkerWriteNotice({
  failed,
  retrying,
  message,
  onRetry,
}: {
  failed: boolean
  retrying: boolean
  message: string | null
  onRetry: () => void
}) {
  const [dismissed, setDismissed] = useState(false)
  // A dismissal answers the failure the user saw, not every future one, so a
  // genuinely NEW failure re-opens the notice — `failed` only goes false→true
  // on a fresh error (a retry that failed again). A re-render with the same
  // error does not, so a dismissed notice stays dismissed.
  useEffect(() => {
    if (failed) setDismissed(false)
  }, [failed])
  if (!failed || dismissed) return null
  return (
    // The wrapper spans the viewport but is click-through (`pointer-events-none`)
    // so the dashboard beneath stays fully operable; only the notice itself
    // (`pointer-events-auto`) takes clicks. Pinned bottom-centre, above the app.
    <div
      className="pointer-events-none fixed left-safe right-safe bottom-safe-offset-4 z-[70] flex justify-center px-4"
      data-testid="kiro-gate-marker-write-error-region"
    >
      {/* No hand-off: this notice floats over the whole running dashboard, so
          the hand-off's navigation to /chat would unmount an unsaved chat
          composition or a half-filled Settings form the user has open beneath
          it. Retry re-fires the marker write in place, losing nothing. */}
      <ErrorNotice
        className="pointer-events-auto w-full max-w-lg shadow-lg"
        title={i18nT('components.kiroPrerequisiteGate.setup_completion_not_recorded')}
        message={message ?? i18nT('components.kiroPrerequisiteGate.setup_marker_retry_body')}
        messagePlacement="below"
        testId="kiro-gate-marker-write-error"
        onDismiss={() => setDismissed(true)}
        footer={
          <Btn type="button" disabled={retrying} onClick={onRetry}>
            <RefreshCw className={`lucide-inline ${retrying ? 'animate-spin' : ''}`} />
            {i18nT('components.kiroPrerequisiteGate.try_again')}
          </Btn>
        }
      />
    </div>
  )
}

export default function KiroPrerequisiteGate({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient()
  // The gateway probes kiro-cli at boot and on explicit request only, so the
  // background poll below reads latched state for free. A user-driven Refresh
  // must still hit the host, so it arms this flag for exactly one fetch.
  const forceProbe = useRef(false)
  const statusQuery = useQuery({
    queryKey: QUERY_KEY,
    queryFn: () => {
      // Probe the host when the user asked (Check again) OR while the blocking
      // first-run gate is up — see kiroPrerequisiteIsBlocking for why a
      // latch-reading poll cannot lift that gate on its own. The two are sent as
      // DIFFERENT modes: the user's click must always probe, while the automatic
      // poll is coalesced server-side so several open tabs do not multiply the
      // gateway's kiro-cli spawns.
      const explicit = forceProbe.current
      forceProbe.current = false
      const refresh = explicit
        ? 'explicit' as const
        : kiroPrerequisiteIsBlocking(queryClient.getQueryData(QUERY_KEY))
          ? 'auto' as const
          : false
      return api.kiroPrerequisite(refresh)
    },
    refetchInterval: (query) => kiroPrerequisiteRefetchInterval(query.state.data),
  })
  const updateStatus = (status: KiroPrerequisiteStatus) => {
    queryClient.setQueryData(QUERY_KEY, status)
  }
  // The repair is a POST, not a flag on the status GET: the gateway's CSRF check
  // and its SEL audit are both method-scoped, so a spec rewrite driven from a GET
  // would be cross-site triggerable and would leave no audit record. Its response
  // IS the post-repair snapshot, so the result seeds the cache directly.
  const repairMutation = useMutation({
    mutationFn: api.repairKiroPrerequisiteSpecs,
    onSuccess: updateStatus,
  })
  // Same POST rationale as the repair above. This one runs `kiro-cli update` on
  // the host to remedy a CLI too old for the `acp` subcommand; its response is
  // the post-update snapshot, so it seeds the cache directly.
  const updateCliMutation = useMutation({
    mutationFn: api.updateKiroPrerequisiteCli,
    onSuccess: updateStatus,
  })

  // Which agent the gateway is configured to drive. Read only once the Kiro
  // check says "not ready": a ready Kiro install never needs it, and a returning
  // user's load stays exactly as cheap as before. A failed read falls back to
  // the Kiro checks — the behaviour this gate always had — never to a bypass.
  const kiroNotReady = !!statusQuery.data && !statusQuery.data.ready
  const configQuery = useQuery<{ agent?: { acp_backend?: string } }>({
    queryKey: CONFIG_QUERY_KEY,
    queryFn: () => api.kirocrewConfig(),
    enabled: kiroNotReady,
    retry: false,
  })
  const configuredBackend = configQuery.data
    ? (configQuery.data.agent?.acp_backend ?? KIRO_BACKEND)
    : undefined
  const otherBackendConfigured = configuredBackend !== undefined && configuredBackend !== KIRO_BACKEND
  const kiroBlocking = kiroPrerequisiteIsBlocking(statusQuery.data)
  // The harness probe Settings → Agent reads. Needed for two things: to let a
  // configured non-Kiro agent through, and to fill the picker on the first-run
  // screen. `retry: false` because its expected failures (403 non-owner, 404 an
  // older gateway) are permanent answers; the gate then keeps the Kiro checks.
  const backendsQuery = useQuery<{ backends: AcpBackendProbe[] }>({
    queryKey: BACKENDS_QUERY_KEY,
    queryFn: () => api.acpBackends(),
    enabled: kiroNotReady && (otherBackendConfigured || kiroBlocking),
    retry: false,
    staleTime: 0,
    refetchInterval: (query) => acpBackendsRefetchInterval(
      statusQuery.data,
      otherBackendConfigured,
      query.state.data?.backends.find(b => b.id === configuredBackend),
    ),
  })
  const configuredProbe = otherBackendConfigured
    ? backendsQuery.data?.backends.find(b => b.id === configuredBackend)
    : undefined
  // The Kiro CLI checks below describe Kiro CLI. When the operator chose
  // another agent and it is usable, none of them is about the agent that will
  // actually run, so the dashboard opens.
  const otherBackendReady = otherBackendConfigured
    && configuredBackendCanStart(statusQuery.data, configuredProbe)

  // The verdict above is read from a read-only poll, but the durable
  // `initial_setup_complete` marker is written only by the config PATCH or the
  // recheck POST (`record_independent_backend_setup`). A PATCH made before the
  // harness was installed writes nothing and is never retried, so a gateway
  // whose config already names an independent harness would be admitted here
  // the moment the install probe flips while the marker stayed false — and
  // every non-owner dashboard user would sit on first-run setup forever. So
  // when the poll admits and the status still says the marker is unwritten,
  // fire the SAME recheck POST Check again makes for that backend, once.
  // Owner-only by construction: the probe this verdict reads comes from
  // `GET /api/acp-backends`, which answers a non-owner with 403, so
  // `otherBackendReady` cannot be true for a viewer the POST would refuse;
  // `setup_allowed === false` is the status's own word for that viewer.
  const persistMarkerMut = useMutation({
    mutationFn: (backend: string) => api.acpBackendRecheck(backend),
    onSuccess: ({ backend }) => applyRecheckedBackend(queryClient, backend),
  })
  const markerUnwritten = otherBackendReady
    && !!statusQuery.data
    && !statusQuery.data.initial_setup_complete
    && statusQuery.data.setup_allowed !== false
  // One POST per (backend, flip), by the effect's own dependencies: it runs
  // when the verdict flips to "can start" with the marker unwritten, or when
  // the configured backend changes while it is. A re-render or a re-poll with
  // the same verdict does not repeat it; a failed POST changes no dependency,
  // so it does not spin — this browser is admitted regardless, exactly as
  // before, and the marker is written by the next explicit Check again in
  // Settings → Agent Harness. The verdict dropping back to "cannot start" and
  // returning is a new flip, and is meant to fire again.
  const persistMarker = persistMarkerMut.mutate
  useEffect(() => {
    if (markerUnwritten && configuredBackend) persistMarker(configuredBackend)
  }, [markerUnwritten, configuredBackend, persistMarker])

  // Remember that this gateway has completed first-run setup, so a later COLD
  // load can classify the user before (or without) a successful status
  // response. `ready` implies setup is done, and covers gateways that report
  // readiness without the first-run bit.
  const setupComplete = (!!statusQuery.data
    && (statusQuery.data.initial_setup_complete || statusQuery.data.ready))
    || otherBackendReady
  useEffect(() => {
    if (setupComplete) safeSetItem(SETUP_COMPLETE_KEY, '1')
  }, [setupComplete])

  // An unresolved check is UNKNOWN — never "setup required", and never a reason
  // to withhold the dashboard OR to pause sessions. Readiness is latched at
  // gateway boot and refreshed only on explicit request, so it is never fresh
  // enough to disable the composer on: a user who signed in from a terminal
  // would sit behind a dead input box. The turn itself is the authority — a
  // signed-out CLI surfaces as an actionable `kiro-cli login` error card in the
  // transcript, which is the ONLY sign-out signal the dashboard shows.
  //
  // This also removes the first-run flash at its root: rendering the
  // setup-branded shell here would show first-run setup on every launch for as
  // long as the gateway's two kiro-cli subprocesses take to answer.
  if (statusQuery.isPending) {
    return <>{children}</>
  }
  const retrying = statusQuery.isFetching
  const retryStatus = () => {
    forceProbe.current = true
    void statusQuery.refetch()
  }
  const prerequisite = statusQuery.data

  // An older gateway has no prerequisite API and must retain its existing
  // dashboard behavior.
  if (
    statusQuery.isError
    && !prerequisite
    && statusQuery.error instanceof ApiError
    && statusQuery.error.status === 404
  ) {
    return <>{children}</>
  }
  // No usable status: either a live gateway error or an unusable body. Both are
  // "we cannot tell". A RETURNING user keeps their dashboard, fully usable —
  // an unreachable status check is not evidence the CLI is broken, and the turn
  // will report the truth either way. Only a user we have never seen complete
  // setup gets the retry screen, since they may genuinely have no CLI yet.
  if (!prerequisite) {
    if (rememberedSetupComplete()) {
      return <>{children}</>
    }
    const message = statusQuery.isError
      ? (statusQuery.error?.message || i18nT('components.kiroPrerequisiteGate.the_gateway_returned_an_unexpected_error'))
      : i18nT('components.kiroPrerequisiteGate.the_gateway_returned_no_prerequisite_status')
    return (
      <SetupStatusError message={message} retrying={retrying} onRetry={retryStatus} />
    )
  }
  if (prerequisite.ready) {
    return <>{children}</>
  }
  // Which agent will run is still unknown: same rule as the pending status
  // above — an unresolved check never paints setup chrome.
  if (configQuery.isPending || (otherBackendConfigured && backendsQuery.isPending)) {
    return <>{children}</>
  }
  if (otherBackendReady) {
    // Admitted to the dashboard. If the automatic marker write failed, this is
    // the only surface that can tell the operator, so it rides ALONGSIDE the
    // app (children) instead of replacing it, and its Retry re-fires the same
    // recheck for the configured backend.
    return (
      <>
        {children}
        <SetupMarkerWriteNotice
          failed={persistMarkerMut.isError}
          retrying={persistMarkerMut.isPending}
          message={setupMarkerErrorMessage(persistMarkerMut.error)}
          onRetry={() => { if (configuredBackend) persistMarker(configuredBackend) }}
        />
      </>
    )
  }
  const status = prerequisite
  // '' when the gateway did not name a platform this screen recognises. Every
  // site below that would print the platform switches to a platform-free line
  // instead of a filler word: "local gateway host" claimed a host the Kiro card
  // then admitted it could not identify.
  const platform = knownPlatform(status.platform)
  // Defensive `?? []`: a gateway older than this field, and every test fixture
  // that builds a partial status object, has no key here.
  const missingSpecs = status.missing_agent_specs ?? []
  const repairError = repairMutation.data?.agent_spec_repair_error
    || (repairMutation.error ? asSentence(repairMutation.error.message) : '')
    || (status.agent_spec_repair_error ?? '')
  // Kiro Crew's own agent specs are absent, so kiro-cli answers every
  // session/set_mode with "Mode '<name>' not found" and not one message can
  // succeed. Placed BEFORE the `initial_setup_complete` bail-out -- the only
  // branch here that hijacks an established install -- and gated ON that same
  // flag, so a GENUINE first run still reaches Install / Sign in instead of a
  // screen offering to repair specs the installer has not written yet.
  //
  // That rule protects against a STALE LATCH: readiness is latched, and blocking
  // an established user on stale state is the failure it avoids. This check is
  // not a latch — it is two `stat` calls made while answering the request, so it
  // cannot be stale, and the condition it reports is total rather than
  // intermittent. It is also the only affordance in the product for repairing
  // this state, so an install without it has no route back.
  if (missingSpecs.length > 0 && status.initial_setup_complete) {
    return (
      <AgentSpecsMissing
        specs={missingSpecs}
        repairError={repairError}
        retrying={retrying || repairMutation.isPending}
        onRepair={() => repairMutation.mutate()}
      />
    )
  }
  // Present but refused. Kiro CLI drops a spec it rejects from its agent table,
  // so `--agent kirocrew` resolves to the default agent with none of Kiro Crew's
  // MCP servers -- the same total failure as an absent spec, and the one the
  // stat-only check above cannot see. Ordered AFTER missing for the same reason
  // that check is scoped to present files: one fault should raise one card, and
  // a spec that is absent is not also rejected.
  const rejectedSpecs = status.rejected_agent_specs ?? []
  if (rejectedSpecs.length > 0 && status.initial_setup_complete) {
    return (
      <AgentSpecsRejected
        specs={rejectedSpecs}
        reason={status.agent_spec_rejection_detail ?? ''}
        repairError={repairError}
        retrying={retrying || repairMutation.isPending}
        onRepair={() => repairMutation.mutate()}
      />
    )
  }
  // Present, but too OLD to expose the `acp` subcommand every session launches
  // through — so even once signed in it cannot start a single turn (it would
  // fail at session-create). The probe runs whether or not the CLI is signed
  // in, so this branch and its card make no claim about sign-in.
  // `acp_supported === false` is a FRESH
  // probe result, not a latch (a `false` default would hide the state on an older
  // gateway that omits the field, so the strict `=== false` is deliberate), so it
  // is safe to surface even on an established install — and its remedy is unique:
  // update the CLI in place, which Kiro Crew runs for the user here. Ordered
  // BEFORE the established-install bail-out for the same reason as the spec
  // branches: this is a total failure the chat error card cannot pre-empt, and
  // this screen is the only place that offers the update.
  const updateError = updateCliMutation.data?.cli_update_error
    || (updateCliMutation.error ? asSentence(updateCliMutation.error.message) : '')
    || (status.cli_update_error ?? '')
  if (status.acp_supported === false) {
    return (
      <CliOutdated
        updateCommand={status.update_command || 'kiro-cli update'}
        bundledCli={status.bundled_cli === true}
        updateError={updateError}
        updating={updateCliMutation.isPending}
        retrying={retrying}
        onUpdate={() => updateCliMutation.mutate()}
        onRetry={retryStatus}
      />
    )
  }
  // Established install, signed out: render NOTHING and pause nothing. The user
  // is not guided to sign in — the chat error card carries that, in context,
  // only when they actually try to use the agent. A persistent banner nagged
  // every surface (including ones that never start a session) for a state the
  // dashboard cannot even keep current.
  if (status.initial_setup_complete) {
    return <>{children}</>
  }
  if (prerequisite.setup_allowed === false) {
    return <OwnerSetupRequired retrying={retrying} onRetry={retryStatus} />
  }
  // A first-run install whose probe genuinely could not verify the CLI (not the
  // sandbox/timeout/acp branches above, which have their own screens): the
  // backend's last-resort backstop degrades an exception to a 200 not-ready
  // body rather than a 500, so `prerequisite` IS resolved and the earlier
  // `!prerequisite` branch never sees it. Without this the "Setup Check
  // Unavailable" screen had no diagnostic at all (the desktop symptom this
  // covers) — `probe_error`/`probe_status` name the failing probe verbatim.
  if (status.probe_error) {
    const withStatus = typeof status.probe_status === 'number'
      ? `${status.probe_error} (exit ${status.probe_status})`
      : status.probe_error
    return (
      <SetupStatusError message={withStatus} retrying={retrying} onRetry={retryStatus} />
    )
  }
  // The CLI is present and executable, but verification runs it INSIDE the
  // sandbox, so a host that cannot build one fails verification. Telling that
  // user to go get Kiro CLI is false on a host whose CLI is installed and signed
  // in, and Kiro's setup page cannot help them. Placed after
  // `initial_setup_complete` deliberately: an established install is not
  // hijacked by a full-screen gate (the chat error card carries it in context,
  // and since the probe names the failing step that message is specific) —
  // this branch only replaces the first-run screen that would otherwise lie.
  if (status.sandbox_unavailable) {
    return (
      <SandboxUnavailable
        failureKind={status.sandbox_failure_kind}
        detail={status.sandbox_detail}
        remedy={status.sandbox_remedy}
        retrying={retrying}
        onRetry={retryStatus}
      />
    )
  }

  return (
    <SetupShell>
        <>
          <div className="mb-7">
            <div className="mb-3 flex items-center gap-2 text-[12px] font-semibold tracking-[0.14em] text-accent">
              <span className="uppercase">{i18nT('components.kiroPrerequisiteGate.setup')}</span>
              <ArrowRight className="lucide-inline" />
              {platform ? (
                <span>{platform} {i18nT('components.kiroPrerequisiteGate.gateway')}</span>
              ) : (
                <span>{i18nT('components.kiroPrerequisiteGate.gateway_host')}</span>
              )}
            </div>
            <h1 className="text-3xl font-bold tracking-tight text-text-strong">{i18nT('components.kiroPrerequisiteGate.set_up_kiro')}</h1>
            <p className="mt-2 max-w-xl text-sm leading-relaxed text-muted">
              {/* One sentence in one key, so each language orders the host
                  phrase where its grammar puts it. Two keys rather than an
                  empty {{platform}}: "the  gateway host" leaves a double space
                  in English and a dangling particle elsewhere. */}
              {platform ? (
                <Trans
                  i18nKey="components.kiroPrerequisiteGate.setup_intro"
                  values={{ platform }}
                  components={[<strong key="host" className="font-semibold text-text" />]}
                />
              ) : (
                <Trans
                  i18nKey="components.kiroPrerequisiteGate.setup_intro_unknown_platform"
                  components={[<strong key="host" className="font-semibold text-text" />]}
                />
              )}
            </p>
          </div>

          {configQuery.isError && (
            <>
              {/* No hand-off: OtherCodingAgents below holds the unsaved radio
                  choice in picked until Use is pressed; navigating would lose it.
                  Retry here without discarding that agent selection. */}
              <ErrorNotice
                className="mb-5"
                message={i18nT('components.kiroPrerequisiteGate.could_not_check_coding_agent')}
                testId="kiro-gate-config-error"
                footer={
                  <Btn type="button" disabled={configQuery.isFetching} onClick={() => void configQuery.refetch()}>
                    {i18nT('components.kiroPrerequisiteGate.try_again')}
                  </Btn>
                }
              />
            </>
          )}

          <Card className="border-accent/60 shadow-[0_10px_35px_var(--accent-glow)]">
            <div className="flex items-start justify-between gap-4">
              <div>
                <h2 className="flex items-center gap-2 text-base font-semibold text-text-strong">
                  <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-accent-subtle text-accent">
                    <Package className="lucide-inline" />
                  </span>
                  {i18nT('components.kiroPrerequisiteGate.get_kiro_cli')}
                </h2>
                {/* The card's first line is the state, in both states: "isn't
                    installed yet" or "is installed, finish signing in". The
                    footer used to carry the installed line, below the other
                    agents section, where it read as an afterthought. */}
                <p className="mt-2 text-sm leading-relaxed text-muted">
                  {status.installed
                    ? i18nT('components.kiroPrerequisiteGate.kiro_cli_is_installed_finish_signing_in_to_conti')
                    : i18nT('components.kiroPrerequisiteGate.kiro_cli_is_not_installed_yet')}
                </p>
              </div>
              <StepStatus complete={false} current />
            </div>
            {/* The one-line installer is SHOWN, never run: Kiro Crew does not
                install Kiro CLI. The setup page stays as the route for every
                other install form (RPM, AppImage, musl) and stays correct as
                those change. */}
            {!status.installed && (
              <div className="mt-4">
                <KiroInstallCommands platform={platform} />
                <a
                  className="mt-3 inline-flex items-center gap-1.5 text-[13px] font-medium text-accent hover:underline focus-ring"
                  href={status.docs_url}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  {i18nT('components.kiroPrerequisiteGate.open_kiro_cli_setup')}
                  <ExternalLink className="lucide-inline" />
                </a>
                <p className="mt-3 text-[13px] leading-relaxed text-muted" aria-live="polite">
                  {i18nT('components.kiroPrerequisiteGate.this_page_detects_kiro_cli_automatically')}
                </p>
              </div>
            )}
            {/* Keep main's bundled-CLI command rendering when sign-in moves into
                this card, including its shared path and copy controls. */}
            {status.installed && !status.authenticated && <SignInCommands status={status} />}
            {/* The card owns its Check again, the way each other agent's panel
                owns its own: the action sits with the thing it re-checks, not
                in a page footer that read as Kiro-only once other agents were
                on screen. Its label names Kiro CLI because a second, identical
                "Check again" can be open in an agent's panel below, and two
                same-labelled buttons that check different things invite the
                wrong press. The agent panel's is scoped by the row it sits in. */}
            <div className="mt-4 flex items-center gap-2">
              <SendBtn
                type="button"
                className="inline-flex items-center gap-1.5"
                disabled={statusQuery.isFetching}
                onClick={retryStatus}
              >
                <RefreshCw className={`lucide-inline ${statusQuery.isFetching ? 'animate-spin' : ''}`} />
                {status.installed
                  ? i18nT('components.kiroPrerequisiteGate.check_sign_in_again')
                  : i18nT('components.kiroPrerequisiteGate.check_again_for_kiro_cli')}
              </SendBtn>
            </div>
          </Card>

          <OtherCodingAgents
            status={status}
            configured={configuredBackend ?? KIRO_BACKEND}
            backends={backendsQuery.data?.backends ?? []}
            loading={backendsQuery.isPending && backendsQuery.fetchStatus !== 'idle'}
            failed={backendsQuery.isError}
            onRetryBackends={() => { void backendsQuery.refetch() }}
          />
        </>
    </SetupShell>
  )
}
