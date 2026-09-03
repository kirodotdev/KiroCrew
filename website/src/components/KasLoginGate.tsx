import { useEffect, useRef, useState, type ReactNode } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle,
  Building2,
  Check,
  Copy,
  Globe,
  IdCard,
  Link2,
  Loader2,
  RefreshCw,
  ExternalLink,
} from 'lucide-react'
import { api, type KasLoginDeviceSession, type KasLoginLoopbackSession } from '../api/client'
import { ApiError } from '../api/apiError'
import {
  PANEL_CLASS,
  SCRIM_CLASS,
  SECTION_CLASS,
  ShellAside,
  type ShellAsideCopy,
} from './OnboardingChapterShell'
import GithubLogo from './icons/GithubLogo'
import { Btn } from './ui'
import { copyToClipboard } from '../utils/clipboard'

import { i18nT } from '../i18n/t'

const QUERY_KEY = ['kas-login'] as const

// Device-flow poll cadence. The auth service does not return a per-provider
// interval, so we poll at the flow's default (kiro-cli uses 5s).
const DEVICE_POLL_INTERVAL_MS = 5_000
// Loopback poll cadence: the listener is in-process on this machine, so a tight
// poll is free and the app opens within a couple of seconds of the redirect.
const LOOPBACK_POLL_INTERVAL_MS = 2_000
// After this long on the loopback wait screen the "use a code" hint is promoted:
// a redirect that has not landed by now is usually one that cannot land.
const LOOPBACK_SLOW_HINT_MS = 30_000

type ActiveSession =
  | ({ kind: 'device'; provider: KasLoginProvider } & KasLoginDeviceSession)
  | ({ kind: 'loopback'; provider: KasLoginProvider } & KasLoginLoopbackSession)

/** True when this page is itself served from a loopback address — the only case
 *  in which a portal redirect to `http://localhost:<port>` reaches the gateway. */
function browserOnLoopback(): boolean {
  const h = window.location.hostname
  return h === 'localhost' || h === '127.0.0.1' || h === '[::1]' || h === '::1'
}

/** The begin-loopback 409: "take the device flow", not an error to display.
 *  Reads the structured error (status + JSON body `code`); the message text is
 *  only a fallback for a transport that lost the body. */
function isLoopbackUnavailable(err: unknown): boolean {
  if (err instanceof ApiError) {
    if (err.status === 409) return true
    try {
      const parsed = JSON.parse(err.body || '{}') as { code?: unknown }
      if (parsed.code === 'loopback_unavailable') return true
    } catch {
      /* non-JSON body: fall through to the message check */
    }
  }
  return /loopback_unavailable/.test(String((err as { message?: string })?.message ?? err))
}

/** Open the portal in a new tab. Popup blockers may refuse; the wait screen
 *  keeps an explicit "open again" button for exactly that case. */
function openAuthTab(url: string): void {
  try {
    window.open(url, '_blank', 'noopener,noreferrer')
  } catch {
    /* blocked or headless: the wait screen's button is the fallback */
  }
}

/**
 * Wire identifiers for the sign-in providers the gateway accepts. Sent verbatim
 * in the POST body — never catalog values, because a translated identifier is
 * not a protocol token.
 */
export type KasLoginProvider = 'google' | 'github' | 'builder_id' | 'idc'

// Extra begin-device fields the `idc` provider needs (the company's IAM Identity
// Center access-portal URL, plus an optional region); other providers send none.
export type KasLoginExtra = { start_url?: string; region?: string }

// Display name for the provider a sign-in is running under (the device view's
// eyebrow interpolates it). Routed through the catalog like every other label
// so locales that transliterate brand names can.
function providerLabel(provider: KasLoginProvider): string {
  switch (provider) {
    case 'google':
      return i18nT('components.kasLogin.provider_google')
    case 'github':
      return i18nT('components.kasLogin.provider_github')
    case 'builder_id':
      return i18nT('components.kasLogin.provider_builder_id')
    case 'idc':
      return i18nT('components.kasLogin.provider_company_sso')
  }
}

