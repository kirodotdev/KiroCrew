import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from 'react'
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
import {
  type KasLoginDeviceSession,
  type KasLoginLoopbackSession,
  type KasLoginStatus,
} from '../api/client'
import { apiErrorCode } from '../api/apiError'
import { isUnansweredRead } from '../api/queryClient'
import { reportForError, type ErrorReport } from '../utils/errorReport'
import {
  PANEL_CLASS,
  SCRIM_CLASS,
  SECTION_CLASS,
  ShellAside,
  type ShellAsideCopy,
} from './OnboardingChapterShell'
import GithubLogo from './icons/GithubLogo'
import { Btn } from './ui'
import { readCompanySignIn, rememberCompanySignIn } from '../utils/companySignIn'
import ErrorNotice from './ErrorNotice'
import { copyToClipboard } from '../utils/clipboard'
import { safeHref } from '../utils/safeHref'
import { openAuthTab, useKasLoginFlow, type KasLoginExtra } from '../hooks/useKasLogin'
import { providerLabel, type KasLoginProvider } from './kasProviderLabel'

import { i18nT } from '../i18n/t'
import { fmtTime } from '../i18n/format'

// After this long on the loopback wait screen the screen says the sign-in is not
// finished: the tab was closed or blocked, or the redirect cannot land.
const LOOPBACK_SLOW_HINT_MS = 30_000
// The begin error's DOM id, so the SSO field it names can point at it.
const SSO_ERROR_ID = 'kas-sso-begin-error'

/**
 * Which chrome the flow renders in. `gate` is the full-screen door (scrim +
 * panel + accent aside, a sibling of KiroPrerequisiteGate); `card` is the same
 * flow embedded inside a dashboard card, where the card already supplies the
 * title and there is no room for a 3xl headline or an aside. The views below
 * read this instead of taking a prop each, so adding the embedded variant did
 * not fork six components.
 */
type Chrome = 'gate' | 'card'
const ChromeContext = createContext<Chrome>('gate')
// True when only an organisation sign-in can work (an LTS gateway with
// nobody signed in): the chooser offers Company SSO alone, already open.
const OrganisationOnlyContext = createContext(false)

/** Per-chrome class choices for the recurring text roles. Gate values are the
 *  strings the full-screen views always rendered; card values scale them down
 *  to sit under a CardTitle. */
function useChrome() {
  const chrome = useContext(ChromeContext)
  const card = chrome === 'card'
  // The view headline: a page title on the gate, a section heading in a card.
  const headline = card
    ? 'text-base font-semibold tracking-tight text-text-strong'
    : 'text-3xl font-bold tracking-tight text-text-strong'
  return {
    card,
    headline,
    // The headline with its usual gap below an eyebrow.
    h1: `${card ? 'mt-1' : 'mt-2'} ${headline}`,
    // The accent eyebrow above it.
    eyebrow: card
      ? 'text-[11px] font-bold uppercase tracking-[0.14em] text-accent'
      : 'text-[12px] font-bold uppercase tracking-[0.16em] text-accent',
    // The status icon tile; smaller in a card so it does not dominate the row.
    tile: card
      ? 'flex h-8 w-8 items-center justify-center rounded-lg'
      : 'flex h-11 w-11 items-center justify-center rounded-xl',
    // Vertical rhythm between the tile and the eyebrow.
    afterTile: card ? 'mt-3' : 'mt-6',
  }
}

