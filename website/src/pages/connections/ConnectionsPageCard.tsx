/**
 * One provider card in the Connections page's Services gallery.
 *
 * `ConnectionCard` renders a provider's header, its value prop and the action
 * row for the card's state (not connected, waiting for approval with the
 * return-address relay, needs configuration, not verified, connected, needs
 * attention), opens and fills the approval tab a consent-starting click asks
 * for, and shows the card's feedback line. `PrerequisiteTip` is the warning
 * triangle beside an action ("What a card shows inline, and what it hides behind
 * the triangle" in `docs/system-specs/modules/connections.md`).
 * `ConnectionsPage.tsx` decides each card's state and runs its actions; this
 * module never imports it.
 */
import { useEffect, useId, useRef, useState, type ReactNode } from 'react'
import { OverlayPortal } from '../../components/overlayRoot'
import { useTranslation } from 'react-i18next'
import {
  AlertTriangle,
  CheckCircle2,
  CircleDashed,
  ExternalLink,
  KeyRound,
  Link2,
  Loader2,
  Lock,
  RotateCw,
  Settings2,
  Unplug,
  X,
} from 'lucide-react'
import { Link } from 'react-router-dom'
import { settingsPath } from '../../components/settingsPath'
import { connectionsOAuthClientEntryId } from '../../components/commandPalette/settingsManual'
import type { McpServer } from '../../types'
import { fmtDate } from '../../i18n/format'
import { Btn } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import ProviderLogo, { PROVIDER_LOGO_SLUGS } from './ProviderLogo'
import type { ConnectionProvider } from './registry'
// The loopback pre-check lives in `utils/loopbackReturnAddress` (shared with
// the chat banner's relay affordance).
import { isValidLoopbackReturnAddress, normalizeLoopbackReturnAddress } from '../../utils/loopbackReturnAddress'
import { isElectron } from '../../lib/electron'
import { useImeGuard } from '../../hooks/useImeGuard'
import {
  errorIndicatesProviderRejection,
  type ConnectionAction,
  type ConnectionCardState,
  type Feedback,
  type OAuthState,
} from './connectionsPageCardState'

const PROVIDER_TONES: Record<string, string> = {
  notion: 'bg-text-strong text-bg',
  github: 'bg-[#24292f] text-white',
  linear: 'bg-[#5e6ad2] text-white',
  atlassian: 'bg-[#1868db] text-white',
  stripe: 'bg-[#635bff] text-white',
  vercel: 'bg-text-strong text-bg',
}

function safeApprovalUrl(value: string): string {
  try {
    const url = new URL(value)
    return url.protocol === 'https:' || url.protocol === 'http:' ? url.toString() : ''
  } catch {
    return ''
  }
}

interface ConnectionCardProps {
  provider: ConnectionProvider
  server?: McpServer
  state: ConnectionCardState
  oauth?: OAuthState
  /** First-authorization timestamp from /api/connections/status. Preferred over
   *  server.connectedSince, which no current runtime populates. */
  connectedSince?: string
  /** Tri-state authorization verdict from /api/connections/status: true = a
   *  grant is on disk, false = CONFIRMED absent, undefined = indeterminate or
   *  not yet loaded. The card needs the raw verdict, not just the folded
   *  `state`, because two substates share `not-verified`: a confirmed absence
   *  can name itself, while an unknowable one must keep the honest hedge. */
  grantPresent?: boolean
  /** Other configured MCP entries backed by the SAME OAuth grant as this
   *  provider, from /api/connections/status. Non-empty only when the grant is
   *  present AND shared. Drives a proactive note so a user learns the connection
   *  is shared (and that a local disconnect/reconnect keeps the token) without
   *  having to attempt a Disconnect first. */
  grantSharedWith?: string[]
  /** True when the shared-grant census could not read every source, so an empty
   *  `grantSharedWith` is "could not confirm sole ownership", not "unshared". */
  grantSharedUnconfirmed?: boolean
  busy?: ConnectionAction
  /** The provider NAME (not slug) of whichever card currently owns the single
   *  in-flight Connections Test, or undefined when none is running. Used only
   *  to disable and explain every OTHER card's Test button -- this card's own
   *  busy==='test' already covers its own button, and a card testing itself
   *  must not disable against its own name. */
  testingProvider?: string
  feedbackSlots: ReadonlyArray<{ slug: string; value: Feedback }>
  highlighted: boolean
  onConnect: () => Promise<unknown>
  onCancel: () => Promise<unknown>
  onDisconnect: () => Promise<unknown>
  onReconnect: () => Promise<unknown>
  onTest: () => Promise<unknown>
  onRelay: (returnAddress: string) => Promise<boolean>
}

/** v1's approved copy: each provider's description leads with what the agent
 *  can DO. The keys are literal (not built at runtime) because a key built at
 *  runtime is invisible to every static tool: the extractor does not find it
 *  and the dead-key scan reports it as referenced nowhere. A provider absent
 *  here falls back to the generic blurb. */
const VALUE_PROP_KEYS = {
  notion: 'pages.connectionsPage.value_prop_notion',
  github: 'pages.connectionsPage.value_prop_github',
  linear: 'pages.connectionsPage.value_prop_linear',
  atlassian: 'pages.connectionsPage.value_prop_atlassian',
  stripe: 'pages.connectionsPage.value_prop_stripe',
  vercel: 'pages.connectionsPage.value_prop_vercel',
  gitlab: 'pages.connectionsPage.value_prop_gitlab',
} as const