// Same full-screen chrome as KiroPrerequisiteGate's SetupShell: scrim + panel +
// accent aside from OnboardingChapterShell, so this gate reads as a sibling of
// the CLI setup gate rather than a look-alike. The aside copy is per-view here
// (chooser vs device wait), so it is a required prop instead of a default.
function GateShell({ aside, children }: { aside: ShellAsideCopy; children: ReactNode }) {
  return (
    <main className={SCRIM_CLASS} aria-label={aside.ariaLabel}>
      <div className={PANEL_CLASS}>
        <ShellAside copy={aside} />
        <section className={SECTION_CLASS}>
          <div className="flex min-h-0 flex-1 flex-col overflow-y-auto">
            <div className="my-auto w-full px-6 py-8 sm:px-10 sm:py-10">{children}</div>
          </div>
        </section>
      </div>
    </main>
  )
}

/**
 * A value rendered as a click-to-copy block — the device flow's verification
 * link has to be retyped on ANOTHER device, so the whole block is the copy
 * target and the glyph stays faintly visible rather than hover-only.
 */
function CopyField({ value }: { value: string }) {
  const [copied, setCopied] = useState(false)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(
    () => () => {
      if (timer.current) clearTimeout(timer.current)
    },
    [],
  )
  const onCopy = async () => {
    try {
      await copyToClipboard(value)
    } catch {
      // Both clipboard paths failed: do not announce a copy that did not happen.
      return
    }
    setCopied(true)
    if (timer.current) clearTimeout(timer.current)
    timer.current = setTimeout(() => setCopied(false), 1500)
  }
  const label = copied
    ? i18nT('components.kasLogin.copied')
    : i18nT('components.kasLogin.copy_link')
  return (
    <button
      type="button"
      onClick={onCopy}
      aria-label={label}
      title={label}
      className="group/copy mt-1.5 flex w-full cursor-pointer items-center justify-between gap-2 rounded-lg border border-border bg-bg-elevated px-3 py-2 text-left hover:bg-bg-hover focus-ring"
    >
      <code className="min-w-0 overflow-x-auto font-mono text-[13px] text-text-strong">
        {value}
      </code>
      {copied ? (
        <Check className="lucide-inline shrink-0 text-ok" />
      ) : (
        <Copy className="lucide-inline shrink-0 text-muted transition-opacity group-hover/copy:opacity-100" />
      )}
    </button>
  )
}

/** One full-width sign-in choice. `primary` renders the accent-filled variant. */
function ProviderButton({
  icon,
  label,
  primary,
  disabled,
  onClick,
}: {
  icon: ReactNode
  label: string
  primary?: boolean
  disabled?: boolean
  onClick: () => void
}) {
  const base =
    'flex h-11 w-full cursor-pointer items-center justify-center gap-2.5 rounded-lg text-sm transition-all focus-ring active:scale-[0.99] disabled:cursor-not-allowed disabled:opacity-40'
  const variant = primary
    ? 'btn-sweep border-none bg-accent font-semibold text-accent-fg hover:bg-accent-hover hover:shadow-[0_0_20px_var(--accent-glow)]'
    : 'border border-border bg-transparent font-medium text-text hover:border-border-strong hover:bg-bg-hover'
  return (
    <button type="button" disabled={disabled} onClick={onClick} className={`${base} ${variant}`}>
      {icon}
      {label}
    </button>
  )
}

