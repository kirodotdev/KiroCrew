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
} from 'lucide-react'
import { api, type KasLoginDeviceSession } from '../api/client'
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

function DeviceWaiting({
  session,
  provider,
  onCancel,
}: {
  session: KasLoginDeviceSession
  provider: KasLoginProvider
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
        onClick={onCancel}
        className="mt-4 text-[13px] font-medium text-accent hover:underline focus-ring"
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
  // A device-code sign-in in flight, with the provider it runs under. Null
  // whenever the chooser (or the loopback wait) owns the screen.
  const [device, setDevice] = useState<
    (KasLoginDeviceSession & { provider: KasLoginProvider }) | null
  >(null)
  const statusQuery = useQuery({
    queryKey: QUERY_KEY,
    queryFn: api.kasLoginStatus,
    refetchInterval: 30_000,
  })

  const beginMutation = useMutation({
    mutationFn: ({ provider, extra }: { provider: KasLoginProvider; extra?: KasLoginExtra }) =>
      api.kasLoginBeginDevice(provider, extra),
    onSuccess: (session, { provider }) => setDevice({ ...session, provider }),
  })

  const pollQuery = useQuery({
    queryKey: ['kas-login-poll', device?.login_id],
    // `device!` is safe: `enabled` gates this off until a session exists.
    queryFn: () => api.kasLoginPoll(device!.login_id),
    enabled: !!device,
    // Poll at the server-requested cadence and STOP on any terminal answer —
    // an authorized/expired login has nothing left to poll, and a transport
    // failure should surface as the problem screen rather than retry forever.
    refetchInterval: (query) => {
      if (query.state.status === 'error') return false
      const s = query.state.data?.status
      if (s && s !== 'pending') return false
      // The backend does not echo the provider's poll interval, so poll at the
      // device-flow default the auth service expects (kiro-cli uses 5s).
      return DEVICE_POLL_INTERVAL_MS
    },
    retry: false,
  })

  // Success is observed, not returned: the poll answering 'authorized' means
  // the gateway now holds a token, so re-read status (the single authority on
  // `authenticated`) and drop the device session.
  const authorized = pollQuery.data?.status === 'authorized'
  useEffect(() => {
    if (!authorized) return
    void queryClient.invalidateQueries({ queryKey: QUERY_KEY })
    setDevice(null)
  }, [authorized, queryClient])

  const status = statusQuery.data

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

  if (device) {
    const pollStatus = pollQuery.error ? 'error' : (pollQuery.data?.status ?? 'pending')
    if (pollStatus === 'expired' || pollStatus === 'error') {
      const detail = pollQuery.error?.message || pollQuery.data?.error || ''
      return (
        <SignInProblem
          expired={pollStatus === 'expired'}
          detail={detail}
          onStartOver={() => {
            setDevice(null)
            beginMutation.reset()
          }}
        />
      )
    }
    return (
      <DeviceWaiting
        session={device}
        provider={device.provider}
        onCancel={() => {
          setDevice(null)
          beginMutation.reset()
        }}
      />
    )
  }

  return (
    <Chooser
      busy={beginMutation.isPending}
      beginError={beginMutation.error?.message ?? ''}
      onPick={(provider, extra) => {
        beginMutation.reset()
        // The device-code flow is the one begin path the backend exposes, and it
        // works identically from a browser on both install shapes — the old
        // transport branch parked desktop users on a loopback screen no backend
        // endpoint could ever complete.
        beginMutation.mutate({ provider, extra })
      }}
    />
  )
}
