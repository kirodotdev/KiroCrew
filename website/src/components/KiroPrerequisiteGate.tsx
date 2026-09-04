import { useEffect, useRef, useState, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle,
  ArrowRight,
  Check,
  CheckCircle2,
  Copy,
  Download,
  ExternalLink,
  LogIn,
  Package,
  RefreshCw,
  ShieldCheck,
} from 'lucide-react'
import {
  ApiError,
  api,
  type KiroPrerequisiteStatus,
} from '../api/client'
import {
  PANEL_CLASS,
  SCRIM_CLASS,
  SECTION_CLASS,
  ShellAside,
} from './OnboardingChapterShell'
import { safeGetItem, safeSetItem } from '../utils/safeStorage'
import { copyToClipboard } from '../utils/clipboard'
import { Badge, Btn, Card, SendBtn } from './ui'

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
            scroll on the tall two-step setup. */}
        <section className={SECTION_CLASS}>
          <div className="flex min-h-0 flex-1 flex-col overflow-y-auto">
            <div className="my-auto w-full px-6 py-8 sm:px-10 sm:py-10">{children}</div>
          </div>
          {footer ? (
            <div className="shrink-0 border-t border-border px-6 py-4 sm:px-10">{footer}</div>
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
        <p className="mt-3 max-w-lg text-sm leading-relaxed text-muted">
          {asSentence(message)} {i18nT('components.kiroPrerequisiteGate.retry_the_gateway_check_before_starting_a_sessio')}
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
 * A shell command rendered as a click-to-copy block.
 *
 * The whole block is the target rather than a small trailing glyph: this command
 * has to be retyped on the gateway host, and one typo restarts the loop the user
 * is already stuck in. The glyph stays faintly visible instead of appearing only
 * on hover, because a recovery screen is the wrong place to hide an affordance.
 *
 * The text is read back out of the DOM rather than taken as a prop. A command is
 * not translatable copy, and the i18n gate's exemption covers a literal that is
 * lexically a child of `code`/`pre` — passing it as `command="..."` would make it
 * a JSX attribute string and trip the zero-tolerance [added-lines] check.
 */
function CopyCommand({ children }: { children: ReactNode }) {
  const hostRef = useRef<HTMLSpanElement>(null)
  const [copied, setCopied] = useState(false)
  const resetTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(
    () => () => {
      if (resetTimer.current) clearTimeout(resetTimer.current)
    },
    [],
  )
  const handleCopy = async () => {
    const text = hostRef.current?.textContent?.trim() ?? ''
    if (!text) return
    try {
      await copyToClipboard(text)
    } catch {
      // Both clipboard paths failed (no clipboard API, execCommand denied):
      // leave the glyph alone rather than announcing a copy that did not happen.
      return
    }
    setCopied(true)
    if (resetTimer.current) clearTimeout(resetTimer.current)
    resetTimer.current = setTimeout(() => setCopied(false), 1500)
  }
  const label = copied
    ? i18nT('components.kiroPrerequisiteGate.copied')
    : i18nT('components.kiroPrerequisiteGate.copy_command')
  return (
    <button
      type="button"
      onClick={handleCopy}
      aria-label={label}
      title={label}
      className="group/cmd mt-1 flex w-full cursor-pointer items-center justify-between gap-2 rounded-lg border-none bg-bg-elevated px-2 py-1.5 text-left hover:bg-bg-hover focus-ring"
    >
      <span
        ref={hostRef}
        className="min-w-0 overflow-x-auto text-xs text-text-strong [&_code]:font-mono"
      >
        {children}
      </span>
      {copied ? (
        <Check className="lucide-inline shrink-0 text-ok" />
      ) : (
        <Copy className="lucide-inline shrink-0 text-muted opacity-50 transition-opacity group-hover/cmd:opacity-100" />
      )}
    </button>
  )
}

/**
 * The remedy for one `sandbox_remedy` token.
 *
 * The backend probe knows WHICH unshare step failed and with which errno, and
 * those identify the host mechanism — so the gate can name the actual fix
 * instead of showing `errno 1 (EPERM)` and a retry button. An unrecognised or
 * empty token renders nothing, and the screen falls back to the doctor
 * pointer, which is still strictly more than the bare errno it replaced.
 *
 * Exactly ONE command per mechanism, deliberately. The AppArmor case previously
 * also offered `aa-exec -p kirocrew-userns` for a hand-started gateway, which is
 * worse than no advice: entering a named profile is not permitted for an
 * unconfined user, and `aa-exec` execs the command anyway instead of failing, so
 * the user gets a remedy that looks applied and changes nothing. The profile is
 * attached by systemd (`AppArmorProfile=`), so installing the service is the
 * only path that actually applies it — and the desktop app reuses an existing
 * gateway on the port, so the service covers that install too.
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
  // kernel just denied the second step. That mechanism therefore overrides the
  // body. The other tokens leave it alone — for them the host genuinely offers
  // no usable namespace, and their remedy step carries the specifics.
  const body =
    failureKind === 'transient'
      ? i18nT('components.kiroPrerequisiteGate.the_check_hit_a_temporary_limit_and_was_not_cach')
      : failureKind === 'foreign_sandbox'
        ? i18nT('components.kiroPrerequisiteGate.another_sandbox_already_confines_kiro_crew_so_it')
        : remedy === 'apparmor_userns'
          ? i18nT('components.kiroPrerequisiteGate.this_host_allows_user_namespaces_but_the_kernel_d')
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
  updateError,
  updating,
  retrying,
  onUpdate,
  onRetry,
}: {
  updateCommand: string
  updateError: string
  updating: boolean
  retrying: boolean
  onUpdate: () => void
  onRetry: () => void
}) {
  // The CLI is installed and signed in, but too old to expose the `acp`
  // subcommand Kiro Crew launches every session through — so it would fail at
  // session-create rather than here. The remedy is an UPDATE in place, not a
  // reinstall, and unlike the install/sign-in steps Kiro Crew CAN run this one
  // for the user (it is the CLI's own self-update). We therefore offer a button
  // that runs it AND show the command for anyone who would rather run it on the
  // host themselves.
  return (
    <SetupShell
      asideHeadline={i18nT('components.kiroPrerequisiteGate.kiro_cli_update_needed')}
      asideBody={i18nT('components.kiroPrerequisiteGate.this_kiro_cli_is_too_old_for_the_acp_command')}
      footer={
        <div className="flex items-center justify-between gap-4">
          <SendBtn type="button" disabled={updating || retrying} onClick={onUpdate}>
            <Download className={`lucide-inline ${updating ? 'animate-pulse' : ''}`} />
            {updating
              ? i18nT('components.kiroPrerequisiteGate.updating_kiro_cli')
              : i18nT('components.kiroPrerequisiteGate.update_kiro_cli')}
          </SendBtn>
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
          {i18nT('components.kiroPrerequisiteGate.kiro_cli_is_installed_and_signed_in_but_too_old')}
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
            complete. role="alert" because it appears in place after the button
            press with no route change. */}
        {updateError ? (
          <div className="mt-4 w-full max-w-lg text-left" role="alert">
            <p className="text-[11px] font-semibold uppercase tracking-[0.14em] text-danger">
              {i18nT('components.kiroPrerequisiteGate.the_update_attempt_failed')}
            </p>
            <pre className="mt-1 overflow-x-auto whitespace-pre-wrap break-words rounded-lg bg-danger/10 p-3 text-xs text-danger">
              {updateError}
            </pre>
          </div>
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
        {repairError ? (
          <div className="mt-4 w-full max-w-lg text-left" role="alert">
            <p className="text-[11px] font-semibold uppercase tracking-[0.14em] text-danger">
              {i18nT('components.kiroPrerequisiteGate.the_repair_attempt_failed')}
            </p>
            <pre className="mt-1 overflow-x-auto whitespace-pre-wrap break-words rounded-lg bg-danger/10 p-3 text-xs text-danger">
              {repairError}
            </pre>
          </div>
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
        {repairError ? (
          <div className="mt-4 w-full max-w-lg text-left" role="alert">
            <p className="text-[11px] font-semibold uppercase tracking-[0.14em] text-danger">
              {i18nT('components.kiroPrerequisiteGate.the_repair_attempt_failed')}
            </p>
            <pre className="mt-1 overflow-x-auto whitespace-pre-wrap break-words rounded-lg bg-danger/10 p-3 text-xs text-danger">
              {repairError}
            </pre>
          </div>
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

  // Remember that this gateway has completed first-run setup, so a later COLD
  // load can classify the user before (or without) a successful status
  // response. `ready` implies setup is done, and covers gateways that report
  // readiness without the first-run bit.
  const setupComplete = !!statusQuery.data
    && (statusQuery.data.initial_setup_complete || statusQuery.data.ready)
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
  const status = prerequisite
  const platform = status.platform || 'local'
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
  // Present, signed in, but too OLD to expose the `acp` subcommand every session
  // launches through — so it runs and authenticates yet cannot start a single
  // turn (it would fail at session-create). `acp_supported === false` is a FRESH
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
              <span>{platform} {i18nT('components.kiroPrerequisiteGate.gateway')}</span>
            </div>
            <h1 className="text-3xl font-bold tracking-tight text-text-strong">{i18nT('components.kiroPrerequisiteGate.set_up_kiro')}</h1>
            <p className="mt-2 max-w-xl text-sm leading-relaxed text-muted">
              {i18nT('components.kiroPrerequisiteGate.kiro_crew_uses_kiro_cli_as_its_agent_engine_comp')}{' '}
              <strong className="font-semibold text-text">{platform} {i18nT('components.kiroPrerequisiteGate.gateway_host')}</strong>{i18nT('components.kiroPrerequisiteGate.then_the_dashboard_will_open_automatically')}
            </p>
          </div>

          <Card className={!status.installed ? 'border-accent/60 shadow-[0_10px_35px_var(--accent-glow)]' : ''}>
            <div className="flex items-start justify-between gap-4">
              <div>
                <h2 className="flex items-center gap-2 text-base font-semibold text-text-strong">
                  <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-accent-subtle text-accent">
                    <Package className="lucide-inline" />
                  </span>
                  {i18nT('components.kiroPrerequisiteGate.get_kiro_cli')}
                </h2>
                <p className="mt-2 text-sm leading-relaxed text-muted">
                  {status.installed
                    ? i18nT('components.kiroPrerequisiteGate.kiro_cli_was_found_on_this_host')
                    : i18nT('components.kiroPrerequisiteGate.install_kiro_cli_from_kiros_official_setup_page')}
                </p>
              </div>
              <StepStatus complete={status.installed} current={!status.installed} />
            </div>
            {/* A link, not a button: Kiro Crew does not install Kiro CLI. Kiro's
                own page carries the per-platform steps and stays correct as they
                change, which a digest-pinned in-app installer did not. */}
            {!status.installed && (
              <div className="mt-4">
                <a
                  className="btn-sweep inline-flex items-center gap-1.5 rounded-lg bg-accent px-4 py-2 text-[13px] font-semibold text-accent-fg hover:bg-accent-hover hover:shadow-[0_0_20px_var(--accent-glow)] transition-all focus-ring"
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
          </Card>

          <Card className={status.installed && !status.authenticated ? 'border-accent/60 shadow-[0_10px_35px_var(--accent-glow)]' : ''}>
            <div className="flex items-start justify-between gap-4">
              <div>
                <h2 className="flex items-center gap-2 text-base font-semibold text-text-strong">
                  <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-accent-subtle text-accent">
                    <LogIn className="lucide-inline" />
                  </span>
                  {i18nT('components.kiroPrerequisiteGate.sign_in_to_kiro')}
                </h2>
                <p className="mt-2 text-sm leading-relaxed text-muted">
                  {status.authenticated
                    ? i18nT('components.kiroPrerequisiteGate.this_kiro_cli_is_signed_in')
                    : i18nT('components.kiroPrerequisiteGate.sign_in_with_kiro_cli_on_the_gateway_host')}
                </p>
              </div>
              <StepStatus
                complete={status.authenticated}
                current={status.installed && !status.authenticated}
              />
            </div>
            {/* Rendered VERBATIM from the backend constants, never catalog
                values: a translated command cannot be typed. Shown only once a CLI
                exists to sign into — before that the step above owns the screen.
                Kiro Crew does not run them; the footer's Check again reads the
                result.

                BOTH tiers are offered, because the sign-in page the bare command
                opens presents a free Builder ID as a peer of organization SSO:
                a user on an SSO plan who picks the wrong one authenticates
                successfully and only discovers the mismatch later, as missing
                models. Naming the tier here makes it a decision instead of a
                guess. Kiro Crew does not detect which one applies — that would
                mean inspecting the host's identity configuration — so the copy
                describes the choice and lets the user make it. */}
            {status.installed && !status.authenticated && (
              <div className="mt-4 space-y-4">
                <div>
                  <p className="text-[13px] font-medium text-text">
                    {i18nT('components.kiroPrerequisiteGate.sign_in_personal_label')}
                  </p>
                  <code className="mt-1.5 inline-block rounded-lg border border-border bg-bg px-2.5 py-1.5 font-mono text-[13px] text-text">
                    {status.login_command}
                  </code>
                </div>
                <div>
                  <p className="text-[13px] font-medium text-text">
                    {i18nT('components.kiroPrerequisiteGate.sign_in_sso_label')}
                  </p>
                  <code className="mt-1.5 inline-block rounded-lg border border-border bg-bg px-2.5 py-1.5 font-mono text-[13px] text-text">
                    {status.sso_login_command}
                  </code>
                  <p className="mt-1.5 text-[12px] leading-relaxed text-muted">
                    {i18nT('components.kiroPrerequisiteGate.sign_in_sso_hint')}
                  </p>
                </div>
                <p className="text-[12px] leading-relaxed text-muted">
                  {i18nT('components.kiroPrerequisiteGate.sign_in_method_note')}
                </p>
              </div>
            )}
          </Card>

          <div className="flex items-center justify-between gap-4 border-t border-border pt-5">
            <p className="text-[13px] text-muted" aria-live="polite">
              {status.installed
                ? i18nT('components.kiroPrerequisiteGate.kiro_cli_is_installed_finish_signing_in_to_conti')
                : i18nT('components.kiroPrerequisiteGate.kiro_cli_is_required_on_the_gateway_host', { platform })}
            </p>
            <SendBtn
              type="button"
              className="inline-flex items-center gap-1.5"
              disabled={statusQuery.isFetching}
              onClick={retryStatus}
            >
              <RefreshCw className={`lucide-inline ${statusQuery.isFetching ? 'animate-spin' : ''}`} />
              {i18nT('components.kiroPrerequisiteGate.check_again')}
            </SendBtn>
          </div>
        </>
    </SetupShell>
  )
}