function Chooser({
  busy,
  beginError,
  onPick,
}: {
  busy: boolean
  beginError: string
  onPick: (provider: KasLoginProvider, extra?: KasLoginExtra) => void
}) {
  // The company-SSO choice expands an inline form (start URL + region) instead of
  // beginning immediately: the portal URL is per-company, so there is nothing to
  // start until the user supplies it.
  const [ssoOpen, setSsoOpen] = useState(false)
  const [startUrl, setStartUrl] = useState('')
  const [region, setRegion] = useState('')
  const startUrlReady = startUrl.trim().length > 0
  return (
    <GateShell
      aside={{
        ariaLabel: i18nT('components.kasLogin.sign_in_to_kiro'),
        panelHeadline: i18nT('components.kasLogin.aside_headline'),
        panelBody: i18nT('components.kasLogin.aside_body'),
        panelFootnote: i18nT('components.kasLogin.aside_footnote'),
      }}
    >
      <p className="text-[12px] font-bold uppercase tracking-[0.16em] text-accent">
        {i18nT('components.kasLogin.get_started')}
      </p>
      <h1 className="mt-2 text-3xl font-bold tracking-tight text-text-strong">
        {i18nT('components.kasLogin.sign_in_to_kiro')}
      </h1>
      <div className="mt-7 flex w-full max-w-md flex-col gap-3">
        <ProviderButton
          icon={<Globe className="lucide-inline" />}
          label={i18nT('components.kasLogin.continue_with_google')}
          primary
          disabled={busy}
          onClick={() => onPick('google')}
        />
        <ProviderButton
          icon={<GithubLogo size={15} />}
          label={i18nT('components.kasLogin.continue_with_github')}
          disabled={busy}
          onClick={() => onPick('github')}
        />
        <ProviderButton
          icon={<IdCard className="lucide-inline" />}
          label={i18nT('components.kasLogin.continue_with_builder_id')}
          disabled={busy}
          onClick={() => onPick('builder_id')}
        />
        {ssoOpen ? (
          <form
            className="rounded-lg border border-border bg-bg-elevated p-3"
            data-testid="kas-login-sso-form"
            onSubmit={(e) => {
              e.preventDefault()
              if (!startUrlReady || busy) return
              onPick('idc', {
                start_url: startUrl.trim(),
                ...(region.trim() ? { region: region.trim() } : {}),
              })
            }}
          >
            <label
              className="block text-[12px] font-medium text-text-strong"
              htmlFor="kas-sso-start-url"
            >
              {i18nT('components.kasLogin.sso_start_url_label')}
              <input
                id="kas-sso-start-url"
                type="url"
                aria-label={i18nT('components.kasLogin.sso_start_url_label')}
                autoFocus
                value={startUrl}
                onChange={(e) => setStartUrl(e.target.value)}
                placeholder={i18nT('components.kasLogin.sso_start_url_placeholder')}
                className="focus-ring mt-1 w-full rounded-lg border border-border bg-bg px-3 py-2 font-mono text-[13px] font-normal text-text-strong placeholder:text-muted"
              />
            </label>
            <p className="mt-1 text-[12px] leading-relaxed text-muted">
              {i18nT('components.kasLogin.sso_helper')}
            </p>
            <label
              className="mt-3 block text-[12px] font-medium text-text-strong"
              htmlFor="kas-sso-region"
            >
              {i18nT('components.kasLogin.sso_region_label')}
              <input
                id="kas-sso-region"
                type="text"
                aria-label={i18nT('components.kasLogin.sso_region_label')}
                value={region}
                onChange={(e) => setRegion(e.target.value)}
                placeholder="us-east-1"
                className="focus-ring mt-1 w-full rounded-lg border border-border bg-bg px-3 py-2 font-mono text-[13px] font-normal text-text-strong placeholder:text-muted"
              />
            </label>
            <div className="mt-3 flex items-center gap-2">
              <Btn type="submit" primary disabled={!startUrlReady || busy}>
                {i18nT('components.kasLogin.sso_continue')}
              </Btn>
              <Btn type="button" disabled={busy} onClick={() => setSsoOpen(false)}>
                {i18nT('components.kasLogin.sso_cancel')}
              </Btn>
            </div>
          </form>
        ) : (
          <ProviderButton
            icon={<Building2 className="lucide-inline" />}
            label={i18nT('components.kasLogin.continue_with_company_sso')}
            disabled={busy}
            onClick={() => setSsoOpen(true)}
          />
        )}
      </div>
      {/* role="alert": the failure appears in place after the click, with no
          route change a screen reader would announce. */}
      {beginError ? (
        <div className="mt-4 max-w-md" role="alert">
          <p className="text-[13px] leading-relaxed text-danger">
            {i18nT('components.kasLogin.could_not_start_sign_in')}
          </p>
          {/* Raw backend detail stays visible for bug reports, but on its own
              muted line — never suffixed onto the connection advice, where
              "Unknown provider: x" reads as a contradiction. */}
          <p className="mt-1 font-mono text-[12px] leading-relaxed text-muted">{beginError}</p>
        </div>
      ) : null}
      <p className="mt-5 max-w-md text-[13px] leading-relaxed text-muted">
        {i18nT('components.kasLogin.browser_note')}
      </p>
    </GateShell>
  )
}