/** Localized prerequisite warnings, slug-keyed like VALUE_PROP_KEYS: the
 *  registry's `prerequisite_copy` decides WHETHER a card warns (and is the
 *  English fallback); the catalogs carry what non-English users read. */
const PREREQUISITE_KEYS = {
  gitlab: 'pages.connectionsPage.prerequisite_gitlab',
  atlassian: 'pages.connectionsPage.prerequisite_atlassian',
  github: 'pages.connectionsPage.prerequisite_github',
  asana: 'pages.connectionsPage.prerequisite_asana',
} as const

/**
 * Amber warning icon beside a card's action for anything the user should know
 * BEFORE pressing it: a provider-side prerequisite beside Connect, the
 * one-time OAuth-app setup beside Configure. Hover or focus previews the
 * message as a small bubble; clicking the icon pins the bubble open; clicking
 * anywhere else (or Escape) dismisses it. Modeled on InfoTip: portal-rendered
 * so card overflow cannot clip it, name/description split so the icon's
 * accessible NAME stays a short phrase while the prose rides as its DESCRIPTION.
 *
 * This is the card's ONE surface for a pre-action caveat. A caveat rendered as
 * an always-visible band costs every card in the grid a row of chrome for
 * prose a user reads once, and makes rows ragged; the inline bands that remain
 * on the card report a live verdict about the CURRENT state (not verified,
 * needs attention) or carry a form (the remote return-address relay) -- copy
 * about what an action will do or needs is neither, and goes here.
 */
function PrerequisiteTip({ label, heading, text }: { label: string; heading: string; text: string }) {
  const [pinned, setPinned] = useState(false)
  const [hovered, setHovered] = useState(false)
  const btnRef = useRef<HTMLButtonElement>(null)
  const tipRef = useRef<HTMLDivElement>(null)
  const hoverOff = useRef<number | undefined>(undefined)
  const tipId = useId()
  const open = pinned || hovered

  // WCAG 1.4.13 hoverable: the pointer must be able to travel from the icon
  // into the bubble, so hover-off waits a short grace instead of dismissing
  // the instant the pointer leaves the icon; entering either surface cancels it.
  const holdHover = () => {
    window.clearTimeout(hoverOff.current)
    setHovered(true)
  }
  const releaseHover = () => {
    window.clearTimeout(hoverOff.current)
    hoverOff.current = window.setTimeout(() => setHovered(false), 120)
  }
  useEffect(() => () => window.clearTimeout(hoverOff.current), [])

  useEffect(() => {
    // Gated on `open`, not `pinned`: a tip opened by keyboard focus alone must
    // still dismiss on Escape (WCAG 1.4.13), and an outside click may as well
    // clear a merely-hovered tip too. A press INSIDE the bubble neither passes
    // through (the bubble is pointer-eventful, so a control hidden beneath it —
    // worst case a sibling Connect starting an unchosen OAuth flow — is never
    // the target) nor dismisses, so the steps stay drag-selectable; dismissal
    // is the icon, an outside press, Escape, or scroll.
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (btnRef.current?.contains(e.target as Node)) return
      if (tipRef.current?.contains(e.target as Node)) return
      setPinned(false)
      setHovered(false)
    }
    const onKey = (e: globalThis.KeyboardEvent) => {
      if (e.key === 'Escape') {
        setPinned(false)
        setHovered(false)
      }
    }
    // The bubble is position:fixed and computed once, so scrolling the gallery
    // would detach it from its icon — dismiss instead (capture phase, so any
    // scrolling ancestor counts, not just the window).
    const onScroll = () => {
      setPinned(false)
      setHovered(false)
    }
    document.addEventListener('mousedown', onDown)
    document.addEventListener('keydown', onKey)
    document.addEventListener('scroll', onScroll, true)
    return () => {
      document.removeEventListener('mousedown', onDown)
      document.removeEventListener('keydown', onKey)
      document.removeEventListener('scroll', onScroll, true)
    }
  }, [open])

  // Bottom-anchored above the icon so bubble height never matters; clamped to
  // the viewport the same way InfoTip clamps, falling below only when the icon
  // sits within bubble reach of the top edge.
  const pos = () => {
    if (!btnRef.current) return { top: 0, left: 0 }
    const r = btnRef.current.getBoundingClientRect()
    const tipW = 300
    let left = r.left + r.width / 2 - tipW / 2
    left = Math.max(8, Math.min(left, window.innerWidth - tipW - 8))
    if (r.top > 132) return { bottom: window.innerHeight - r.top + 6, left }
    return { top: r.bottom + 6, left }
  }

  return (
    <>
      <button
        ref={btnRef}
        type="button"
        aria-label={label}
        aria-expanded={open}
        aria-describedby={open ? tipId : undefined}
        onClick={e => { e.stopPropagation(); setPinned(p => !p) }}
        onMouseEnter={holdHover}
        onMouseLeave={releaseHover}
        onFocus={holdHover}
        onBlur={releaseHover}
        className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md text-warn transition-colors hover:text-warn-text hover:bg-warn-subtle"
      >
        <AlertTriangle className="h-4 w-4" aria-hidden="true" />
      </button>
      {open && <OverlayPortal>{
        /* eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions -- the mouse listeners implement WCAG 1.4.13 "hoverable" (pointer may cross into the tooltip without it dismissing); keyboard users have their own complete path on the button (focus opens, Escape dismisses), so there is no keyboard operation to mirror here */
        <div
          ref={tipRef}
          id={tipId}
          role="tooltip"
          onMouseEnter={holdHover}
          onMouseLeave={releaseHover}
          className="fixed z-[9999] max-w-[300px] whitespace-normal rounded-lg border border-warn/30 p-2.5 text-[12px] leading-relaxed text-text"
          style={{ ...pos(), backgroundColor: 'var(--card)', boxShadow: 'var(--shadow-lg)' }}
        >
          <span className="block font-medium text-text-strong">{heading}</span>
          <span className="mt-0.5 block">{text}</span>
        </div>
      }</OverlayPortal>}
    </>
  )
}