// Same full-screen chrome as KiroPrerequisiteGate's SetupShell: scrim + panel +
// accent aside from OnboardingChapterShell, so this gate reads as a sibling of
// the CLI setup gate rather than a look-alike. The aside copy is per-view here
// (chooser vs device wait), so it is a required prop instead of a default.
//
// In card chrome the aside has nowhere to go (the card's own title carries the
// context) and the scrim would cover the page around it, so the view renders as
// a plain region: same children, same aria-label, no door.
function ViewShell({ aside, children }: { aside: ShellAsideCopy; children: ReactNode }) {
  const { card } = useChrome()
  if (card) {
    return (
      <section aria-label={aside.ariaLabel} data-testid="kas-login-embedded">
        {children}
      </section>
    )
  }
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
 * The device flow's verification link: the URL itself opens the issuer's page
 * in a new tab (a click is a user gesture, so a popup blocker lets it through),
 * and a separate always-visible button copies it for approval on ANOTHER device.
 */
function LinkField({ value }: { value: string }) {
  const [copied, setCopied] = useState(false)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(
    () => () => {
      if (timer.current) clearTimeout(timer.current)
    },
    [],
  )
  const onCopy = async () => {
    // Both clipboard paths failed: do not announce a copy that did not happen.
    if (!(await copyToClipboard(value))) return
    setCopied(true)
    if (timer.current) clearTimeout(timer.current)
    timer.current = setTimeout(() => setCopied(false), 1500)
  }
  const label = copied
    ? i18nT('components.kasLogin.copied')
    : i18nT('components.kasLogin.copy_link')
  // The URL comes from the issuer's response: only an http(s) URL becomes a
  // link, so no other scheme (javascript:, data:) can run on a click.
  const href = safeHref(value)
  return (
    <div className="mt-1.5 flex w-full items-center gap-2 rounded-lg border border-border bg-bg-elevated py-1.5 pl-3 pr-1.5">
      {href ? (
        <a
          href={href}
          target="_blank"
          rel="noopener noreferrer"
          data-testid="kas-login-verification-link"
          className="min-w-0 flex-1 overflow-x-auto font-mono text-[13px] text-accent underline underline-offset-2 hover:text-accent-hover focus-ring"
        >
          {value}
        </a>
      ) : (
        <code className="min-w-0 flex-1 overflow-x-auto font-mono text-[13px] text-text-strong">
          {value}
        </code>
      )}
      <button
        type="button"
        onClick={onCopy}
        aria-label={label}
        title={label}
        className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md border border-border bg-bg text-muted hover:bg-bg-hover hover:text-text focus-ring"
      >
        {copied ? (
          <Check className="lucide-inline text-ok" />
        ) : (
          <Copy className="lucide-inline" />
        )}
      </button>
    </div>
  )
}

/**
 * One sign-in choice. `primary` renders the accent-filled variant — on the
 * gate only. In card chrome every choice is the outlined, shorter form and the
 * four sit in a two-column grid: the card lives inside a Settings detail whose
 * own primary button is "Use {{name}}", and a second accent-filled button the
 * full width of the panel read as the page's main action rather than as one of
 * four equal ways to sign in.
 */
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
  const { card } = useChrome()
  const base = card
    ? 'flex min-h-9 w-full cursor-pointer items-center justify-center gap-2 rounded-lg px-3 text-[13px] transition-all focus-ring active:scale-[0.99] disabled:cursor-not-allowed disabled:opacity-40'
    : 'flex h-11 w-full cursor-pointer items-center justify-center gap-2.5 rounded-lg text-sm transition-all focus-ring active:scale-[0.99] disabled:cursor-not-allowed disabled:opacity-40'
  const variant = primary && !card
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
  beginErrorCode,
  intro,
  onPick,
}: {
  busy: boolean
  beginError: string
  /** The begin refusal's `code`, or '' (see apiErrorCode). */
  beginErrorCode: string
  /** Card chrome's intro line in place of the default (`KasLoginFlowSlots.intro`). */
  intro?: ReactNode
  onPick: (provider: KasLoginProvider, extra?: KasLoginExtra) => void
}) {
  // The company-SSO choice expands an inline form (start URL + region) instead of
  // beginning immediately: the portal URL is per-company, so there is nothing to
  // start until the user supplies it.
  const organisationOnly = useContext(OrganisationOnlyContext)
  const [ssoOpen, setSsoOpen] = useState(organisationOnly)
  // Seeded from the last company sign-in submitted on this browser, so a reload
  // or gateway restart does not hand back an empty field.
  const [startUrl, setStartUrl] = useState(() => readCompanySignIn().startUrl)
  const [region, setRegion] = useState(() => readCompanySignIn().region)
  const startUrlReady = startUrl.trim().length > 0
  // The 400s the user fixes in this form: the error names the field (both, when
  // AWS's answer names neither) and is tied to it, instead of sending them to
  // check their connection.
  const eitherBad = beginErrorCode === 'start_url_or_region_rejected'
  const startUrlBad = eitherBad || beginErrorCode === 'invalid_start_url'
  const regionBad = eitherBad || beginErrorCode === 'invalid_region'
  const chrome = useChrome()
  return (
    <ViewShell
      aside={{
        ariaLabel: i18nT('components.kasLogin.sign_in_to_kiro'),
        panelHeadline: i18nT('components.kasLogin.aside_headline'),
        panelBody: i18nT('components.kasLogin.aside_body'),
        panelFootnote: i18nT('components.kasLogin.aside_footnote'),
      }}
    >
      {chrome.card ? (
        // The card's title already says "Kiro sign-in"; what the embedded
        // chooser needs is one sentence saying WHICH backend the identity is
        // for -- it sits under a switch that also lists Kiro CLI, whose own
        // kiro-cli login this sign-in never touches.
        <p className="text-[13px] leading-relaxed text-muted" data-testid="kas-login-card-intro">
          {intro ?? i18nT('components.kasLogin.card_intro')}
        </p>
      ) : (
        <>
          <p className={chrome.eyebrow}>{i18nT('components.kasLogin.get_started')}</p>
          <h1 className={chrome.h1}>{i18nT('components.kasLogin.sign_in_to_kiro')}</h1>
        </>
      )}
      <div
        className={
          chrome.card
            ? 'mt-3 grid w-full max-w-lg grid-cols-1 gap-2 sm:grid-cols-2'
            : 'mt-7 flex w-full max-w-md flex-col gap-3'
        }
      >
        {!organisationOnly && (
          <>
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
          </>
        )}
        {ssoOpen ? (
          <form
            className="rounded-lg border border-border bg-bg-elevated p-3 sm:col-span-full"
            data-testid="kas-login-sso-form"
            onSubmit={(e) => {
              e.preventDefault()
              if (!startUrlReady || busy) return
              rememberCompanySignIn(startUrl, region)
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
              {/* type="text", not "url": the browser's URL check refuses a paste
                  without "https://" before the gateway can add it. inputMode
                  keeps the URL keyboard on touch devices. */}
              <input
                id="kas-sso-start-url"
                type="text"
                inputMode="url"
                autoCapitalize="none"
                autoCorrect="off"
                spellCheck={false}
                aria-label={i18nT('components.kasLogin.sso_start_url_label')}
                aria-invalid={startUrlBad || undefined}
                aria-describedby={startUrlBad ? SSO_ERROR_ID : undefined}
                autoFocus
                value={startUrl}
                onChange={(e) => setStartUrl(e.target.value)}
                placeholder={i18nT('components.kasLogin.sso_start_url_placeholder')}
                className="focus-ring mt-1 w-full rounded-lg border border-border bg-bg px-3 py-2 font-mono text-[13px] font-normal text-text-strong placeholder:text-muted aria-invalid:border-danger"
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
                aria-invalid={regionBad || undefined}
                aria-describedby={regionBad ? SSO_ERROR_ID : undefined}
                value={region}
                onChange={(e) => setRegion(e.target.value)}
                placeholder="us-east-1"
                className="focus-ring mt-1 w-full rounded-lg border border-border bg-bg px-3 py-2 font-mono text-[13px] font-normal text-text-strong placeholder:text-muted aria-invalid:border-danger"
              />
            </label>
            <div className="mt-3 flex items-center gap-2">
              <Btn type="submit" primary disabled={!startUrlReady || busy}>
                {i18nT('components.kasLogin.sso_continue')}
              </Btn>
              {!organisationOnly && (
                <Btn type="button" disabled={busy} onClick={() => setSsoOpen(false)}>
                  {i18nT('components.kasLogin.sso_cancel')}
                </Btn>
              )}
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
      {/* The failure appears in place after the click (ErrorNotice is
          role="alert"), with no route change a screen reader would announce.
          The raw backend detail is the `message` (kept verbatim for bug
          reports) under the title — never suffixed onto the connection
          advice, where "Unknown provider: x" reads as a contradiction. */}
      {/* Hand-off decision per chrome. Gate: none -- the Company SSO start-URL
          field (the form above) is unsaved, and the gate stands between the
          user and the chat the hand-off would navigate to. Card: the chat is
          one click away and there is no draft unless the SSO form is open, so
          the agent is offered exactly when nothing would be lost. */}
      <ErrorNotice
        id={SSO_ERROR_ID}
        className={chrome.card ? 'mt-3 max-w-lg' : 'mt-4 max-w-md'}
        askAgent={chrome.card && !ssoOpen}
        title={
          eitherBad
            ? i18nT('components.kasLogin.sso_start_url_or_region_invalid')
            : regionBad
              ? i18nT('components.kasLogin.sso_region_invalid')
              : startUrlBad
                ? i18nT('components.kasLogin.sso_start_url_invalid')
                : i18nT('components.kasLogin.could_not_start_sign_in')
        }
        message={beginError || null}
        testId="kas-login-begin-error"
      />
      {/* "Each option starts…" reads wrong over a single option. */}
      {!organisationOnly && (
        <p
          className={
            chrome.card
              ? 'mt-3 max-w-lg text-[12px] leading-relaxed text-muted'
              : 'mt-5 max-w-md text-[13px] leading-relaxed text-muted'
          }
        >
          {i18nT('components.kasLogin.browser_note')}
        </p>
      )}
    </ViewShell>
  )
}

function LoopbackWaiting({
  session,
  provider,
  busy,
  notice,
  onUseCode,
  onStartOver,
  onCancel,
}: {
  session: KasLoginLoopbackSession
  provider: KasLoginProvider
  /** True while a cancel is settling: every transition off this screen is disabled. */
  busy: boolean
  /** A cancel that did not settle (see settleCancel): shown, never swallowed. */
  notice: string
  onUseCode: () => void
  onStartOver: () => void
  onCancel: () => void
}) {
  // Once the redirect has had a fair chance to land, say the sign-in is not
  // finished. The tab is opened with noopener, so this page cannot see whether
  // it was closed or blocked; the listener itself keeps waiting.
  const [slow, setSlow] = useState(false)
  useEffect(() => {
    const t = window.setTimeout(() => setSlow(true), LOOPBACK_SLOW_HINT_MS)
    return () => window.clearTimeout(t)
  }, [session.login_id])
  const chrome = useChrome()
  return (
    <ViewShell
      aside={{
        ariaLabel: i18nT('components.kasLogin.loopback_waiting_title'),
        panelHeadline: i18nT('components.kasLogin.loopback_aside_headline'),
        panelBody: i18nT('components.kasLogin.loopback_aside_body'),
        panelFootnote: i18nT('components.kasLogin.loopback_aside_footnote'),
      }}
    >
      <div className={`${chrome.tile} bg-accent-subtle text-accent`}>
        <Loader2 className="lucide-inline animate-spin" />
      </div>
      <p className={`${chrome.afterTile} ${chrome.eyebrow}`}>
        {i18nT('components.kasLogin.signing_in_with', { provider: providerLabel(provider) })}
      </p>
      <h1 className={chrome.h1}>
        {i18nT('components.kasLogin.loopback_waiting_title')}
      </h1>
      <p className="mt-3 max-w-md text-sm leading-relaxed text-muted">
        {i18nT('components.kasLogin.loopback_waiting_body')}
      </p>
      {/* role="status", mounted before the notice fills it: assistive tech
          announces a live region's change, not one that arrives full, and
          should hear it once without being interrupted by it. */}
      <div role="status">
        {slow ? (
          <div
            className="mt-4 max-w-md rounded-lg border border-border bg-bg-elevated px-3 py-2"
            data-testid="kas-login-loopback-slow"
          >
            <p className="text-[13px] leading-relaxed text-text">
              {i18nT('components.kasLogin.loopback_slow_hint')}
            </p>
            <Btn type="button" className="mt-2" disabled={busy} onClick={onStartOver}>
              <RefreshCw className="lucide-inline" />
              {i18nT('components.kasLogin.start_over')}
            </Btn>
          </div>
        ) : null}
      </div>
      {/* Hand-off per chrome: none on the gate (the chat behind it is not
          reachable); offered in the card, where the chat is a click away and a
          cancel that did not settle holds no draft. */}
      <ErrorNotice
        className="mt-4 max-w-md"
        askAgent={chrome.card}
        message={notice || null}
        testId="kas-login-settle-error"
      />
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
    </ViewShell>
  )
}

function DeviceWaiting({
  session,
  provider,
  fellBack,
  busy,
  notice,
  onCancel,
}: {
  session: KasLoginDeviceSession
  provider: KasLoginProvider
  /** True when a loopback attempt degraded here, so the screen explains the switch. */
  fellBack?: boolean
  /** True while a cancel is settling: leaving this screen is disabled until then. */
  busy: boolean
  /** A cancel that did not settle (see settleCancel): shown, never swallowed. */
  notice: string
  onCancel: () => void
}) {
  const chrome = useChrome()
  return (
    <ViewShell
      aside={{
        ariaLabel: i18nT('components.kasLogin.enter_the_code_in_your_browser'),
        panelHeadline: i18nT('components.kasLogin.device_aside_headline'),
        panelBody: i18nT('components.kasLogin.device_aside_body'),
        panelFootnote: i18nT('components.kasLogin.device_aside_footnote'),
      }}
    >
      <div className={`${chrome.tile} bg-accent-subtle text-accent`}>
        <Link2 className="lucide-inline" />
      </div>
      {/* The mockup's "REMOTE HOST" aside eyebrow lives here as a badge: the
          shared ShellAside owns its brand lockup, and forking it for one word
          would un-share the chrome the sibling gates rely on. */}
      <p className={`${chrome.afterTile} flex items-center gap-2 ${chrome.eyebrow}`}>
        {i18nT('components.kasLogin.signing_in_with', { provider: providerLabel(provider) })}
        <span className="rounded-full bg-[var(--bg-hover)] px-2 py-[2px] text-[11px] font-semibold normal-case tracking-normal text-muted">
          {i18nT('components.kasLogin.remote_host')}
        </span>
      </p>
      <h1 className={chrome.h1}>
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
          <LinkField value={session.verification_uri_complete} />
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
      <p className="mt-2 text-[12px] text-muted" data-testid="kas-login-code-expiry">
        {i18nT('components.kasLogin.code_valid_note', { time: fmtTime(session.expires_at) })}
      </p>
      {/* Hand-off per chrome: none on the gate (the chat behind it is not
          reachable); offered in the card, where the chat is a click away and a
          cancel that did not settle holds no draft. */}
      <ErrorNotice
        className="mt-4 max-w-md"
        askAgent={chrome.card}
        message={notice || null}
        testId="kas-login-settle-error"
      />
      <button
        type="button"
        disabled={busy}
        onClick={onCancel}
        className="mt-4 text-[13px] font-medium text-accent hover:underline focus-ring disabled:cursor-not-allowed disabled:opacity-40"
      >
        {i18nT('components.kasLogin.use_different_sign_in')}
      </button>
    </ViewShell>
  )
}

// Terminal poll outcomes (code expired / poll failed) share one recovery shape:
// name what happened, then offer a fresh attempt with the same sign-in, and a
// smaller link back to the chooser to switch method. Detail text is shown
// verbatim when the backend sent one. An Identity Center sign-in whose Kiro
// profile could not be found gets its own plain sentence per `code`: "no
// profile" only when the lookup answered with none, never when the lookup
// itself failed. A poll the gateway never answered gets one too, since the
// browser's own text for it ("Failed to fetch") names no cause and no remedy.
function SignInProblem({
  expired,
  code,
  connectionLost,
  report,
  detail,
  busy,
  notice,
  onStartOver,
  onCancel,
}: {
  expired: boolean
  code?: string
  /** The poll got no answer from the gateway: it restarted, or the link dropped. */
  connectionLost: boolean
  /** The poll failure's own report, so the hand-off keeps it under a reworded sentence. */
  report?: ErrorReport
  detail: string
  /** True while the old attempt's cancel is settling: both actions are disabled. */
  busy: boolean
  /** A cancel that did not settle (see settleCancel): shown, never swallowed. */
  notice: string
  onStartOver: () => void
  onCancel: () => void
}) {
  const chrome = useChrome()
  // The coded sentence goes through ErrorNotice (alert role, hand-off), under a
  // plain "Sign-in failed" headline; expired and uncoded failures keep the
  // eyebrow-plus-headline shape.
  const coded = expired
    ? ''
    : connectionLost
      ? i18nT('components.kasLogin.connection_lost_body')
      : code === 'no_kiro_profile'
        ? i18nT('components.kasLogin.no_kiro_profile_body')
        : code === 'profile_lookup_failed'
          ? i18nT('components.kasLogin.profile_lookup_failed_body')
          : ''
  return (
    <ViewShell
      aside={{
        ariaLabel: i18nT('components.kasLogin.sign_in_to_kiro'),
        panelHeadline: i18nT('components.kasLogin.aside_headline'),
        panelBody: i18nT('components.kasLogin.aside_body'),
        panelFootnote: i18nT('components.kasLogin.aside_footnote'),
      }}
    >
      <div className={`${chrome.tile} bg-danger/10 text-danger-text`}>
        <AlertTriangle className="lucide-inline" />
      </div>
      {coded ? (
        <h1 className={`${chrome.afterTile} ${chrome.headline}`}>
          {i18nT('components.kasLogin.sign_in_failed')}
        </h1>
      ) : (
        <>
          <p className={`${chrome.afterTile} ${chrome.eyebrow.replace('text-accent', 'text-danger')}`}>
            {expired
              ? i18nT('components.kasLogin.the_code_expired')
              : i18nT('components.kasLogin.sign_in_failed')}
          </p>
          <h1 className={chrome.h1}>
            {expired
              ? i18nT('components.kasLogin.the_code_expired_body')
              : i18nT('components.kasLogin.sign_in_failed_body')}
          </h1>
        </>
      )}
      {/* Hand-off per chrome. Gate: none -- the chat the hand-off would open
          sits behind it, and without a signed-in account there is no agent to
          hand the failure to; Start over is the remedy. Card: the dashboard
          is signed in to itself regardless of this sign-in, and the card holds
          no draft, so the agent is offered. */}
      <ErrorNotice
        className="mt-3 max-w-lg"
        askAgent={chrome.card}
        message={coded || detail || null}
        report={report}
        testId="kas-login-problem-detail"
      />
      <ErrorNotice
        className="mt-3 max-w-lg"
        askAgent={chrome.card}
        message={notice || null}
        testId="kas-login-settle-error"
      />
      <div className="mt-6">
        <Btn type="button" primary disabled={busy} onClick={onStartOver}>
          <RefreshCw className="lucide-inline" />
          {i18nT('components.kasLogin.start_over')}
        </Btn>
      </div>
      <button
        type="button"
        disabled={busy}
        onClick={onCancel}
        className="mt-4 text-[13px] font-medium text-accent hover:underline focus-ring disabled:cursor-not-allowed disabled:opacity-40"
      >
        {i18nT('components.kasLogin.use_different_sign_in')}
      </button>
    </ViewShell>
  )
}

/**
 * Render hooks the two chromes plug into the shared flow. The gate renders
 * `children` (the app) for both; the embedded card renders a loader and a
 * signed-in summary. Kept as props on the flow rather than branches inside it,
 * so the flow's state machine has exactly one copy.
 */
export interface KasLoginFlowSlots {
  /** While the first status read is in flight. Default: `children`. */
  renderPending?: () => ReactNode
  /**
   * When the gateway holds a credential and no re-authentication was asked
   * for. `reauth` starts the chooser over the current sign-in (a new
   * credential landing in the same slot replaces it). Default: `children`.
   */
  renderAuthenticated?: (status: KasLoginStatus, actions: { reauth: () => void }) => ReactNode
  /**
   * Shown under the chooser while a signed-in identity is being replaced, so
   * the user can back out of the replacement. `busy` is true while a begin is
   * in flight; the control MUST be disabled then, because backing out at that
   * moment would let the begin's success open a poll that persists the
   * replacement after the user thinks they cancelled. Only meaningful with
   * `renderAuthenticated`; the gate never re-authenticates.
   */
  renderReauthCancel?: (cancel: () => void, busy: boolean) => ReactNode
  /**
   * The line above the provider buttons in card chrome. Default: the Settings
   * card's sentence saying which harness the sign-in is for. A surface that
   * asks for the sign-in for its own reason (the chat's prompt) says that.
   */
  intro?: ReactNode
}

/**
 * KAS-mode sign-in gate. Replaces the "install kiro-cli and run `kiro-cli
 * login` in a terminal" prerequisite with in-product login buttons: the chooser
 * (Google / GitHub / AWS Builder ID / company SSO) starts a browser
 * authorization, and on a remote gateway — where the OAuth callback cannot
 * reach the user's browser — it switches to the device-code flow and shows the
 * code to approve. Not mounted at the app root: the flow's product entry point
 * is the embedded variant on Settings → Agent Harness (`KasLoginEmbedded`,
 * rendered by `KiroSignInCard` under the harness switch); this full-screen form stays
 * available for a future app-root mount as a sibling of KiroPrerequisiteGate.
 */
export default function KasLoginGate({
  children,
  organisationOnly = false,
}: {
  children?: ReactNode
  /** Offer only Company SSO: an enterprise gateway accepts no other sign-in. */
  organisationOnly?: boolean
}) {
  return (
    <ChromeContext.Provider value="gate">
      <OrganisationOnlyContext.Provider value={organisationOnly}>
        <KasLoginFlow>{children}</KasLoginFlow>
      </OrganisationOnlyContext.Provider>
    </ChromeContext.Provider>
  )
}

/**
 * The same flow embedded in a dashboard card: no scrim, compact headings, and
 * instead of rendering the app when signed in it renders a token-free summary
 * of the identity (with sign-out and sign-in-again). `renderAuthenticated` is
 * required here because a card has no "app" to fall through to.
 */
export function KasLoginEmbedded(props: Required<Pick<KasLoginFlowSlots, 'renderAuthenticated'>> & KasLoginFlowSlots) {
  return (
    <ChromeContext.Provider value="card">
      <KasLoginFlow {...props} />
    </ChromeContext.Provider>
  )
}

function KasLoginFlow({
  children,
  renderPending,
  renderAuthenticated,
  renderReauthCancel,
  intro,
}: { children?: ReactNode } & KasLoginFlowSlots) {
  const chrome = useChrome()
  const flow = useKasLoginFlow()
  const { statusQuery, status, session, pollQuery, settling, settleError } = flow

  // Mirror KiroPrerequisiteGate: an unresolved check is UNKNOWN, never a locked
  // door — render the app rather than flashing a sign-in screen at every load.
  if (statusQuery.isPending) return <>{renderPending ? renderPending() : children}</>

  if (!status) {
    return (
      <ViewShell
        aside={{
          ariaLabel: i18nT('components.kasLogin.sign_in_to_kiro'),
          panelHeadline: i18nT('components.kasLogin.aside_headline'),
          panelBody: i18nT('components.kasLogin.aside_body'),
          panelFootnote: i18nT('components.kasLogin.aside_footnote'),
        }}
      >
        <div className={`${chrome.tile} bg-danger/10 text-danger-text`}>
          <AlertTriangle className="lucide-inline" />
        </div>
        <h1 className={`${chrome.afterTile} ${chrome.headline}`}>
          {i18nT('components.kasLogin.sign_in_status_unavailable')}
        </h1>
        {/* Hand-off per chrome. Gate: none -- it cannot tell whether anyone is
            signed in, so the chat behind it (and the agent) is not reachable;
            Check again is the remedy. Card: the dashboard's own agent is
            reachable whatever this status read says, and a failed status read
            (a vault it cannot open) is exactly what `kirocrew doctor` and the
            agent diagnose. */}
        <ErrorNotice
          className="mt-3 max-w-lg"
          askAgent={chrome.card}
          message={statusQuery.error?.message || null}
          testId="kas-login-status-error"
        />
        <div className={chrome.card ? 'mt-3' : 'mt-6'}>
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
      </ViewShell>
    )
  }

  if (status.authenticated && !flow.reauth) {
    const unsettledLoginId = flow.unsettledLoginId
    return (
      <>
        {flow.replaceWarning ? (
          // askAgent on: nothing here is a draft, and a vault entry that could
          // not be deleted is a store/permissions fault the agent can diagnose.
          // Dismissable: once the user has signed the old account out by hand
          // the notice has done its job.
          <ErrorNotice
            className={chrome.card ? 'mb-3' : 'mb-4 max-w-lg'}
            askAgent
            title={i18nT('components.kasLogin.previous_sign_in_not_removed')}
            message={i18nT('components.kasLogin.previous_sign_in_not_removed_body')}
            onDismiss={flow.dismissReplaceWarning}
            testId="kas-login-replace-warning"
          />
        ) : null}
        {unsettledLoginId ? (
          // askAgent on, as above: nothing here is a draft, and the agent runs
          // as the account that is signed in.
          <ErrorNotice
            className={chrome.card ? 'mb-3' : 'mb-4 max-w-lg'}
            askAgent
            message={i18nT('components.kasLogin.pending_cancel_unsettled')}
            footer={
              <Btn type="button" onClick={() => flow.retryDroppedCancel(unsettledLoginId)}>
                {i18nT('components.kiroPrerequisiteGate.try_again')}
              </Btn>
            }
            onDismiss={flow.dismissUnsettled}
            testId="kas-login-cancel-unsettled"
          />
        ) : null}
        {renderAuthenticated ? renderAuthenticated(status, { reauth: flow.startReauth }) : children}
      </>
    )
  }

  if (session) {
    const effective = pollQuery.error ? 'error' : (pollQuery.data?.status ?? 'pending')
    const terminal = effective === 'expired' || effective === 'error'
    // A loopback terminal state is handled by the hook's degradation effect (it
    // restarts on the device flow); only a token-store failure — or a transport
    // error on the poll itself — is a dead end worth a problem screen.
    const loopbackDeadEnd =
      session.kind === 'loopback' &&
      terminal &&
      (pollQuery.error != null || pollQuery.data?.code === 'token_store_failed')
    if ((session.kind === 'device' && terminal) || loopbackDeadEnd) {
      const detail = pollQuery.error?.message || pollQuery.data?.error || ''
      return (
        <SignInProblem
          expired={effective === 'expired'}
          code={pollQuery.error ? undefined : pollQuery.data?.code}
          // A coded refusal is the gateway's own answer (its 502 when the auth
          // service is unreachable), so it keeps its text.
          connectionLost={isUnansweredRead(pollQuery.error) && !apiErrorCode(pollQuery.error)}
          report={reportForError(pollQuery.error)}
          detail={detail}
          busy={settling}
          notice={settleError}
          onStartOver={flow.startOver}
          onCancel={flow.reset}
        />
      )
    }
    if (session.kind === 'loopback') {
      return (
        <LoopbackWaiting
          // Keyed by attempt: a Start over gets a fresh 30 s wait, not the old notice.
          key={session.login_id}
          session={session}
          provider={session.provider}
          busy={settling}
          notice={settleError}
          onUseCode={flow.switchToCode}
          onStartOver={flow.startOver}
          onCancel={flow.reset}
        />
      )
    }
    return (
      <DeviceWaiting
        session={session}
        provider={session.provider}
        fellBack={flow.fellBack}
        busy={settling}
        notice={settleError}
        onCancel={flow.reset}
      />
    )
  }

  return (
    <>
      <Chooser
        busy={flow.beginBusy}
        beginError={flow.beginError}
        beginErrorCode={flow.beginErrorCode}
        intro={intro}
        onPick={flow.pick}
      />
      {/* Replacing a signed-in identity is reversible until a new credential
          lands: the stored one is untouched while the chooser is up. NOT while
          a begin is in flight, though -- its onSuccess would open a session and
          start polling after the cancel had already closed the chooser, and the
          poll would then persist the replacement nobody is watching. The card
          disables the back-out for that window (`busy`). */}
      {flow.reauth && status.authenticated && renderReauthCancel
        ? renderReauthCancel(flow.cancelReauth, flow.beginBusy)
        : null}
    </>
  )
}