function LoopbackWaiting({
  session,
  provider,
  busy,
  onUseCode,
  onCancel,
}: {
  session: KasLoginLoopbackSession
  provider: KasLoginProvider
  /** True while a cancel is settling: every transition off this screen is disabled. */
  busy: boolean
  onUseCode: () => void
  onCancel: () => void
}) {
  // Promote the "use a code" path once the redirect has had a fair chance to
  // land. The listener itself keeps waiting — this is only a hint change.
  const [slow, setSlow] = useState(false)
  useEffect(() => {
    const t = window.setTimeout(() => setSlow(true), LOOPBACK_SLOW_HINT_MS)
    return () => window.clearTimeout(t)
  }, [session.login_id])
  return (
    <GateShell
      aside={{
        ariaLabel: i18nT('components.kasLogin.loopback_waiting_title'),
        panelHeadline: i18nT('components.kasLogin.loopback_aside_headline'),
        panelBody: i18nT('components.kasLogin.loopback_aside_body'),
        panelFootnote: i18nT('components.kasLogin.loopback_aside_footnote'),
      }}
    >
      <div className="flex h-11 w-11 items-center justify-center rounded-xl bg-accent-subtle text-accent">
        <Loader2 className="lucide-inline animate-spin" />
      </div>
      <p className="mt-6 text-[12px] font-bold uppercase tracking-[0.16em] text-accent">
        {i18nT('components.kasLogin.signing_in_with', { provider: providerLabel(provider) })}
      </p>
      <h1 className="mt-2 text-3xl font-bold tracking-tight text-text-strong">
        {i18nT('components.kasLogin.loopback_waiting_title')}
      </h1>
      <p className="mt-3 max-w-md text-sm leading-relaxed text-muted">
        {i18nT('components.kasLogin.loopback_waiting_body')}
      </p>
      {/* role="status": the hint appears without a route change; assistive tech
          should hear it once, not be interrupted by it. */}
      {slow ? (
        <p
          className="mt-4 max-w-md rounded-lg border border-border bg-bg-elevated px-3 py-2 text-[13px] leading-relaxed text-text"
          role="status"
          data-testid="kas-login-loopback-slow"
        >
          {i18nT('components.kasLogin.loopback_slow_hint')}
        </p>
      ) : null}
      <div className="mt-6 flex flex-wrap items-center gap-2">
        <Btn type="button" primary disabled={busy} onClick={() => openAuthTab(session.auth_url)}>
          <ExternalLink className="lucide-inline" />
          {i18nT('components.kasLogin.loopback_open_again')}
        </Btn>
        <Btn type="button" disabled={busy} onClick={onUseCode}>
          {i18nT('components.kasLogin.loopback_use_code')}
        </Btn>
      </div>
      <button
        type="button"
        disabled={busy}
        onClick={onCancel}
        className="mt-5 cursor-pointer text-[13px] text-muted underline-offset-2 hover:text-text hover:underline focus-ring disabled:cursor-not-allowed disabled:opacity-40"
      >
        {i18nT('components.kasLogin.use_different_sign_in')}
      </button>
    </GateShell>
  )
}