function ConnectionCard({
  provider,
  server,
  state,
  oauth,
  connectedSince,
  grantPresent,
  grantSharedWith,
  grantSharedUnconfirmed,
  busy,
  testingProvider,
  feedbackSlots,
  highlighted,
  onConnect,
  onCancel,
  onDisconnect,
  onReconnect,
  onTest,
  onRelay,
}: ConnectionCardProps) {
  const ime = useImeGuard()
  const { t } = useTranslation()
  const [returnAddress, setReturnAddress] = useState('')
  const [invalidReturnAddress, setInvalidReturnAddress] = useState(false)
  const approvalUrl = safeApprovalUrl(oauth?.oauthUrl || '')

  // The approval tab is opened by the CLICK and filled later, which is the only
  // ordering the browser allows. POST /api/connections/mint answers as soon as
  // the mint is SCHEDULED -- the URL does not exist yet and the card polls for it
  // -- so a window.open() that waited for the URL would fire outside the click's
  // user-activation window and be blocked as a popup. That holds on the warm path
  // too: a preminted URL still surfaces on the next poll, not inside the click.
  // So the click opens a blank tab and this ref holds it until there is somewhere
  // to send it.
  const approvalTabRef = useRef<Window | null>(null)
  // Whether the attempt whose URL is arriving was STARTED FROM THIS CARD's buttons.
  // The desktop path's equivalent of `approvalTabRef`'s ownership discipline: the
  // browser path can only fill a tab its own click opened, so a URL this card did
  // not ask for reaches the fallback link and nothing else. The desktop path has
  // no tab to stand in for that ownership, and `approvalUrl` alone does NOT carry
  // it -- a chat `mcp_oauth` banner (external MCP content, no click of ours) folds
  // into the same value through `latestOAuthByServer` -> `effectiveOAuth`, so an
  // ungated hand-off would launch the OS browser at a provider consent page the
  // user never initiated, and again on every return to the page while the banner
  // lives. Only `startMint` sets this, so only Connect / Authorize / Reconnect can
  // auto-open.
  const startedFromThisCardRef = useRef(false)
  // The URL already handed to the desktop shell, so an effect that runs twice for
  // one delivery (StrictMode's double invoke) opens the browser once. State cannot
  // carry this: both invocations run inside the same commit, before any re-render.
  const browserHandoffRef = useRef('')
  // Tri-state, not a boolean, because "no tab" and "a tab the browser refused"
  // must read differently and `oauth.minted` cannot tell them apart: it stays
  // false for the whole poll window, so a boolean gated on it let a blocked-popup
  // user read "finish approving in your browser" about a tab they never got --
  // the exact claim this change exists to stop making.
  //   none     -- this attempt was not started from this card's Connect button
  //   open     -- the click opened a tab and it is waiting for a URL
  //   refused  -- the click asked for a tab and the browser said no
  //   external -- the URL went to the OS default browser (desktop shell only)
  const [clickTab, setClickTab] = useState<'none' | 'open' | 'refused' | 'external'>('none')

  const closeQuietly = (win: Window): boolean => {
    try {
      win.close()
      return true
    } catch {
      return false
    }
  }

  // A granted tab would otherwise sit on a bare about:blank for the whole poll
  // interval, which reads as a misfire on the page's core action. The document is
  // same-origin by construction -- this component just created it -- so it can be
  // written directly, and it is written as TEXT rather than markup so a
  // translated string can never become nodes. No colours are set: `color-scheme`
  // lets the browser pick, so the holding page cannot clash with a light or dark
  // theme the way a hardcoded background would.
  const describeApprovalTab = (tab: Window) => {
    const body = tab.document.body
    if (!body) return
    const label = t('pages.connectionsPage.connecting')
    tab.document.title = label
    body.setAttribute(
      'style',
      'margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;'
      + 'color-scheme:light dark;font:14px system-ui,-apple-system,sans-serif',
    )
    body.textContent = label
  }

  // Takes the action that STARTS a mint, not the Connect button specifically:
  // Authorize and Reconnect run the identical mint-and-poll path through
  // `onReconnect`, so wiring the tab to one button left the other two opening
  // nothing and — because their clicks left `clickTab` at `none` — still reading
  // the "finish approving in your browser" claim about a page that never opened.
  // The tab belongs to the moment a mint attempt starts, whichever button starts
  // it.
  const startMint = async (begin: () => Promise<unknown>) => {
    // Claimed for BOTH hosts before either branch: this is the only place a mint
    // attempt starts from this card, so it is the only honest place to record that
    // a URL arriving later is one we asked for.
    startedFromThisCardRef.current = true
    // The desktop shell asks for no tab. Every window.open there is arbitrated by
    // the main process (electron/external-scheme.js): a blank target cannot be
    // parsed as a URL, so it is denied, the call returns null, and a browser-only
    // reading of that null degrades the card to the fallback link as its ONLY
    // route -- which is what made Connect a dead end in the app. Consent also
    // belongs in the user's real browser, where their provider sessions live, so
    // this path waits for the URL and hands it to the OS below.
    if (!isElectron) {
      let tab: Window | null = null
      try {
        // `noopener` is deliberately NOT passed: it makes window.open return null,
        // and the handle IS the feature here. The reverse-tabnabbing reference it
        // would have removed is severed on the next line instead, while the tab is
        // still the same-origin blank document this call just created.
        tab = window.open('', '_blank')
        if (tab) tab.opener = null
      } catch {
        // A browser that refuses the tab outright is the same case as a blocked
        // popup, and the fallback link below is the way in.
        tab = null
      }
      if (tab) describeApprovalTab(tab)
      approvalTabRef.current = tab
      setClickTab(tab ? 'open' : 'refused')
    }
    // No reclaim branch here on purpose: a rejected mint ends the attempt without
    // a URL, which the invariant effect below already recognises. One reclaimer
    // rather than one per dead end.
    await begin()
  }

  // Hand the URL to the tab the click opened. Declared BEFORE the reclaim effect
  // so that a render which both delivers a URL and moves the card runs delivery
  // first and leaves the reclaimer nothing to find. Keyed on the URL alone
  // because it arrives once per attempt; a tab the user closed meanwhile is
  // dropped rather than reopened, since re-opening a window someone deliberately
  // closed is not ours to do -- the link below remains the way back.
  useEffect(() => {
    if (!approvalUrl) return
    if (isElectron) {
      // Nothing to open for a URL this card did not ask for. The browser path
      // expresses the same rule one line below by finding no tab of its own and
      // leaving the link as the way in ("a tab the user closed meanwhile is
      // dropped rather than reopened"); a banner-delivered URL gets exactly that
      // treatment here, and `clickTab` stays `none` so the heading claims nothing.
      if (!startedFromThisCardRef.current) return
      // Handed to the shell at ARRIVAL, not at the click. Electron's
      // setWindowOpenHandler is main-process arbitration rather than a popup
      // blocker, so it carries no user-activation requirement -- the constraint
      // that forces the browser path to pre-open a blank tab does not exist here.
      // A cross-origin https target classifies as `external`: the main process
      // calls shell.openExternal and DENIES the in-app window, so the null return
      // is this path's success shape and must never be read as a refusal. The
      // hand-off can still fail inside the OS, silently by design, which is why
      // the link below renders whenever a URL exists.
      if (browserHandoffRef.current === approvalUrl) return
      browserHandoffRef.current = approvalUrl
      try {
        window.open(approvalUrl, '_blank', 'noopener,noreferrer')
      } catch {
        // Nothing to recover: no handle was wanted and the link holds the URL.
      }
      setClickTab('external')
      return
    }
    const tab = approvalTabRef.current
    if (!tab) return
    approvalTabRef.current = null
    if (tab.closed) {
      setClickTab('refused')
      return
    }
    try {
      tab.location.href = approvalUrl
    } catch {
      // The tab is no longer ours to drive. Recoverable rather than fatal: the
      // link holds the same URL, and the heading must stop claiming a usable
      // browser page, so this lands in the same state as a refused tab.
      setClickTab('refused')
    }
  }, [approvalUrl])

  // One invariant instead of a patch per dead end. While a tab is held, the only
  // situations in which a URL can still arrive are the in-flight click itself and
  // the waiting state; ANY other resting place -- Cancel, a mint that reported
  // failed or expired, a mint request that was rejected outright -- means no URL
  // is coming, so the blank tab is taken back instead of left for the user to
  // close. Reclaiming also clears the ref, which is the half that matters beyond
  // tidiness: a stale ref meant the next Connect click overwrote it and orphaned
  // the first tab permanently.
  //
  // The clickTab reset sits ABOVE the ref guard deliberately. Delivery clears the
  // ref, so guarding on it first left `clickTab` at `open` forever after a
  // SUCCESSFUL connect -- and a later minted flow on the same card then re-made
  // the exact claim this predicate exists to prevent, about a tab navigated away
  // long before. A click's outcome must not outlive its attempt.
  useEffect(() => {
    if (busy === 'connect') return
    if (state === 'waiting-for-approval') return
    setClickTab('none')
    // Released with the attempt, exactly like the tri-state above: ownership must
    // not outlive the attempt that earned it (a later banner would inherit it), and
    // a later click hands its URL over again even when the mint reproduces the same
    // URL.
    startedFromThisCardRef.current = false
    browserHandoffRef.current = ''
    const orphan = approvalTabRef.current
    if (!orphan) return
    approvalTabRef.current = null
    if (!orphan.closed) closeQuietly(orphan)
  }, [busy, state])

  // Unmounting mid-flight (a search filter, a tab switch) would otherwise leave a
  // blank tab nothing can ever fill, because the component that would deliver the
  // URL is gone. Safe to close unconditionally here: the ref is non-null ONLY
  // while the tab is still blank -- delivery clears it -- so this can never close
  // a consent page the user is working in.
  useEffect(() => () => {
    const held = approvalTabRef.current
    approvalTabRef.current = null
    if (held && !held.closed) closeQuietly(held)
  }, [])
  // Asked of the module rather than of the element. `<ProviderLogo …/>` is always
  // a truthy JSX element even for a slug it ships no mark for -- the component
  // returns null when RENDERED, which `??` below cannot observe -- so building the
  // element unconditionally made the lettered fallback unreachable and any
  // provider without art rendered an empty gap where its mark should be. GitLab
  // was exactly that: the art inventory covers GitHub, which the launch set holds
  // back, and not GitLab, which it includes.
  const logo = PROVIDER_LOGO_SLUGS.includes(provider.slug)
    ? <ProviderLogo slug={provider.slug} />
    : null
  // `official_mcp_server` used to be a subtitle line under the name; the brand
  // mark now carries provenance visually, so keep the assurance as the card's
  // accessible/hover description instead of a third row of chrome.
  const provenance = t('pages.connectionsPage.official_mcp_server')
  const valueProp = provider.slug in VALUE_PROP_KEYS
    ? t(VALUE_PROP_KEYS[provider.slug as keyof typeof VALUE_PROP_KEYS])
    : t('pages.connectionsPage.service_value_prop', { provider: provider.name })
  // One shared element so EVERY consent-initiating action (Connect, Authorize,
  // Reconnect) carries the provider-side prerequisite — a GitLab whose Duo is
  // off fails identically whichever button started the OAuth flow.
  const prerequisiteTip = provider.prerequisite_copy ? (
    <PrerequisiteTip
      label={t('pages.connectionsPage.prerequisites_for_provider', { provider: provider.name })}
      heading={t('pages.connectionsPage.before_you_connect')}
      text={
        provider.slug in PREREQUISITE_KEYS
          ? t(PREREQUISITE_KEYS[provider.slug as keyof typeof PREREQUISITE_KEYS], {
              defaultValue: provider.prerequisite_copy,
            })
          : provider.prerequisite_copy
      }
    />
  ) : null
  const stateMeta: Record<ConnectionCardState, { label: string; icon: ReactNode; tone: string }> = {
    'not-connected': {
      label: t('pages.connectionsPage.not_connected'),
      icon: <Link2 className="w-3.5 h-3.5" aria-hidden="true" />,
      tone: 'bg-bg-hover text-muted',
    },
    'waiting-for-approval': {
      label: t('pages.connectionsPage.waiting_for_approval'),
      icon: <CircleDashed className="w-3.5 h-3.5 animate-spin motion-reduce:animate-none" aria-hidden="true" />,
      tone: 'bg-warn-subtle text-warn-text',
    },
    connected: {
      label: t('pages.connectionsPage.connected'),
      icon: <CheckCircle2 className="w-3.5 h-3.5" aria-hidden="true" />,
      tone: 'bg-ok-subtle text-ok-text',
    },
    // Warn tone, not the error tone: an unverifiable state is not a failure. The
    // icon is static on purpose — a spinner would claim a grant is in flight
    // when nothing is pending.
    'not-verified': {
      label: t('pages.connectionsPage.not_verified'),
      icon: <KeyRound className="w-3.5 h-3.5" aria-hidden="true" />,
      tone: 'bg-warn-subtle text-warn-text',
    },
    'needs-attention': {
      label: t('pages.connectionsPage.needs_attention'),
      icon: <AlertTriangle className="w-3.5 h-3.5" aria-hidden="true" />,
      tone: 'bg-danger-subtle text-danger-text',
    },
    // Muted, not warn: nothing is wrong with the provider, a step is missing on
    // OUR side. The lock says "this needs a key" without alarming a user who
    // cannot act on it (only the owner can configure).
    'needs-configuration': {
      label: t('pages.connectionsPage.needs_configuration'),
      icon: <Lock className="w-3.5 h-3.5" aria-hidden="true" />,
      tone: 'bg-bg-hover text-muted',
    },
  }
  const meta = stateMeta[state]
  const runRelay = async () => {
    // Normalize a scheme-less mobile paste (#7406) and submit the normalized
    // form, mirroring the chat banner's relay affordance.
    const normalized = normalizeLoopbackReturnAddress(returnAddress)
    if (!isValidLoopbackReturnAddress(normalized)) {
      setInvalidReturnAddress(true)
      return
    }
    setInvalidReturnAddress(false)
    const delivered = await onRelay(normalized)
    if (delivered) setReturnAddress('')
  }

  return (
    <article
      id={`connection-${provider.slug}`}
      data-state={state}
      className={`relative flex flex-col rounded-lg border bg-card p-3.5 shadow-sm transition-colors ${
        highlighted ? 'border-accent ring-1 ring-accent/40' : state === 'needs-attention' ? 'border-danger/40' : 'border-border'
      }`}
    >
      <header className="flex items-center gap-2.5">
        <span className="flex shrink-0 items-center" title={provenance} aria-label={provenance} role="img">
          {logo ?? (
            <span
              className={`flex h-5 w-5 items-center justify-center rounded text-[11px] font-bold ${PROVIDER_TONES[provider.slug] || 'bg-accent text-accent-fg'}`}
              aria-hidden="true"
            >
              {provider.name.slice(0, 1)}
            </span>
          )}
        </span>
        <h3 className="m-0 min-w-0 flex-1 truncate text-[14px] font-semibold text-text-strong">{provider.name}</h3>
        <span className={`inline-flex shrink-0 items-center gap-1 text-[11px] font-medium ${meta.tone}`}>
          {meta.icon}
          {meta.label}
        </span>
      </header>

      {/* A two-line FLOOR with a three-line ceiling, not a fixed height. The
          value props differ in length -- GitLab's wraps to two lines where
          Notion's takes one -- and text that sizes to its own copy makes each card
          as tall as its description, so a row renders ragged. Reserving two lines
          fixes that for every English value prop (the longest, GitLab's, is 83
          chars and takes exactly two).

          The ceiling is three rather than two because pinning it at two would
          CLIP, and the clipped tail is the consequential part: GitLab's copy ends
          in "this grant can also write", and translations run materially longer
          (Italian 97 chars, Russian Notion 78 vs 58) -- so at the 3-column width a
          verbose locale could hide a write-scope disclosure behind a hover-only
          `title` that touch and keyboard users cannot reach. Growing that one card
          by a line is the cheaper failure. Same trio as the agents gallery card,
          which pins an exact height because no permission scope rides in its
          text. */}
      <p
        className="mb-2.5 mt-1.5 line-clamp-3 min-h-[34px] min-w-0 text-[12.5px] leading-[17px] text-muted"
        title={valueProp}
      >
        {valueProp}
      </p>

      <div className="mt-auto">
        {state === 'not-connected' && (
          <div className="flex items-center justify-between gap-3">
            <a href={provider.docs_url} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-[12px] text-muted hover:text-text">
              {t('pages.connectionsPage.documentation')} <ExternalLink className="w-3 h-3" aria-hidden="true" />
            </a>
            <div className="flex items-center gap-2">
              {prerequisiteTip}
              <Btn primary onClick={() => void startMint(onConnect)} disabled={!!busy}>
                {busy === 'connect' && <Loader2 className="w-3.5 h-3.5 animate-spin" aria-hidden="true" />}
                {busy === 'connect' ? t('pages.connectionsPage.connecting') : t('pages.connectionsPage.connect')}
              </Btn>
            </div>
          </div>
        )}

        {state === 'waiting-for-approval' && (
          <div className="space-y-3">
            <div className="text-[13px] font-medium text-text-strong">
              {/* Connect opens the approval tab itself, so "finish approving in
                  your browser" can name a window that exists -- but only when the
                  browser granted one. `oauth.minted` cannot carry this: it stays
                  false for the whole poll window, so gating on it told a
                  blocked-popup user to finish in a browser page they never got.
                  The click's own outcome decides instead, and a flow this card did
                  not start (`none`) keeps the original rule.

                  The desktop shell has no tab of its own to name: it hands the URL
                  to the OS default browser when it arrives (`external`), and until
                  then nothing is open anywhere, so the neutral heading is the only
                  true one -- the browser-tab claim would be about a window the app
                  never opens. */}
              {isElectron
                ? t(clickTab === 'external'
                  ? 'pages.connectionsPage.approval_opened_in_default_browser'
                  : 'pages.connectionsPage.waiting_for_approval')
                : t(clickTab === 'open' || (clickTab === 'none' && !oauth?.minted)
                  ? 'pages.connectionsPage.finish_approving_in_browser'
                  : 'pages.connectionsPage.waiting_for_approval')}
            </div>
            <div className="flex flex-wrap items-center gap-2">
              {approvalUrl ? (
                <a href={approvalUrl} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-[12px] font-medium text-accent hover:text-accent-hover">
                  {t('pages.connectionsPage.reopen_approval')} <ExternalLink className="w-3 h-3" aria-hidden="true" />
                </a>
              ) : (
                <span className="inline-flex items-center gap-1 text-[12px] text-muted" aria-live="polite">
                  <Loader2 className="w-3 h-3 animate-spin motion-reduce:animate-none" aria-hidden="true" />
                  {t('pages.connectionsPage.waiting_for_approval_address')}
                </span>
              )}
              <Btn className="ml-auto" onClick={() => void onCancel()} disabled={!!busy}>
                <X className="w-3.5 h-3.5" aria-hidden="true" /> {t('pages.connectionsPage.cancel')}
              </Btn>
            </div>
            <div className="rounded-md border border-warn/30 bg-warn-subtle p-2.5">
              <p className="m-0 text-[11px] leading-relaxed text-text">
                {t('pages.connectionsPage.remote_gateway_help')}
              </p>
              <div className="mt-2 block text-[11px] font-medium text-text">
                {t('pages.connectionsPage.return_address')}
              </div>
              <div className="mt-1 flex gap-1.5">
                <input
                  id={`return-address-${provider.slug}`}
                  type="url"
                  aria-label={t('pages.connectionsPage.return_address')}
                  value={returnAddress}
                  onChange={event => {
                    setReturnAddress(event.target.value)
                    if (invalidReturnAddress) setInvalidReturnAddress(false)
                  }}
                  {...ime.bindEnter({ onEnter: () => void runRelay() })}
                  placeholder={t('pages.connectionsPage.return_address_placeholder')}
                  autoComplete="off"
                  spellCheck={false}
                  disabled={busy === 'relay'}
                  aria-invalid={invalidReturnAddress}
                  aria-describedby={invalidReturnAddress ? `return-address-error-${provider.slug}` : undefined}
                  className="min-w-0 flex-1 rounded-md border border-border bg-bg px-2.5 py-1.5 font-mono text-[11px] text-text focus-ring-accent"
                />
                <Btn primary onClick={() => void runRelay()} disabled={!returnAddress.trim() || busy === 'relay'}>
                  {busy === 'relay' && <Loader2 className="w-3.5 h-3.5 animate-spin" aria-hidden="true" />}
                  {busy === 'relay' ? t('pages.connectionsPage.relaying') : t('pages.connectionsPage.complete_connection')}
                </Btn>
              </div>
              {invalidReturnAddress && (
                <p id={`return-address-error-${provider.slug}`} role="alert" className="mb-0 mt-1.5 text-[11px] text-danger">
                  {t('pages.connectionsPage.invalid_return_address')}
                </p>
              )}
            </div>
          </div>
        )}

        {state === 'needs-configuration' && (
          <div className="flex items-center justify-between gap-3">
            <a href={provider.docs_url} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 text-[12px] text-muted hover:text-text">
              {t('pages.connectionsPage.documentation')} <ExternalLink className="w-3 h-3" aria-hidden="true" />
            </a>
            <div className="flex items-center gap-2">
              {/* The one-time-setup explanation is a caveat about the action
                  beside it, so it rides in the same warning tip every other
                  card state uses for its pre-action caveat -- never as a band
                  above the action row. The badge already says what state the
                  card is in; the tip says what pressing the button entails. */}
              <PrerequisiteTip
                label={t('pages.connectionsPage.prerequisites_for_provider', { provider: provider.name })}
                heading={t('pages.connectionsPage.before_you_connect')}
                text={t('pages.connectionsPage.needs_configuration_help', { provider: provider.name })}
              />
              {/* A route, not a Connect button: the missing step lives on the
                  Settings tab, and only the owner can complete it. The highlight
                  lands on this provider's card there. */}
              <Link
                to={settingsPath({ tab: 'connections', highlight: connectionsOAuthClientEntryId(provider.slug) })}
                className="inline-flex items-center gap-1.5 rounded-md bg-accent px-2.5 py-1.5 text-[12px] font-medium text-accent-fg hover:opacity-90"
              >
                <Settings2 className="w-3.5 h-3.5" aria-hidden="true" />
                {t('pages.connectionsPage.configure_oauth_app')}
              </Link>
            </div>
          </div>
        )}

        {state === 'not-verified' && (
          <div className="space-y-3">
            <div className="flex items-start gap-2 rounded-md border border-warn/30 bg-warn-subtle p-2.5 text-[12px] text-text">
              <KeyRound className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warn" aria-hidden="true" />
              {/* Two substates share this card. `grantPresent === false` is a
                  CONFIRMED verdict (the status feed stat'd kiro-cli's grant
                  artifacts and found none), so the copy names the held fact
                  instead of hedging "cannot see the authorization" — the hedge
                  is only honest while the verdict is indeterminate. */}
              <span>
                {grantPresent === false
                  ? t('pages.connectionsPage.not_authorized_help', { provider: provider.name })
                  : t('pages.connectionsPage.not_verified_help', { provider: provider.name })}
              </span>
            </div>
            <div className="flex items-center justify-end gap-2">
              {prerequisiteTip}
              <Btn primary onClick={() => void startMint(onReconnect)} disabled={!!busy}>
                {busy === 'connect' ? <Loader2 className="w-3.5 h-3.5 animate-spin" aria-hidden="true" /> : <KeyRound className="w-3.5 h-3.5" aria-hidden="true" />}
                {busy === 'connect' ? t('pages.connectionsPage.connecting') : t('pages.connectionsPage.connect')}
              </Btn>
            </div>
          </div>
        )}

        {state === 'connected' && (
          <div className="space-y-3">
            {(connectedSince || server?.connectedSince) && (
              <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 text-[12px]">
                <dt className="text-muted">{t('pages.connectionsPage.connected_since')}</dt>
                <dd className="m-0 text-text">{fmtDate((connectedSince || server?.connectedSince) as string)}</dd>
              </dl>
            )}
            {/* The shared-grant state (shared with whom, or could-not-confirm)
                and its disconnect/reconnect caveat BOTH ride in the single
                action-row PrerequisiteTip below, never an always-visible band:
                the connection-card-caveat-tip rule keeps one triangle per action
                and the card the same height in every state. */}
            {/* Test and Disconnect are visually separated groups (never three
                peer buttons): the shared-grant caveat tip sits in the Disconnect
                group, immediately before Disconnect, because it qualifies that
                action -- the connection-card-caveat-tip convention, reusing the
                shared PrerequisiteTip heading and label. */}
            <div className="flex items-center justify-between gap-2">
              <Btn
                onClick={() => void onTest()}
                disabled={!!busy || !!testingProvider}
                title={testingProvider ? t('pages.connectionsPage.test_blocked_by_sibling', { provider: testingProvider }) : undefined}
                aria-label={testingProvider ? t('pages.connectionsPage.test_blocked_by_sibling', { provider: testingProvider }) : undefined}
              >
                {busy === 'test' ? <Loader2 className="w-3.5 h-3.5 animate-spin" aria-hidden="true" /> : <RotateCw className="w-3.5 h-3.5" aria-hidden="true" />}
                {busy === 'test' ? t('pages.connectionsPage.testing') : t('pages.connectionsPage.test_connection')}
              </Btn>
              <div className="flex items-center gap-2">
                {(grantSharedWith && grantSharedWith.length > 0) || grantSharedUnconfirmed ? (
                  <PrerequisiteTip
                    label={t('pages.connectionsPage.prerequisites_for_provider', { provider: provider.name })}
                    heading={t('pages.connectionsPage.before_you_connect')}
                    text={`${
                      grantSharedWith && grantSharedWith.length > 0
                        ? t('pages.connectionsPage.grant_shared_fact', { names: grantSharedWith.join(', ') })
                        : t('pages.connectionsPage.grant_shared_fact_unconfirmed')
                    } ${t('pages.connectionsPage.grant_shared_caveat')}`}
                  />
                ) : null}
                <Btn danger onClick={() => void onDisconnect()} disabled={!!busy}>
                  <Unplug className="w-3.5 h-3.5" aria-hidden="true" /> {t('pages.connectionsPage.disconnect')}
                </Btn>
              </div>
            </div>
          </div>
        )}

        {state === 'needs-attention' && (
          <div className="space-y-3">
            {/* The headline is copy selected by evidence (verdict vs could-not-reach);
                the raw detail stays the ErrorNotice `message` so the journal lookup
                key keeps its structured context. Detail absent: the headline itself
                is the message so the notice still renders. askAgent is ON: a status
                card holds no unsaved draft, so the hand-off can destroy nothing. */}
            <ErrorNotice
              title={(oauth?.error || server?.error) ? t(
                errorIndicatesProviderRejection(oauth?.error || server?.error)
                  ? 'pages.connectionsPage.connection_invalid'
                  : 'pages.connectionsPage.connection_unreachable',
                { provider: provider.name },
              ) : undefined}
              message={oauth?.error || server?.error || t(
                'pages.connectionsPage.connection_unreachable',
                { provider: provider.name },
              )}
              askAgent
            />
            <div className="flex items-center justify-end gap-2">
              {prerequisiteTip}
              <Btn primary onClick={() => void startMint(onReconnect)} disabled={!!busy}>
                {busy === 'connect' ? <Loader2 className="w-3.5 h-3.5 animate-spin" aria-hidden="true" /> : <RotateCw className="w-3.5 h-3.5" aria-hidden="true" />}
                {busy === 'connect' ? t('pages.connectionsPage.reconnecting') : t('pages.connectionsPage.reconnect')}
              </Btn>
            </div>
          </div>
        )}
      </div>

      {feedbackSlots.length > 0 && (
        <div data-slot="connection-feedback" className="mt-3 grid text-[11px]">
          {feedbackSlots.map(slot => {
            const visible = slot.slug === provider.slug
            if (!visible) {
              const revokeLabel = slot.value.revoke
                ? t('pages.connectionsPage.revoke_at_provider', { provider: slot.value.revoke.provider })
                : ''
              const helpLabel = slot.value.help
                ? t('pages.connectionsPage.documentation')
                : ''
              return (
                <div
                  key={slot.slug}
                  aria-hidden="true"
                  data-placeholder={[slot.value.text, slot.value.detail, revokeLabel, helpLabel].filter(Boolean).join(' ')}
                  className="invisible col-start-1 row-start-1 before:content-[attr(data-placeholder)]"
                />
              )
            }
            const isError = slot.value.kind === 'error'
            return (
              <div
                key={slot.slug}
                // An `error` renders through `ErrorNotice`, which supplies its OWN
                // role="alert" -- a second one here would announce twice and make
                // a by-role lookup ambiguous. Non-error kinds keep this wrapper's
                // role, because they have no shared surface of their own.
                role={isError ? undefined : (slot.value.kind === 'success' ? 'status' : 'alert')}
                className={`col-start-1 row-start-1 ${
                  slot.value.kind === 'error'
                    ? 'text-danger'
                    : slot.value.kind === 'warning'
                      ? 'text-warn'
                      : 'text-ok'
                }`}
              >
                {isError ? (
                  /* No hand-off: the waiting card renders the return-address
                     paste-back input, whose typed value lives only in card-local
                     state and is cleared ONLY on a delivered relay -- so after a
                     FAILED relay the address the user must retry with is still
                     sitting in that input, and the hand-off navigates to the chat
                     and unmounts this gallery, discarding it. That covers the
                     generic action failure; the single-flight refusal separately
                     has no diagnosis to hand over, being self-describing and
                     self-correcting (wait for the running test, click again). */
                  <ErrorNotice variant="inline" message={slot.value.text} report={slot.value.report} />
                ) : slot.value.text}
                {slot.value.detail && (
                  <>
                    {' '}
                    <span className="mt-1 block">{slot.value.detail}</span>
                  </>
                )}
                {slot.value.revoke && (
                  <>
                    {' '}
                    <a href={slot.value.revoke.href} target="_blank" rel="noopener noreferrer" className="font-medium text-accent hover:text-accent-hover">
                      {t('pages.connectionsPage.revoke_at_provider', { provider: slot.value.revoke.provider })} <ExternalLink className="lucide-inline" aria-hidden="true" />
                    </a>
                  </>
                )}
                {slot.value.help && (
                  <>
                    {' '}
                    <a href={slot.value.help.href} target="_blank" rel="noopener noreferrer" className="font-medium text-accent hover:text-accent-hover">
                      {t('pages.connectionsPage.documentation')} <ExternalLink className="lucide-inline" aria-hidden="true" />
                    </a>
                  </>
                )}
              </div>
            )
          })}
        </div>
      )}
    </article>
  )
}

// `ConnectionsPage.tsx` renders one per provider.
export { ConnectionCard }
