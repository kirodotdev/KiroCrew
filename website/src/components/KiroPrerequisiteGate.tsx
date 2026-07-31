import { useEffect, useRef, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle,
  ArrowRight,
  CheckCircle2,
  ExternalLink,
  Loader2,
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
import { Badge, Btn, Card, SendBtn } from './ui'

import { i18nT } from '../i18n/t'
const QUERY_KEY = ['kiro-prerequisite'] as const

export function kiroPrerequisiteRefetchInterval(
  status: KiroPrerequisiteStatus | undefined,
): number | false {
  if (status?.operation.status === 'running') return 1_000
  if (status?.ready) return 30_000
  if (status && status.setup_allowed === false) return 3_000
  return 30_000
}

function trustedLoginUrl(value: string): string | null {
  if (!value || value.includes('\\')) return null
  for (const character of value) {
    const code = character.charCodeAt(0)
    if (code < 32 || code === 127) return null
  }
  try {
    const parsed = new URL(value)
    const host = parsed.hostname.toLowerCase()
    const trustedPath = host === 'app.kiro.dev'
      || (host === 'view.awsapps.com'
        && (parsed.pathname === '/start' || parsed.pathname.startsWith('/start/')))
    if (
      parsed.protocol !== 'https:'
      || (parsed.port !== '' && parsed.port !== '443')
      || parsed.username !== ''
      || parsed.password !== ''
      || !trustedPath
    ) {
      return null
    }
    return parsed.href
  } catch {
    return null
  }
}

// Gateway error strings arrive unpunctuated ("Token required"), and the gate
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
  cardLabel,
}: {
  children: ReactNode
  cardLabel?: string
}) {
  const label = cardLabel || i18nT('components.kiroPrerequisiteGate.your_crew_is_almost_ready')
  return (
    <main className={SCRIM_CLASS} aria-label={label}>
      <div className={PANEL_CLASS}>
        <ShellAside
          copy={{
            ariaLabel: label,
            panelHeadline: i18nT('components.kiroPrerequisiteGate.your_crew_is_almost_ready'),
            panelBody: i18nT(
              'components.kiroPrerequisiteGate.install_kiro_cli_sign_in_once_and_kiro_crew_will',
            ),
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

function OperationProgress({ status }: { status: KiroPrerequisiteStatus }) {
  const operation = status.operation
  if (operation.status === 'idle' && !operation.message) return null
  const isRunning = operation.status === 'running'
  const isFailure = operation.status === 'failed'
  const loginUrl = trustedLoginUrl(operation.url)

  return (
    <div
      className={`mt-4 rounded-lg border p-3 ${
        isFailure
          ? 'border-danger/20 bg-danger/10'
          : 'border-border bg-bg-elevated'
      }`}
      aria-live="polite"
    >
      <div className={`flex items-center gap-2 text-sm ${isFailure ? 'text-danger' : 'text-text'}`}>
        {isRunning && <Loader2 className="lucide-inline animate-spin" />}
        {isFailure && <AlertTriangle className="lucide-inline" />}
        <span>{operation.error || operation.message}</span>
      </div>
      {loginUrl && (
        <a
          className="mt-3 inline-flex items-center gap-1.5 text-[13px] font-medium text-accent hover:underline focus-ring"
          href={loginUrl}
          target="_blank"
          rel="noopener noreferrer"
        >
          {i18nT('components.kiroPrerequisiteGate.open_kiro_sign_in_page')} <ExternalLink className="lucide-inline" />
        </a>
      )}
      {operation.detail && (
        <pre className="mt-3 max-h-36 overflow-auto whitespace-pre-wrap break-words rounded-md bg-bg p-3 font-mono text-[12px] leading-relaxed text-muted">
          {operation.detail}
        </pre>
      )}
    </div>
  )
}

function OwnerSetupRequired({
  retrying,
  onRetry,
}: {
  retrying: boolean
  onRetry: () => void
}) {
  return (
    <SetupShell>
      <>
        <div className="flex h-11 w-11 items-center justify-center rounded-xl bg-accent-subtle text-accent">
          <ShieldCheck className="lucide-inline" />
        </div>
        <p className="mt-6 text-[12px] font-bold uppercase tracking-[0.16em] text-accent">
          {i18nT('components.kiroPrerequisiteGate.gateway_setup_required')}
        </p>
        <h1 className="mt-2 text-3xl font-bold tracking-tight text-text-strong">
          {i18nT('components.kiroPrerequisiteGate.the_gateway_owner_needs_to_finish_setup')}
        </h1>
        <p className="mt-3 max-w-lg text-sm leading-relaxed text-muted">
          {i18nT('components.kiroPrerequisiteGate.ask_the_kiro_crew_owner_to_install_kiro_cli_and')}
        </p>
        <div className="mt-6">
          <Btn type="button" disabled={retrying} onClick={onRetry}>
            <RefreshCw className={`lucide-inline ${retrying ? 'animate-spin' : ''}`} />
            {i18nT('components.kiroPrerequisiteGate.check_again')}
          </Btn>
        </div>
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

export default function KiroPrerequisiteGate({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient()
  // The gateway probes kiro-cli at boot and on explicit request only, so the
  // background poll below reads latched state for free. A user-driven Refresh
  // must still hit the host, so it arms this flag for exactly one fetch.
  const forceProbe = useRef(false)
  const statusQuery = useQuery({
    queryKey: QUERY_KEY,
    queryFn: () => {
      const refresh = forceProbe.current
      forceProbe.current = false
      return api.kiroPrerequisite(refresh)
    },
    refetchInterval: (query) => kiroPrerequisiteRefetchInterval(query.state.data),
  })
  const updateStatus = (status: KiroPrerequisiteStatus) => {
    queryClient.setQueryData(QUERY_KEY, status)
  }
  const installMutation = useMutation({
    mutationFn: api.installKiroPrerequisite,
    onSuccess: updateStatus,
  })
  const loginMutation = useMutation({
    mutationFn: api.loginKiroPrerequisite,
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
  // This also removes the first-run flash at its root: this window used to
  // render the setup-branded shell, so every launch showed first-run setup for
  // as long as the gateway's two kiro-cli subprocesses took to answer.
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
  const busy = status.operation.status === 'running'
    || installMutation.isPending
    || loginMutation.isPending
  const mutationError = installMutation.error || loginMutation.error
  const platform = status.platform || 'local'
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
                  {i18nT('components.kiroPrerequisiteGate.install_kiro_cli')}
                </h2>
                <p className="mt-2 text-sm leading-relaxed text-muted">
                  {i18nT('components.kiroPrerequisiteGate.kiro_crew_downloads_the_official_kiro_installer')}
                </p>
              </div>
              <StepStatus complete={status.installed} current={!status.installed} />
            </div>
            <div className="mt-4 flex flex-wrap items-center gap-3">
              <SendBtn
                type="button"
                disabled={busy || status.installed || !status.can_auto_install}
                onClick={() => installMutation.mutate()}
              >
                {busy && status.operation.kind === 'install'
                  ? <><Loader2 className="lucide-inline animate-spin" /> {i18nT('components.kiroPrerequisiteGate.installing')}</>
                  : status.installed
                    ? <><CheckCircle2 className="lucide-inline" /> {i18nT('components.kiroPrerequisiteGate.installed')}</>
                    : <><Package className="lucide-inline" /> {i18nT('components.kiroPrerequisiteGate.install_kiro_cli')}</>}
              </SendBtn>
              <a
                className="inline-flex items-center gap-1.5 text-[13px] font-medium text-accent hover:underline focus-ring"
                href={status.docs_url}
                target="_blank"
                rel="noopener noreferrer"
              >
                {i18nT('components.kiroPrerequisiteGate.installation_guide')} <ExternalLink className="lucide-inline" />
              </a>
            </div>
            {!status.installed && !status.can_auto_install && (
              <p className="mt-3 text-[13px] leading-relaxed text-muted">
                {i18nT('components.kiroPrerequisiteGate.automatic_installation_is_unavailable_here_insta')}
              </p>
            )}
            {status.operation.kind === 'install' && <OperationProgress status={status} />}
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
                  {i18nT('components.kiroPrerequisiteGate.start_kiro_s_device_sign_in_open_the_secure_page')}
                </p>
              </div>
              <StepStatus
                complete={status.authenticated}
                current={status.installed && !status.authenticated}
              />
            </div>
            <div className="mt-4">
              <SendBtn
                type="button"
                disabled={
                  busy
                  || !status.installed
                  || status.authenticated
                }
                onClick={() => loginMutation.mutate()}
              >
                {busy && status.operation.kind === 'login'
                  ? <><Loader2 className="lucide-inline animate-spin" /> {i18nT('components.kiroPrerequisiteGate.waiting_for_sign_in')}</>
                  : status.authenticated
                    ? <><CheckCircle2 className="lucide-inline" /> {i18nT('components.kiroPrerequisiteGate.signed_in')}</>
                    : <><LogIn className="lucide-inline" /> {i18nT('components.kiroPrerequisiteGate.sign_in_to_kiro')}</>}
              </SendBtn>
            </div>
            {status.operation.kind === 'login' && <OperationProgress status={status} />}
          </Card>

          {mutationError && (
            <div
              className="mb-4 flex items-start gap-2 rounded-lg border border-danger/20 bg-danger/10 p-3 text-sm text-danger"
              role="alert"
            >
              <AlertTriangle className="lucide-inline" />
              {mutationError.message || i18nT('components.kiroPrerequisiteGate.kiro_setup_could_not_start')}
            </div>
          )}

          <div className="flex items-center justify-between gap-4 border-t border-border pt-5">
            <p className="text-[13px] text-muted" aria-live="polite">
              {status.installed
                ? i18nT('components.kiroPrerequisiteGate.kiro_cli_is_installed_finish_signing_in_to_conti')
                : `Kiro CLI is required on the ${platform} gateway host.`}
            </p>
            <Btn
              type="button"
              disabled={busy || statusQuery.isFetching}
              onClick={retryStatus}
            >
              <RefreshCw className={`lucide-inline ${statusQuery.isFetching ? 'animate-spin' : ''}`} />
              {i18nT('components.kiroPrerequisiteGate.check_again')}
            </Btn>
          </div>
        </>
    </SetupShell>
  )
}