function DeviceWaiting({
  session,
  provider,
  fellBack,
  busy,
  onCancel,
}: {
  session: KasLoginDeviceSession
  provider: KasLoginProvider
  /** True when a loopback attempt degraded here, so the screen explains the switch. */
  fellBack?: boolean
  /** True while a cancel is settling: leaving this screen is disabled until then. */
  busy: boolean
  onCancel: () => void
}) {
  return (
    <GateShell
      aside={{
        ariaLabel: i18nT('components.kasLogin.enter_the_code_in_your_browser'),
        panelHeadline: i18nT('components.kasLogin.device_aside_headline'),
        panelBody: i18nT('components.kasLogin.device_aside_body'),
        panelFootnote: i18nT('components.kasLogin.device_aside_footnote'),
      }}
    >
      <div className="flex h-11 w-11 items-center justify-center rounded-xl bg-accent-subtle text-accent">
        <Link2 className="lucide-inline" />
      </div>
      {/* The mockup's "REMOTE HOST" aside eyebrow lives here as a badge: the
          shared ShellAside owns its brand lockup, and forking it for one word
          would un-share the chrome the sibling gates rely on. */}
      <p className="mt-6 flex items-center gap-2 text-[12px] font-bold uppercase tracking-[0.16em] text-accent">
        {i18nT('components.kasLogin.signing_in_with', { provider: providerLabel(provider) })}
        <span className="rounded-full bg-[var(--bg-hover)] px-2 py-[2px] text-[11px] font-semibold normal-case tracking-normal text-muted">
          {i18nT('components.kasLogin.remote_host')}
        </span>
      </p>
      <h1 className="mt-2 text-3xl font-bold tracking-tight text-text-strong">
        {i18nT('components.kasLogin.enter_the_code_in_your_browser')}
      </h1>
      {fellBack ? (
        <p
          className="mt-3 max-w-md text-[13px] leading-relaxed text-muted"
          role="status"
          data-testid="kas-login-fell-back"
        >
          {i18nT('components.kasLogin.loopback_fell_back')}
        </p>
      ) : null}
      <ol className="mt-6 w-full max-w-md list-none space-y-5">
        <li>
          <p className="flex items-center gap-2 text-[13px] font-medium text-text">
            <span className="flex h-5 w-5 items-center justify-center rounded-full bg-accent-subtle font-mono text-[11px] font-bold text-accent">
              {1}
            </span>
            {i18nT('components.kasLogin.open_this_link')}
          </p>
          <CopyField value={session.verification_uri_complete} />
          <p className="mt-1 text-[11px] text-muted">
            {i18nT('components.kasLogin.click_to_copy')}
          </p>
        </li>
        <li>
          <p className="flex items-center gap-2 text-[13px] font-medium text-text">
            <span className="flex h-5 w-5 items-center justify-center rounded-full bg-accent-subtle font-mono text-[11px] font-bold text-accent">
              {2}
            </span>
            {i18nT('components.kasLogin.enter_this_code')}
          </p>
          <div className="mt-1.5 rounded-lg border border-accent/60 bg-accent-subtle/40 px-4 py-3 text-center">
            <span
              className="font-mono text-2xl font-bold tracking-[0.2em] text-text-strong"
              data-testid="kas-login-user-code"
            >
              {session.user_code}
            </span>
          </div>
        </li>
      </ol>
      <p className="mt-6 flex items-center gap-2 text-[13px] text-muted" aria-live="polite">
        <Loader2 className="lucide-inline animate-spin text-accent" />
        {i18nT('components.kasLogin.waiting_for_you_to_approve')}
      </p>
      <p className="mt-2 text-[12px] text-muted">
        {i18nT('components.kasLogin.code_valid_note')}
      </p>
      <button
        type="button"
        disabled={busy}
        onClick={onCancel}
        className="mt-4 text-[13px] font-medium text-accent hover:underline focus-ring disabled:cursor-not-allowed disabled:opacity-40"
      >
        {i18nT('components.kasLogin.use_different_sign_in')}
      </button>
    </GateShell>
  )
}

// Terminal poll outcomes (code expired / poll failed) share one recovery shape:
// name what happened, then offer exactly one action — back to the chooser for a
// fresh code. Detail text is shown verbatim when the backend sent one.
function SignInProblem({
  expired,
  detail,
  onStartOver,
}: {
  expired: boolean
  detail: string
  onStartOver: () => void
}) {
  return (
    <GateShell
      aside={{
        ariaLabel: i18nT('components.kasLogin.sign_in_to_kiro'),
        panelHeadline: i18nT('components.kasLogin.aside_headline'),
        panelBody: i18nT('components.kasLogin.aside_body'),
        panelFootnote: i18nT('components.kasLogin.aside_footnote'),
      }}
    >
      <div className="flex h-11 w-11 items-center justify-center rounded-xl bg-danger/10 text-danger">
        <AlertTriangle className="lucide-inline" />
      </div>
      <p className="mt-6 text-[12px] font-bold uppercase tracking-[0.16em] text-danger">
        {expired
          ? i18nT('components.kasLogin.the_code_expired')
          : i18nT('components.kasLogin.sign_in_failed')}
      </p>
      <h1 className="mt-2 text-3xl font-bold tracking-tight text-text-strong">
        {expired
          ? i18nT('components.kasLogin.the_code_expired_body')
          : i18nT('components.kasLogin.sign_in_failed_body')}
      </h1>
      {detail ? (
        <p className="mt-3 max-w-lg text-sm leading-relaxed text-muted">{detail}</p>
      ) : null}
      <div className="mt-6">
        <Btn type="button" primary onClick={onStartOver}>
          <RefreshCw className="lucide-inline" />
          {i18nT('components.kasLogin.start_over')}
        </Btn>
      </div>
    </GateShell>
  )
}

/**
 * KAS-mode sign-in gate. Replaces the "install kiro-cli and run `kiro-cli
 * login` in a terminal" prerequisite with in-product login buttons: the chooser
 * (Google / GitHub / AWS Builder ID / company SSO) starts a browser
 * authorization, and on a remote gateway — where the OAuth callback cannot
 * reach the user's browser — it switches to the device-code flow and shows the
 * code to approve. NOT yet wired into the app root: pre-integration sibling of
 * KiroPrerequisiteGate.
 */
export default function KasLoginGate({ children }: { children?: ReactNode }) {
  const queryClient = useQueryClient()
  // The sign-in in flight, tagged by transport. `loopback` means the gateway is
  // listening on a local port for the portal's redirect; `device` means the user
  // confirms a code. Null whenever the chooser owns the screen.
  const [session, setSession] = useState<ActiveSession | null>(null)
  // Set when a loopback attempt degraded to the device flow, so the code screen
  // can say why it appeared instead of the promised no-typing sign-in.
  const [fellBack, setFellBack] = useState(false)
  // True from the moment the user abandons a login until the gateway has
  // acknowledged the cancel and the status has been re-read. The waiting screen
  // stays up with its transition buttons disabled, so no second login can be
  // started while the first is still being unwound.
  const [settling, setSettling] = useState(false)
  const statusQuery = useQuery({
    queryKey: QUERY_KEY,
    queryFn: api.kasLoginStatus,
    refetchInterval: 30_000,
  })

  const beginDevice = useMutation({
    mutationFn: ({ provider, extra }: { provider: KasLoginProvider; extra?: KasLoginExtra }) =>
      api.kasLoginBeginDevice(provider, extra),
    onSuccess: (s, { provider }) => setSession({ kind: 'device', provider, ...s }),
  })

  const beginLoopback = useMutation({
    mutationFn: (provider: KasLoginProvider) => api.kasLoginBeginLoopback(provider),
    onSuccess: (s, provider) => {
      setSession({ kind: 'loopback', provider, ...s })
      openAuthTab(s.auth_url)
    },
    // 409 loopback_unavailable (busy ports, or the gateway decided against it) is
    // not a failure the user can act on: silently take the device path instead.
    onError: (err, provider) => {
      if (isLoopbackUnavailable(err)) {
        setFellBack(true)
        beginDevice.mutate({ provider })
      }
    },
  })

  const pollQuery = useQuery({
    queryKey: ['kas-login-poll', session?.login_id],
    // `session!` is safe: `enabled` gates this off until a session exists.
    queryFn: () => api.kasLoginPoll(session!.login_id),
    // Off while a cancel is settling: the id is about to become unknown, and an
    // error answer here must not read as a loopback failure to degrade from.
    enabled: !!session && !settling,
    // Poll at the server-requested cadence and STOP on any terminal answer —
    // an authorized/expired login has nothing left to poll, and a transport
    // failure should surface as the problem screen rather than retry forever.
    refetchInterval: (query) => {
      if (query.state.status === 'error') return false
      const s = query.state.data?.status
      if (s && s !== 'pending') return false
      // The device flow polls at the auth service's expected cadence (kiro-cli
      // uses 5s); the loopback listener is local, so a tighter poll costs nothing
      // and shortens the gap between the redirect landing and the app opening.
      return session?.kind === 'loopback' ? LOOPBACK_POLL_INTERVAL_MS : DEVICE_POLL_INTERVAL_MS
    },
    retry: false,
  })

  // Success is observed, not returned: the poll answering 'authorized' means
  // the gateway now holds a token, so re-read status (the single authority on
  // `authenticated`) and drop the session.
  const authorized = pollQuery.data?.status === 'authorized'
  useEffect(() => {
    if (!authorized) return
    void queryClient.invalidateQueries({ queryKey: QUERY_KEY })
    setSession(null)
    setFellBack(false)
  }, [authorized, queryClient])

  // Loopback degradation: a listener nobody reached (timeout — the browser is
  // not on this machine) or that failed before persisting a token restarts the
  // SAME provider on the device flow. Only a persist failure is a real dead end.
  const pollCode = pollQuery.data?.code
  const pollStatus = pollQuery.data?.status
  useEffect(() => {
    if (session?.kind !== 'loopback' || settling) return
    if (pollStatus !== 'expired' && pollStatus !== 'error') return
    if (pollCode === 'token_store_failed') return
    const provider = session.provider
    setFellBack(true)
    setSession(null)
    beginDevice.mutate({ provider })
    // beginDevice is a stable mutation handle; listing it would re-fire on every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [session, pollStatus, pollCode, settling])

  const status = statusQuery.data

  // Abandon the in-flight login and learn whether a credential landed anyway.
  // The gateway serialises cancel against the poll's persist step, so once
  // cancel SUCCEEDS the outcome is settled: either nothing was written, or the
  // token is already stored. Deciding the next screen before that answer would
  // leave an abandoned credential active behind a second sign-in.
  //   'signed_in'  -- a token landed; the refreshed status renders the app.
  //   'signed_out' -- nothing landed; safe to start another login.
  //   'unknown'    -- cancel or the status read failed; the old login may still
  //                  complete, so the caller must NOT start another one.
  const settleCancel = async (loginId: string): Promise<'signed_in' | 'signed_out' | 'unknown'> => {
    try {
      await api.kasLoginCancel(loginId)
    } catch {
      return 'unknown'
    }
    // An independent request, not fetchQuery: fetchQuery joins a status refetch
    // already in flight, and one that left before the token landed answers
    // "signed out" for a login that just succeeded. The fresh answer is pushed
    // into the cache so the gate re-renders on it.
    try {
      const fresh = await api.kasLoginStatus()
      queryClient.setQueryData(QUERY_KEY, fresh)
      return fresh.authenticated ? 'signed_in' : 'signed_out'
    } catch {
      return 'unknown'
    }
  }

  const reset = async () => {
    const loginId = session?.login_id
    beginDevice.reset()
    beginLoopback.reset()
    if (!loginId) {
      setSession(null)
      setFellBack(false)
      return
    }
    // The waiting screen stays up, buttons disabled, until the gateway has
    // answered. A confirmed outcome clears the session; an unknown one (cancel
    // or the status read failed) keeps it and lets the poll resume, because the
    // old login may still complete and the chooser must not offer a second one.
    setSettling(true)
    const outcome = await settleCancel(loginId)
    setSettling(false)
    if (outcome === 'unknown') return
    setSession(null)
    setFellBack(false)
  }

  const useCodeInstead = async () => {
    if (!session) return
    const { provider, login_id: loginId } = session
    setSettling(true)
    const outcome = await settleCancel(loginId)
    setSettling(false)
    // Unknown: the cancel never settled, so the old login is still live -- stay
    // on its waiting screen with polling resumed rather than racing it.
    if (outcome === 'unknown') return
    setSession(null)
    setFellBack(true)
    // Signed in after all (the portal redirect landed while the user reached
    // for the code): the refreshed status renders the app. Only a confirmed
    // sign-out starts the device flow.
    if (outcome !== 'signed_out') return
    beginDevice.mutate({ provider })
  }

  // Mirror KiroPrerequisiteGate: an unresolved check is UNKNOWN, never a locked
  // door — render the app rather than flashing a sign-in screen at every load.
  if (statusQuery.isPending) return <>{children}</>

  if (!status) {
    return (
      <GateShell
        aside={{
          ariaLabel: i18nT('components.kasLogin.sign_in_to_kiro'),
          panelHeadline: i18nT('components.kasLogin.aside_headline'),
          panelBody: i18nT('components.kasLogin.aside_body'),
          panelFootnote: i18nT('components.kasLogin.aside_footnote'),
        }}
      >
        <div className="flex h-11 w-11 items-center justify-center rounded-xl bg-danger/10 text-danger">
          <AlertTriangle className="lucide-inline" />
        </div>
        <h1 className="mt-6 text-3xl font-bold tracking-tight text-text-strong">
          {i18nT('components.kasLogin.sign_in_status_unavailable')}
        </h1>
        {statusQuery.error?.message ? (
          <p className="mt-3 max-w-lg text-sm leading-relaxed text-muted">
            {statusQuery.error.message}
          </p>
        ) : null}
        <div className="mt-6">
          <Btn
            type="button"
            disabled={statusQuery.isFetching}
            onClick={() => void statusQuery.refetch()}
          >
            <RefreshCw
              className={`lucide-inline ${statusQuery.isFetching ? 'animate-spin' : ''}`}
            />
            {i18nT('components.kasLogin.check_again')}
          </Btn>
        </div>
      </GateShell>
    )
  }

  if (status.authenticated) return <>{children}</>

  if (session) {
    const effective = pollQuery.error ? 'error' : (pollQuery.data?.status ?? 'pending')
    const terminal = effective === 'expired' || effective === 'error'
    // A loopback terminal state is handled by the degradation effect above (it
    // restarts on the device flow); only a token-store failure — or a transport
    // error on the poll itself — is a dead end worth a problem screen.
    const loopbackDeadEnd =
      session.kind === 'loopback' &&
      terminal &&
      (pollQuery.error != null || pollQuery.data?.code === 'token_store_failed')
    if ((session.kind === 'device' && terminal) || loopbackDeadEnd) {
      const detail = pollQuery.error?.message || pollQuery.data?.error || ''
      return <SignInProblem expired={effective === 'expired'} detail={detail} onStartOver={reset} />
    }
    if (session.kind === 'loopback') {
      return (
        <LoopbackWaiting
          session={session}
          provider={session.provider}
          busy={settling}
          onUseCode={useCodeInstead}
          onCancel={reset}
        />
      )
    }
    return (
      <DeviceWaiting
        session={session}
        provider={session.provider}
        fellBack={fellBack}
        busy={settling}
        onCancel={reset}
      />
    )
  }

  // Loopback only when BOTH sides agree the browser shares this machine: the
  // gateway's install-shape verdict, and the page itself being served from a
  // loopback address. A dashboard reached over a tunnel or tailnet fails the
  // second test, so the portal's redirect to localhost would land on the wrong
  // machine — the device flow is the right transport there regardless of shape.
  const loopbackOk = status.transport === 'loopback' && browserOnLoopback()

  return (
    <Chooser
      busy={beginDevice.isPending || beginLoopback.isPending}
      beginError={
        beginDevice.error?.message ??
        (beginLoopback.error && !isLoopbackUnavailable(beginLoopback.error)
          ? beginLoopback.error.message
          : '')
      }
      onPick={(provider, extra) => {
        beginDevice.reset()
        beginLoopback.reset()
        setFellBack(false)
        // Only the two portal-brokered social providers have a loopback flow;
        // Builder ID / IdC run the SSO-OIDC device flow on every shape (as in kiro-cli).
        if (loopbackOk && (provider === 'google' || provider === 'github')) {
          beginLoopback.mutate(provider)
          return
        }
        beginDevice.mutate({ provider, extra })
      }}
    />
  )
}
