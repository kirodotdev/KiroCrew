import { useEffect, useId, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import {
  AlertTriangle,
  CheckCircle2,
  CircleDashed,
  ExternalLink,
  KeyRound,
  Link2,
  Loader2,
  RotateCw,
  Server,
  Unplug,
  X,
} from 'lucide-react'
import { api, ApiError, type ConnectionMintState, type ConnectionStatus } from '../../api/client'
import { useAppSelector } from '../../store'
import type { ChatMessage, McpApplyChange, McpServer } from '../../types'
import { fmtDate } from '../../i18n/format'
import { Badge, Btn, ContentSkeleton, SearchInput } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import { findReport, type ErrorReport } from '../../utils/errorReport'
import McpTab from '../overview/McpTab'
import ProviderLogo, { PROVIDER_LOGO_SLUGS } from './ProviderLogo'
import {
  CONNECTION_PROVIDERS,
  serverForConnection,
  type ConnectionProvider,
} from './registry'

/** Mint poll cadence. A cold mint takes seconds, so this is tuned to surface the
 *  URL promptly without spinning on a request that mostly answers `minting`. */
const MINT_POLL_MS = 2_000

/** Authorization-status poll cadence. A grant changes rarely and the read is a
 *  local stat, so this is slow relative to the mint poll — it only has to notice
 *  a grant completed outside the dashboard and keep connected-since fresh. */
const CONNECTION_STATUS_POLL_MS = 30_000

export type ConnectionCardState =
  | 'not-connected'
  | 'waiting-for-approval'
  | 'connected'
  | 'not-verified'
  | 'needs-attention'

type ConnectionAction = 'connect' | 'disconnect' | 'relay' | 'test'
export type Feedback = {
  // THREE kinds, because "the click did not do what you asked" splits in two. An
  // `error` is a failure; a `warning` is a deliberate refusal that leaves the user
  // a repair to make. Both must ANNOUNCE (role=alert) -- only `success` is a
  // passing status update.
  kind: 'success' | 'warning' | 'error'
  text: string
  /** Localized supplemental guidance appended after `text` (e.g. a provider's
   *  prerequisite steps on a zero-tools verdict). */
  detail?: string
  /**
   * Structured context for an `error` feedback, when the journal holds it.
   *
   * Enrichment ONLY -- it never decides which surface renders: every `error`
   * kind routes through `ErrorNotice` because `kind` alone says the request
   * failed, and `ErrorNotice` degrades to its own message-keyed lookup when no
   * report is found. Gating the routing on a report instead would let a lookup
   * miss (a mocked client, a redaction difference, journal eviction) silently
   * fall back to a hand-written error line, which is the defect the shared
   * surface exists to prevent -- and it would fail on the least-exercised path.
   *
   * Carried explicitly rather than derived from `text`, because `text` is a
   * LOCALIZED string while the journal is keyed on the message the API layer
   * produced, so a lookup by the rendered text misses in every locale.
   */
  report?: ErrorReport
  revoke?: { href: string; provider: string }
  help?: { href: string }
}
export type OAuthState = {
  completed: boolean
  failed: boolean
  oauthUrl: string
  error: string
  timestamp: number
  /** The URL was minted on demand, so no browser tab was ever opened for it. */
  minted?: boolean
}

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

// The loopback pre-check lives in `utils/loopbackReturnAddress` (shared with
// the chat banner's relay affordance).
import { isValidLoopbackReturnAddress, normalizeLoopbackReturnAddress } from '../../utils/loopbackReturnAddress'
import { useImeGuard } from '../../hooks/useImeGuard'

export interface PendingConnect {
  kind: 'new' | 'reconnect'
  /** Timestamp of the newest `mcp_oauth` banner observed for this server at
   *  click time (0 when none). Banner timestamps are gateway-generated, so
   *  fencing against a *snapshot of them* stays within one clock domain —
   *  never compare them to the browser's own wall clock. */
  sinceTs: number
  /** The row token this tab's own POST returned, when it returned one. The mint
   *  table is keyed by slug, so a sibling tab connecting the same provider
   *  REPLACES the row -- without this, a tab reads the sibling's terminal state as
   *  the verdict on its own attempt and clears a wait it should still be holding. */
  token?: string
}

/** A banner no newer than the snapshot taken at click time belongs to a prior
 *  grant of the same server name — it must never mark a fresh attempt
 *  connected/failed. */
export function effectiveOAuth(
  oauth: OAuthState | undefined,
  pending: PendingConnect | undefined,
): OAuthState | undefined {
  if (oauth && pending && oauth.timestamp <= pending.sinceTs) return undefined
  return oauth
}

/** Fold a minted approval URL into the card's OAuth view.
 *
 * Applied AFTER `effectiveOAuth`, so a minted URL never passes through the
 * banner staleness fence: a mint is started by the click being served, so it is
 * current by construction and carries no gateway banner timestamp to compare
 * against. A URL is taken only from a `waiting` mint — every other state either
 * has no URL or holds one that can no longer be redeemed.
 *
 * A chat banner that already carries a URL wins: it is the same consent request,
 * and preferring one source keeps the rendered link stable across polls.
 */
/** What a mint state means for the card, given how the entry got there.
 *
 *  The full table — every mint state against both entry situations — so the card
 *  implements a decision rather than accumulating one branch per review round:
 *
 *  | mint state | entry           | wait  | probe | error | uninstall |
 *  |------------|-----------------|-------|-------|-------|-----------|
 *  | absent     | either          | keep  |  no   |  no   |    no     |
 *  | minting    | either          | keep  |  no   |  no   |    no     |
 *  | waiting    | either          | keep  |  no   |  no   |    no     |
 *  | granted    | either          | clear | YES   |  no   |    no     |
 *  | failed     | new-this-flow   | clear |  no   | YES   |    no     |
 *  | failed     | pre-existing    | clear |  no   | YES   |    no     |
 *  | expired    | any             | clear |  no   |  no   |
 *
 *  No terminal state deletes configuration. An expired mint clears this tab's
 *  wait and leaves the entry in place, so the card shows needs-attention and the
 *  user retries with Connect or removes it with Disconnect. Deleting an entry on
 *  a timeout meant racing a sibling tab for the same slug-keyed row, and no
 *  amount of token fencing makes an automatic delete worth that: config removal
 *  is a decision the user makes explicitly.
 *
 *  Two rows carry the reasoning:
 *  - `granted` must PROBE. The card's cached status predates consent, so without
 *    a fresh read it keeps showing the pre-consent error after authorization
 *    succeeded.
 *  - `failed` keeps the entry on purpose. Something went wrong rather than timed
 *    out, so the error surface plus a retryable entry beats silently undoing the
 *    install.
 */
export type MintOutcome = {
  clearWait: boolean
  probe: boolean
  error: boolean
}

const MINT_WAIT_HELD: MintOutcome = {
  clearWait: false, probe: false, error: false,
}


/** Whether a row is the one THIS tab's POST started. Unknown on either side reads
 *  as ours: a row with no token predates the fence, and a pending wait with no
 *  token means the POST answered without one -- neither is a sibling's. */
function mintRowIsOurs(
  mint: ConnectionMintState | undefined,
  pending: PendingConnect | undefined,
): boolean {
  if (!mint?.token || !pending?.token) return true
  return mint.token === pending.token
}

export function mintOutcome(
  mint: ConnectionMintState | undefined,
  pending?: PendingConnect,
): MintOutcome {
  // A row carrying a DIFFERENT token is a sibling tab's, not this tab's. Clear the
  // wait -- the mint table is keyed by slug, so this tab's row was REPLACED and no
  // verdict for its own attempt is ever coming, and holding would spin forever --
  // but claim nothing from the sibling's outcome: no probe, no error. This is the
  // client half of the fence the backend applies; neither is sufficient alone,
  // because the client cannot see a supersede that lands after it reads, and the
  // server cannot see which tab is asking.
  if (!mintRowIsOurs(mint, pending)) return { clearWait: true, probe: false, error: false }
  switch (mint?.state) {
    case 'granted':
      return { clearWait: true, probe: true, error: false }
    case 'failed':
      return { clearWait: true, probe: false, error: true }
    case 'expired':
      return { clearWait: true, probe: false, error: false }
    default:
      return MINT_WAIT_HELD
  }
}


export function withMintedUrl(
  oauth: OAuthState | undefined,
  mint: ConnectionMintState | undefined,
): OAuthState | undefined {
  const minted = mint?.state === 'waiting' ? (mint.oauth_url || '') : ''
  if (!minted || oauth?.oauthUrl) return oauth
  return {
    completed: false,
    failed: false,
    error: '',
    timestamp: 0,
    ...(oauth ?? {}),
    oauthUrl: minted,
    minted: true,
  }
}

/** Only a cancelled *new* connect uninstalls the entry it just created;
 *  cancelling a reconnect (or a stateless wait) must not destroy config. */
export function uninstallOnCancel(pending: PendingConnect | undefined): boolean {
  return pending?.kind === 'new'
}

export function disconnectFeedback(
  provider: Pick<ConnectionProvider, 'name' | 'revoke_page_url'>,
  text: string,
  kind: Feedback['kind'] = 'success',
): Feedback {
  return {
    kind,
    text,
    revoke: { href: provider.revoke_page_url, provider: provider.name },
  }
}

/**
 * The ONE reading of a probe status against the authorization axis. Both the
 * card's badge and the Test button's verdict fold through this, because they
 * judge the same probe and a second reading is how they came to disagree:
 * a connected Linear card rendered Connected while its Test click reported a
 * failure, from `status !== 'ok'` on the exact answer the badge folds as healthy.
 *
 * Exported for test.
 */
export function probeIndicatesConnected(status: string, grantPresent?: boolean): boolean {
  // `ok` is REACHABILITY and it is cached, so it outlives a revoked grant. Only
  // a CONFIRMED absent grant (never the indeterminate or not-yet-loaded
  // undefined) is a fresher fact than it.
  if (status === 'ok') return grantPresent !== false
  // A tokenless probe of a remote OAuth server answers 401, which the gateway
  // reports as `needs_auth` — kiro-cli owns token custody, so needs_auth beside
  // a grant IS the healthy shape. The grant axis is the only thing separating
  // "authorized outside this app" from "nobody authorized this", so an absent
  // OR indeterminate verdict is not a grant.
  if (status === 'needs_auth') return grantPresent === true
  return false
}

/**
 * The confirmed-only grant verdict, read once and shared. An indeterminate
 * lookup reports `grantPresent: false` without knowing anything, so it must
 * collapse to `undefined` (the honest hedge) rather than to a confirmed absence.
 *
 * Exported for test.
 */
export function confirmedGrantPresent(status: ConnectionStatus | undefined): boolean | undefined {
  return status && !status.grantIndeterminate ? status.grantPresent : undefined
}

export function connectionStateFor(
  server: McpServer | undefined,
  oauth: OAuthState | undefined,
  locallyWaiting = false,
  grantPresent?: boolean,
  awaitingConsent = false,
): ConnectionCardState {
  if (!server) {
    // `awaitingConsent` is the backend's mint table saying a flow for this
    // provider is in flight RIGHT NOW. It is what survives a refresh: the
    // locally-pending map and the chat's oauth message are both per-tab state,
    // so without it a reload mid-consent silently drops back to Connect while
    // the approval URL is still live.
    return locallyWaiting || awaitingConsent ? 'waiting-for-approval' : 'not-connected'
  }
  if (oauth?.failed) return 'needs-attention'
  // A completed OAuth flow in THIS session outranks a possibly-lagging status
  // poll: the grant was just written, the feed may not have re-read yet.
  if (oauth?.completed) return 'connected'
  // A pending attempt THIS TAB is holding, or the backend's own mint table
  // saying a flow is in flight right now, outranks the cached probe verdicts
  // below. The mint side of this fix validates an existing grant before ever
  // reporting a mint `granted`, so a live `awaitingConsent`/`locallyWaiting`
  // here means either a genuinely fresh consent flow (the old grant did not
  // hold up) or a not-yet-decided reconnect -- never a flow the backend itself
  // already knows is stale. Reading `server.status === 'ok'` first, as this
  // branch used to, is exactly what let Connect flip Stripe and Vercel to
  // Connected on a cached probe the instant the click landed, well before the
  // mint had validated anything: the card claimed an authorization no fresher
  // fact yet backed. `oauth?.oauthUrl` is kept alongside the mint signal for
  // the chat-message delivery path, which never sets `awaitingConsent`.
  if (locallyWaiting || awaitingConsent || oauth?.oauthUrl) return 'waiting-for-approval'
  if (server.status === 'ok') {
    // The reachability probe is cached, so `ok` outlives a revoked grant. A
    // CONFIRMED absent grant (grantPresent === false, never the indeterminate
    // or not-yet-loaded undefined) is the fresher authorization fact and wins:
    // render the honest not-verified card instead of a Connected badge for an
    // authorization that no longer exists.
    return probeIndicatesConnected(server.status, grantPresent) ? 'connected' : 'not-verified'
  }
  // The status probe carries no OAuth token — kiro-cli owns token custody and
  // Kiro Crew stores no credential — so a remote OAuth server answers it with 401
  // and the gateway reports `needs_auth`. Two very different situations produce
  // that identical answer: a server nobody has authorized, and a server
  // authorized OUTSIDE the dashboard, which the runtime calls fine and which
  // raised no `mcp_oauth` banner here. The authorization axis from
  // /api/connections/status (`grantPresent`) is what tells them apart: a grant on
  // disk means the runtime IS authorized and the card is connected; no grant
  // leaves the honest `not-verified` (needs authorization to see this server).
  // Absent `grantPresent` (status feed not yet loaded) keeps the prior behaviour.
  // It must reach neither the error card (#1853) nor the spinner below, which
  // would imply a grant is in flight.
  if (server.status === 'needs_auth') {
    return probeIndicatesConnected(server.status, grantPresent) ? 'connected' : 'not-verified'
  }
  if (server.status === 'error' || server.status === 'disabled') return 'needs-attention'
  return 'waiting-for-approval'
}

/**
 * The card's approval-URL feed: the newest mcp_oauth chat message per server.
 *
 * Exported for test. `card_owned` is deliberately NOT consulted — that flag is a
 * hint to the CHAT renderer that this card already shows the same prompt, and the
 * card is the surface it points at. Filtering on it here would leave the card
 * with no URL at all.
 */
export function latestOAuthByServer(
  activeMessages: readonly ChatMessage[],
  slotMessages: Record<string, ChatMessage[]>,
): Record<string, OAuthState> {
  const result: Record<string, OAuthState> = {}
  const messages = [...Object.values(slotMessages).flat(), ...activeMessages]
  messages.forEach((message, index) => {
    if (message.role !== 'mcp_oauth') return
    const serverName = String(message.meta?.server_name || '').trim().toLowerCase()
    if (!serverName) return
    const parsed = Date.parse(message.ts || '')
    const timestamp = Number.isFinite(parsed) ? parsed : index
    const current = result[serverName]
    if (current && current.timestamp > timestamp) return
    result[serverName] = {
      completed: !!message.meta?.completed,
      failed: !!message.meta?.failed,
      oauthUrl: String(message.meta?.oauth_url || ''),
      error: String(message.meta?.error || ''),
      timestamp,
    }
  })
  return result
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
} as const

/**
 * Amber warning icon beside Connect for a provider with a blocking
 * provider-side prerequisite. Hover or focus previews the message as a small
 * bubble; clicking the icon pins the bubble open; clicking anywhere else (or
 * Escape) dismisses it. Modeled on InfoTip: portal-rendered so card overflow
 * cannot clip it, name/description split so the icon's accessible NAME stays a
 * short phrase while the prose rides as its DESCRIPTION.
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
        className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md text-warn transition-colors hover:bg-warn-subtle"
      >
        <AlertTriangle className="h-4 w-4" aria-hidden="true" />
      </button>
      {open && createPortal(
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
        </div>,
        document.body,
      )}
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
  // Tri-state, not a boolean, because "no tab" and "a tab the browser refused"
  // must read differently and `oauth.minted` cannot tell them apart: it stays
  // false for the whole poll window, so a boolean gated on it let a blocked-popup
  // user read "finish approving in your browser" about a tab they never got --
  // the exact claim this change exists to stop making.
  //   none    -- this attempt was not started from this card's Connect button
  //   open    -- the click opened a tab and it is waiting for a URL
  //   refused -- the click asked for a tab and the browser said no
  const [clickTab, setClickTab] = useState<'none' | 'open' | 'refused'>('none')

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
      tone: 'bg-warn-subtle text-warn',
    },
    connected: {
      label: t('pages.connectionsPage.connected'),
      icon: <CheckCircle2 className="w-3.5 h-3.5" aria-hidden="true" />,
      tone: 'bg-ok-subtle text-ok',
    },
    // Warn tone, not the error tone: an unverifiable state is not a failure. The
    // icon is static on purpose — a spinner would claim a grant is in flight
    // when nothing is pending.
    'not-verified': {
      label: t('pages.connectionsPage.not_verified'),
      icon: <KeyRound className="w-3.5 h-3.5" aria-hidden="true" />,
      tone: 'bg-warn-subtle text-warn',
    },
    'needs-attention': {
      label: t('pages.connectionsPage.needs_attention'),
      icon: <AlertTriangle className="w-3.5 h-3.5" aria-hidden="true" />,
      tone: 'bg-danger-subtle text-danger',
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
                  not start (`none`) keeps the original rule. Existing keys only:
                  the fuller copy rewrite needs a 14-locale pass and rides with the
                  connections-copy slice. */}
              {t(clickTab === 'open' || (clickTab === 'none' && !oauth?.minted)
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
                  className="min-w-0 flex-1 rounded-md border border-border bg-bg px-2.5 py-1.5 font-mono text-[11px] text-text outline-none focus-visible:ring-1 focus-visible:ring-accent"
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
            <div className="flex justify-end gap-2">
              <Btn
                onClick={() => void onTest()}
                disabled={!!busy || !!testingProvider}
                title={testingProvider ? t('pages.connectionsPage.test_blocked_by_sibling', { provider: testingProvider }) : undefined}
                aria-label={testingProvider ? t('pages.connectionsPage.test_blocked_by_sibling', { provider: testingProvider }) : undefined}
              >
                {busy === 'test' ? <Loader2 className="w-3.5 h-3.5 animate-spin" aria-hidden="true" /> : <RotateCw className="w-3.5 h-3.5" aria-hidden="true" />}
                {busy === 'test' ? t('pages.connectionsPage.testing') : t('pages.connectionsPage.test_connection')}
              </Btn>
              <Btn danger onClick={() => void onDisconnect()} disabled={!!busy}>
                <Unplug className="w-3.5 h-3.5" aria-hidden="true" /> {t('pages.connectionsPage.disconnect')}
              </Btn>
            </div>
          </div>
        )}

        {state === 'needs-attention' && (
          <div className="space-y-3">
            <div className="flex items-start gap-2 rounded-md bg-danger-subtle p-2.5 text-[12px] text-danger">
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
              <span>
                {t('pages.connectionsPage.connection_invalid', { provider: provider.name })}
                {(oauth?.error || server?.error) && <span className="mt-1 block text-[11px] text-muted">{oauth?.error || server?.error}</span>}
              </span>
            </div>
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

/**
 * `servicesEnabled` gates the provider gallery. The Connections work is merged
 * on main but held for a later release, so the default is CLOSED: the Services
 * panel offers no providers, so no card, Connect button or OAuth flow is
 * reachable.
 *
 * The panel still RENDERS rather than being removed, which is deliberate.
 * Hiding the sub-tab and defaulting to the MCP Servers table was tried and
 * reverted: it makes that table the default-rendered surface and so exposes its
 * pre-existing i18n debt to the render-time gate, which measured
 * `capabilities-mcp` going 44 -> 102 findings. Emptying the list keeps the
 * measured surface comparable to main (568 -> 558 overall, gate PASS) while
 * still removing every way to actually connect a provider.
 */
export default function ConnectionsPage({ servicesEnabled = false }: { servicesEnabled?: boolean } = {}) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const [activeTab, setActiveTab] = useState<'services' | 'mcp-servers'>('services')
  const [search, setSearch] = useState('')
  /** Pending connect attempts. `kind` decides Cancel semantics (only a
   *  cancelled *new* connect uninstalls the entry it just created); `sinceTs`
   *  fences off stale `mcp_oauth` banners left over from an earlier grant of
   *  the same server name (they must not mark a fresh attempt connected). */
  const [locallyWaiting, setLocallyWaiting] = useState<Record<string, PendingConnect>>({})
  const [busy, setBusy] = useState<{ slug: string; action: ConnectionAction } | null>(null)
  const [feedback, setFeedback] = useState<Record<string, Feedback>>({})
  const [highlightedSlug, setHighlightedSlug] = useState('')
  const activeMessages = useAppSelector(state => state.chat.messages)
  const slotMessages = useAppSelector(state => state.chat.slotMessages)
  const oauthByServer = useMemo(
    () => latestOAuthByServer(activeMessages, slotMessages),
    [activeMessages, slotMessages],
  )
  const { data: servers = [], isLoading, isError } = useQuery<McpServer[]>({
    queryKey: ['mcp-servers'],
    queryFn: () => api.mcpServers(),
    refetchInterval: activeTab === 'services' && Object.values(locallyWaiting).some(Boolean) ? 5_000 : false,
  })

  // Authorization verdict + first-connect time per visible provider. Polled while
  // the gallery is mounted so a grant completed outside the dashboard, and the
  // connected-since clock, surface without a manual refresh. Additive to the mint
  // feed below; this never mints and never owns reachability (that stays with
  // /api/mcp). Declared before the mint feed because `waitingSlugs` reads the
  // awaiting_consent verdicts.
  const { data: statusBySlug = {} } = useQuery<Record<string, ConnectionStatus>>({
    queryKey: ['connections-status'],
    queryFn: async () => {
      const { connections } = await api.connectionsStatus()
      const next: Record<string, ConnectionStatus> = {}
      for (const entry of connections) next[entry.slug] = entry
      return next
    },
    enabled: servicesEnabled,
    // Only while the gallery is the visible surface. On the MCP Servers tab no
    // card is rendered, so a background poll would stat every provider's grant
    // artifacts every 30s for a surface nobody is looking at.
    refetchInterval: activeTab === 'services' ? CONNECTION_STATUS_POLL_MS : false,
  })

  // Minted approval URLs, keyed by slug. Fetched only while a connect is pending:
  // outside that window nothing is minting and the endpoint would answer `idle`.
  // "Pending" has two sources of truth, and both must feed the poll: this tab's
  // own clicks (locallyWaiting) AND the backend's awaiting_consent verdict --
  // per-tab state dies on a reload, so without the status-fed half the
  // refresh-survival waiting card would render with no approval URL and copy
  // telling the user to start a flow that is already running.
  const waitingSlugs = useMemo(() => {
    const slugs = new Set(Object.keys(locallyWaiting))
    for (const [slug, entry] of Object.entries(statusBySlug)) {
      if (entry.status === 'awaiting_consent') slugs.add(slug)
    }
    return [...slugs].sort()
  }, [locallyWaiting, statusBySlug])
  const { data: mintByServer = {} } = useQuery<Record<string, ConnectionMintState>>({
    queryKey: ['connections-mint', waitingSlugs],
    queryFn: async () => {
      const states = await Promise.all(
        waitingSlugs.map(slug => api.connectionsMintState(slug).catch(() => undefined)),
      )
      const next: Record<string, ConnectionMintState> = {}
      for (const state of states) if (state) next[state.slug] = state
      return next
    },
    enabled: waitingSlugs.length > 0,
    refetchInterval: MINT_POLL_MS,
    // A mint row is only valid for the attempt that produced it. Cached across an
    // inactive window it would be replayed on the next Connect for the same
    // provider, flashing a previous attempt's URL that no listener can redeem.
    gcTime: 0,
  })

  /** Latched by the premint effect below, so one page visit warms once. */
  const premintFiredRef = useRef(false)
  // Warm every mintable provider's approval URL once, ahead of any click, so a
  // Connect serves a URL the warm table already holds instead of paying a cold
  // spawn. This is the documented caller for POST /api/connections/premint.
  //
  // Keyed on `servicesEnabled` rather than mount, because the flag arrives with
  // the config query: the page's FIRST render is always gated-off, so a `[]`
  // effect would warm on every install that never opted in. Gated for the same
  // reason as the status feed above — behind a closed flag the gallery offers no
  // card to connect, and warming would spawn a process for a surface that has no
  // Connect button.
  //
  // The ref guards StrictMode's development double-invoke (an in-flight request
  // is not cancellable, so a teardown flag cannot un-spawn the first activation).
  // It is per-mount by design: a later remount from navigation may warm again,
  // which the engine's warm reuse makes cheap, and suppressing it across visits
  // would pin the first visit's grant state for the whole session.
  useEffect(() => {
    if (!servicesEnabled || premintFiredRef.current) return
    premintFiredRef.current = true
    // Strictly fire-and-forget: nothing here reaches render, and the response is
    // never a verdict (a card's state stays its own mint feed). Every failure is
    // swallowed on purpose — a non-owner dashboard session is denied by design,
    // and a cold mint on Connect is the intended fallback either way.
    void api.connectionsPremint().catch(() => undefined)
  }, [servicesEnabled])

  useEffect(() => {
    // Decided BEFORE any setState: a state updater runs on a later render, so
    // collecting side-effect targets inside one leaves them empty at read time.
    const cleared: string[] = []
    const mintFailures: Array<{ slug: string; reason?: string }> = []
    const grantedMints: string[] = []
    for (const provider of CONNECTION_PROVIDERS) {
      const pending = locallyWaiting[provider.slug]
      if (!pending) continue
      const server = serverForConnection(provider, servers)
      const fresh = effectiveOAuth(oauthByServer[provider.slug], pending)
      const mint = mintByServer[provider.slug]
      const outcome = mintOutcome(mint, pending)
      if (!(server?.status === 'ok' || fresh?.completed || fresh?.failed || outcome.clearWait)) {
        continue
      }
      cleared.push(provider.slug)
      if (
        outcome.error
        || (
          mintRowIsOurs(mint, pending)
          && mint?.state === 'expired'
          && mint.reason
        )
      ) {
        mintFailures.push({ slug: provider.slug, reason: mint.reason })
      }
      if (outcome.probe) grantedMints.push(provider.slug)
    }
    if (!cleared.length) return

    setLocallyWaiting(current => {
      const next = { ...current }
      for (const slug of cleared) delete next[slug]
      return next
    })
    if (grantedMints.length) {
      // The cached status predates consent, so without a fresh read the card
      // keeps showing its pre-consent error after authorization succeeded.
      void api.mcpProbe().then(probed => {
        queryClient.setQueryData<McpServer[]>(['mcp-servers'], probed as McpServer[])
      }).catch(() => undefined)
      // Same staleness on the authorization axis: the status feed polls every
      // 30s, so its cached pre-consent verdict (grantPresent=false /
      // awaiting_consent) would outrank the grant that just landed and downgrade
      // the card for up to a full poll interval. Invalidate rather than
      // setQueryData: the fresh verdict is the backend's to compute.
      void queryClient.invalidateQueries({ queryKey: ['connections-status'] })
    }
    if (mintFailures.length) {
      setFeedback(current => {
        const next = { ...current }
        for (const { slug, reason } of mintFailures) {
          let error: string
          switch (reason) {
            case 'mint_timeouterror':
              error = t('pages.connectionsPage.mint_failure_timed_out')
              break
            case 'mint_process_gone':
              error = t('pages.connectionsPage.mint_failure_process_gone')
              break
            case 'mint_server_absent':
              error = t('pages.connectionsPage.mint_failure_server_absent')
              break
            case 'mint_url_rejected':
              error = t('pages.connectionsPage.mint_failure_url_rejected')
              break
            default:
              error = t('pages.connectionsPage.mint_failure_unknown')
          }
          next[slug] = {
            kind: 'error',
            text: t('pages.connectionsPage.action_failed', { error }),
          }
        }
        return next
      })
    }
  }, [servers, oauthByServer, mintByServer, locallyWaiting, queryClient, t])

  const filteredProviders = useMemo(() => {
    // Held feature: offer nothing. No card renders, so no Connect button and no
    // OAuth flow is reachable, while the panel itself still renders exactly the
    // markup it renders on main -- which is what keeps the render-time i18n gate
    // measuring a comparable surface.
    if (!servicesEnabled) return []
    const needle = search.trim().toLowerCase()
    if (!needle) return CONNECTION_PROVIDERS
    return CONNECTION_PROVIDERS.filter(provider =>
      `${provider.name} ${provider.slug} ${provider.mcp_url}`.toLowerCase().includes(needle),
    )
  }, [search, servicesEnabled])
  const feedbackSlots = filteredProviders.flatMap(provider => {
    const value = feedback[provider.slug]
    return value ? [{ slug: provider.slug, value }] : []
  })

  useEffect(() => {
    if (activeTab !== 'services' || !highlightedSlug) return
    requestAnimationFrame(() => {
      document.getElementById(`connection-${highlightedSlug}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' })
    })
  }, [activeTab, highlightedSlug])

  const run = async (
    provider: ConnectionProvider,
    action: ConnectionAction,
    operation: () => Promise<void>,
  ): Promise<boolean> => {
    setBusy({ slug: provider.slug, action })
    setFeedback(current => {
      const next = { ...current }
      delete next[provider.slug]
      return next
    })
    try {
      await operation()
      return true
    } catch (error) {
      const message = error instanceof Error ? error.message : t('pages.connectionsPage.unknown_error')
      setFeedback(current => ({
        ...current,
        [provider.slug]: {
          kind: 'error',
          text: t('pages.connectionsPage.action_failed', { error: message }),
          // Keyed on the message the API layer journaled, not the localized text
          // above, so the endpoint/status/`code` context survives into the
          // shared error surface in every locale.
          report: findReport(message),
        },
      }))
      return false
    } finally {
      setBusy(current => current?.slug === provider.slug ? null : current)
    }
  }

  const connect = async (provider: ConnectionProvider, existing?: McpServer) => run(provider, 'connect', async () => {
    // Snapshot the newest banner already observed for this server: anything
    // at or below this timestamp predates the attempt (same clock domain as
    // the banners themselves — see PendingConnect.sinceTs).
    const sinceTs = oauthByServer[provider.slug]?.timestamp ?? 0
    if (existing) {
      // Round-trip the stored spec and only overlay the url: a `{ url }`-only
      // PUT is authoritative for the OAuth hints, so it would clear configured
      // `scopes`/`clientId` (and any other stated field) on every reconnect.
      const stored = await api.mcpCustomGet(existing.name)
      await api.mcpCustomUpdate(existing.name, { ...stored.spec, url: provider.mcp_url })
      // Editing a spec deliberately preserves the disabled flag ("editing is
      // not consent to run") — but Reconnect IS consent, so re-enable the
      // KiroCrew-managed scope. mcpToggle would write the GLOBAL mcp.json
      // (creating an empty stub for kirocrew-scoped names), so use the
      // scope-preserving apply instead: kirocrew on, every observed global
      // scope passed through unchanged (the backend defaults kiroGlobal to
      // false when omitted).
      if (!existing.enabled) {
        const reenable: McpApplyChange = { name: existing.name, kirocrew: true }
        for (const [scope, present] of Object.entries(existing.presence ?? {})) {
          if (scope !== 'kirocrew' && scope.endsWith('Global')) reenable[scope as `${string}Global`] = !!present
        }
        await api.mcpApply([reenable])
      }
    } else {
      await api.mcpCustomAdd({ [provider.slug]: { url: provider.mcp_url } }, true)
    }
    // Ask for the approval URL rather than waiting for one, and await it: a
    // rejected POST must reach `run`'s error path instead of leaving the card in
    // a waiting state no mint will ever answer. Ordered after the entry write
    // because the mint activates a one-server spec derived from it. The response
    // names the row THIS tab started, so a sibling tab's terminal state cannot be
    // mistaken for ours.
    const started = await api.connectionsMint(provider.slug)
    setLocallyWaiting(current => ({
      ...current,
      [provider.slug]: {
        kind: existing ? 'reconnect' : 'new',
        sinceTs,
        token: started?.token,
      },
    }))
    // Kick a real status probe so the card reflects the new entry instead of
    // dead-ending on the cached /api/mcp read.
    void api.mcpProbe().then(probed => {
      queryClient.setQueryData<McpServer[]>(['mcp-servers'], probed as McpServer[])
    }).catch(() => undefined)
    await queryClient.invalidateQueries({ queryKey: ['mcp-servers'] })
  })

  const disconnect = async (provider: ConnectionProvider, server: McpServer, cancelled = false) => run(provider, 'disconnect', async () => {
    // Cancel must NOT revoke, and this branch is load-bearing. A grant is keyed by
    // ENDPOINT, not by entry, so a cancelled *new* connect routed through the
    // revoking endpoint would delete a grant that a user's own separately-named
    // server at the same URL is still using — silently, because `cancelled`
    // suppresses the note below. Cancel therefore keeps the entry-only removal it
    // always had; only a deliberate Disconnect revokes.
    if (cancelled) {
      await api.mcpApply([{ name: server.name, uninstall: true }])
    }
    // One call does all three local things: dispose any in-flight mint, delete the
    // stored grant artifacts when they are ours alone, and remove the MCP entry.
    // This was an mcpApply uninstall, which took the entry out and left a usable
    // refresh token on disk — so a later reconnect silently resumed a grant this
    // card had already told the user was gone.
    const result = cancelled ? undefined : await api.connectionsDisconnect(provider.slug)
    setLocallyWaiting(current => {
      const next = { ...current }
      delete next[provider.slug]
      return next
    })
    await queryClient.invalidateQueries({ queryKey: ['mcp-servers'] })
    // The grant feed too, mirroring the connect-completed path: a Disconnect that
    // deletes the grant but keeps the entry would otherwise leave the cached
    // grantPresent=true rendering "Connected" beside a note saying the grant is
    // gone, until the next poll.
    void queryClient.invalidateQueries({ queryKey: ['connections-status'] })
    if (result) {
      // Facts are reported INDEPENDENTLY, never as an exclusive chain — two review
      // rounds landed findings in this span because each single message asserted a
      // second fact it never tested ("Entry removed." while the entry stayed;
      // "Disconnected, but…" while the backend declined). The GRANT clause states
      // only what happened to the grant; the ENTRY clause is appended whenever the
      // backend left the entry alone. Outcomes that announce: a survivor is an
      // `error` (a grant outliving the click is the state this endpoint exists to
      // prevent), a census gap is a `warning` (nothing failed, but the grant is
      // still there and the configuration needs checking), and a not-ours entry is
      // a `warning` too (the card still shows Connected with a live Disconnect
      // button, so a green success would misreport a click that changed nothing).
      // A grant deliberately kept for a NAMED sharer needs nothing from the user,
      // so it stays a success status.
      // `grantSurviving` now reports FAILED unlinks only: the backend re-stats
      // just the pairs it actually tried to remove, so a deliberate keep (a
      // sharer, or a census gap) never appears here. That is what collapses the
      // precedence ladder these branches used to need — a survivor no longer has
      // to be disambiguated against `shared`/`censusGap` before it can alert.
      const survived = result.grantSurviving.length > 0
      const shared = result.grantSharedWith.length > 0
      const censusGap = !shared && result.grantCensusIncomplete
      const entryKept = !result.entryRemoved
      // The not-ours outcome: nothing here was this provider's to remove — no
      // grant artifacts existed and no purge-eligible entry matched, so the
      // click changed nothing. The entry clause is the whole message there, and
      // it must hand the user a next move: without the recourse their only
      // move is to click Disconnect again. The message states only what the
      // response proves (nothing changed) — `entryRemoved=false` cannot say WHY
      // the entry was kept, so the copy never asserts a cause.
      const entryNotOurs = entryKept && !survived && !shared && !censusGap && !result.grantRemoved
      // The census knows which source it could not read, so the repair instruction
      // names it. Empty is the honest case, not a missing field: `censusIncomplete`
      // is also set by an entry whose URL could not be compared, which names no
      // file -- so that outcome keeps the source-less wording instead of
      // interpolating a blank into "fix that file".
      const unreadable = result.grantCensusUnreadable ?? []
      const grantClause = survived
        ? t('pages.connectionsPage.disconnect_grant_survived')
        : shared
          ? t('pages.connectionsPage.disconnect_grant_shared', {
              names: result.grantSharedWith.join(', '),
            })
          : censusGap
            ? unreadable.length > 0
              ? t('pages.connectionsPage.disconnect_census_incomplete_source', {
                  source: unreadable[0],
                })
              : t('pages.connectionsPage.disconnect_census_incomplete')
            : result.grantRemoved && entryKept
            ? t('pages.connectionsPage.disconnect_entry_not_ours')
            : entryKept
              ? '' // no grant existed and the entry stayed: the entry clause is the whole story
              : t('pages.connectionsPage.disconnected_locally')
      const entryClause =
        entryKept && (survived || shared || !result.grantRemoved)
          ? t('pages.connectionsPage.disconnect_entry_left_alone')
          : ''
      setFeedback(current => ({
        ...current,
        [provider.slug]: disconnectFeedback(
          provider,
          [grantClause, entryClause].filter(Boolean).join(' '),
          // A census gap tells the user their access was NOT withdrawn and hands
          // them a repair to make; a not-ours entry leaves the card showing
          // Connected with a live Disconnect button, so a green success would
          // misreport a click that changed nothing. Neither is an `error`,
          // because nothing failed — a safety rule declined to act, or there was
          // nothing here to act on.
          survived ? 'error' : censusGap || entryNotOurs ? 'warning' : 'success',
        ),
      }))
    }
  })

  const cancelConnection = async (provider: ConnectionProvider, server?: McpServer): Promise<boolean> => {
    const pending = locallyWaiting[provider.slug]
    // Dispose the in-flight backend mint (its kiro-cli process, loopback listener
    // and ephemeral spec) whether or not we also uninstall the config below. This
    // is what main lacked: a cancelled reconnect or stateless wait dropped only
    // the local wait and left the mint held to its TTL.
    //
    // Deliberately NOT awaited. Disposal waits on a child process shutdown, which
    // is bounded only by the gateway's shutdown timeout (~10s), and awaiting it
    // would leave Cancel un-actioned and re-clickable for that whole window. The
    // withdrawal the user asked for is local; the dispose is bookkeeping that
    // follows. Token-fenced so a stale tab cannot dispose a sibling's row, and
    // the rejection is swallowed so a gateway failure never surfaces as a Cancel
    // that did not work.
    void api.connectionsCancel(provider.slug, pending?.token).catch(() => undefined).finally(() => {
      // The dispose just changed the backend verdict, so re-fetch it rather
      // than waiting out the 30s poll.
      void queryClient.invalidateQueries({ queryKey: ['connections-status'] })
    })
    // Standard optimistic-update fence: a 30s poll already in flight was
    // fetched BEFORE the cancel, so letting it resolve after the drop below
    // would repopulate the stale awaiting_consent verdict until the
    // settlement invalidation lands. Cancel the in-flight fetch first.
    await queryClient.cancelQueries({ queryKey: ['connections-status'] })
    // Drop this provider's cached verdict NOW: the poll cached `awaiting_consent`
    // for up to 30s, and with the flow just disposed that stale entry would put
    // the card straight back into waiting-for-approval -- a Cancel that appears
    // to not work. Dropping (not fabricating a verdict) returns the card to the
    // status-not-yet-loaded behaviour until the invalidated query answers.
    queryClient.setQueryData<Record<string, ConnectionStatus>>(['connections-status'], current => {
      if (!current || !(provider.slug in current)) return current
      const next = { ...current }
      delete next[provider.slug]
      return next
    })
    // The wait dies with the click, unconditionally and BEFORE the uninstall:
    // the mint was just disposed, so if the uninstall below fails there is no
    // outcome left that could ever clear this flag -- leaving it set would
    // strand the card on a waiting state with no live flow behind it.
    setLocallyWaiting(current => {
      const next = { ...current }
      delete next[provider.slug]
      return next
    })
    if (uninstallOnCancel(pending)) {
      // The entry may not be in the cached list yet (probe still pending) —
      // fall back to the slug the connect just wrote so Cancel always undoes it.
      const target = server ?? ({ name: provider.slug } as McpServer)
      return disconnect(provider, target, true)
    }
    return true
  }

  const testConnection = async (provider: ConnectionProvider) => run(provider, 'test', async () => {
    let result
    try {
      result = await api.connectionsTest(provider.slug)
    } catch (error) {
      // A single-flight refusal is a REJECTED REQUEST, so it belongs to the
      // shared error surface (`ErrorNotice`, via `Feedback.report`) rather than
      // this page's plain feedback line -- but it is named rather than left to
      // the ambiguous "action_failed" catch in `run` below, because the one
      // thing the user needs is WHICH provider is holding the slot.
      if (error instanceof ApiError && error.status === 409) {
        let runningSlug = ''
        try {
          const parsed: unknown = JSON.parse(error.body)
          if (parsed && typeof parsed === 'object' && typeof (parsed as { slug?: unknown }).slug === 'string') {
            runningSlug = (parsed as { slug: string }).slug
          }
        } catch { /* malformed body — fall back to the generic provider name below */ }
        const runningProvider = CONNECTION_PROVIDERS.find(candidate => candidate.slug === runningSlug)?.name
          ?? provider.name
        setFeedback(current => ({
          ...current,
          [provider.slug]: {
            kind: 'error',
            text: t('pages.connectionsPage.test_in_flight', { provider: runningProvider }),
            // Keyed on the message the API layer journaled, not the localized
            // text rendered above, so the endpoint/status/`code` context is
            // recovered in every locale. A miss is tolerated -- `kind: 'error'`
            // is what routes this to the shared surface, not the report.
            report: findReport(error.message),
          },
        }))
        return
      }
      throw error
    }
    if (result.verdict === 'usable') {
      setFeedback(current => ({
        ...current,
        [provider.slug]: { kind: 'success', text: t('pages.connectionsPage.connection_healthy') },
      }))
      return
    }
    if (result.verdict === 'no_tools') {
      // A connected-but-toolless GitLab is exactly who needs the provider-side
      // steps, and its prerequisite copy describes this very state ("connecting
      // succeeds but exposes no tools"), so it rides along localized. Atlassian's
      // copy describes a pre-connect consent gate the user already passed, so it
      // deliberately does not.
      setFeedback(current => ({
        ...current,
        [provider.slug]: {
          kind: 'warning',
          text: t('pages.mcpManagement.assessment.reason_no_tools_listed'),
          detail:
            provider.slug === 'gitlab'
              ? t('pages.connectionsPage.prerequisite_gitlab_steps')
              : undefined,
          help: provider.slug === 'gitlab' ? { href: provider.docs_url } : undefined,
        },
      }))
      return
    }
    throw new Error(t('pages.connectionsPage.test_failed'))
  })

  const relayReturnAddress = async (provider: ConnectionProvider, returnAddress: string) => run(provider, 'relay', async () => {
    await api.mcpOAuthRelay(provider.slug, returnAddress)
    setFeedback(current => ({
      ...current,
      [provider.slug]: { kind: 'success', text: t('pages.connectionsPage.return_address_delivered') },
    }))
    await queryClient.invalidateQueries({ queryKey: ['mcp-servers'] })
  })

  const selectTab = (tab: 'services' | 'mcp-servers') => setActiveTab(tab)
  const onTabKeyDown = (event: KeyboardEvent<HTMLButtonElement>) => {
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return
    event.preventDefault()
    setActiveTab(current => current === 'services' ? 'mcp-servers' : 'services')
  }
  const openProvider = (slug: string) => {
    setSearch('')
    setHighlightedSlug(slug)
    setActiveTab('services')
  }

  return (
    <section className="min-w-0" aria-label={t('pages.connectionsPage.connections')}>
      <div className="mb-4 flex border-b border-border" role="tablist" aria-label={t('pages.connectionsPage.connection_views')}>
        <button
          id="connections-services-tab"
          type="button"
          role="tab"
          aria-selected={activeTab === 'services'}
          aria-controls="connections-services-panel"
          tabIndex={activeTab === 'services' ? 0 : -1}
          onClick={() => selectTab('services')}
          onKeyDown={onTabKeyDown}
          className={`flex items-center gap-1.5 border-b-2 px-3 py-2 text-[13px] font-medium transition-colors ${activeTab === 'services' ? 'border-accent text-accent' : 'border-transparent text-muted hover:text-text'}`}
        >
          <Link2 className="h-4 w-4" aria-hidden="true" /> {t('pages.connectionsPage.services')}
        </button>
        <button
          id="connections-mcp-tab"
          type="button"
          role="tab"
          aria-selected={activeTab === 'mcp-servers'}
          aria-controls="connections-mcp-panel"
          tabIndex={activeTab === 'mcp-servers' ? 0 : -1}
          onClick={() => selectTab('mcp-servers')}
          onKeyDown={onTabKeyDown}
          className={`flex items-center gap-1.5 border-b-2 px-3 py-2 text-[13px] font-medium transition-colors ${activeTab === 'mcp-servers' ? 'border-accent text-accent' : 'border-transparent text-muted hover:text-text'}`}
        >
          <Server className="h-4 w-4" aria-hidden="true" /> {t('pages.connectionsPage.mcp_servers')}
        </button>
      </div>

      {activeTab === 'services' ? (
        <div id="connections-services-panel" role="tabpanel" aria-labelledby="connections-services-tab">
          {servicesEnabled && <div className="mb-4 flex items-center gap-3">
            <SearchInput
              value={search}
              onChange={event => setSearch(event.target.value)}
              placeholder={t('pages.connectionsPage.search_services')}
              aria-label={t('pages.connectionsPage.search_services')}
              className="max-w-[520px] flex-1"
            />
            <Badge variant="muted">{t('pages.connectionsPage.services_available', { value: filteredProviders.length })}</Badge>
          </div>}

          {isError && (
            <div role="alert" className="mb-3 rounded-md border border-danger/30 bg-danger-subtle px-3 py-2 text-[12px] text-danger">
              {t('pages.connectionsPage.could_not_load_status')}
            </div>
          )}

          {isLoading ? (
            <ContentSkeleton rows={6} />
          ) : filteredProviders.length === 0 ? (
            <div className="rounded-lg border border-dashed border-border px-4 py-10 text-center text-sm text-muted">
              {t('pages.connectionsPage.no_matching_services')}
            </div>
          ) : (
            <div className="grid grid-cols-1 gap-3 xl:grid-cols-2 2xl:grid-cols-3">
              {filteredProviders.map(provider => {
                const server = serverForConnection(provider, servers)
                const pending = locallyWaiting[provider.slug]
                const oauth = withMintedUrl(
                  effectiveOAuth(oauthByServer[provider.slug], pending),
                  mintByServer[provider.slug],
                )
                const status = statusBySlug[provider.slug]
                const state = connectionStateFor(
                  server,
                  oauth,
                  !!pending,
                  // Only a CONFIRMED verdict may steer the card: an indeterminate
                  // lookup reports grantPresent=false without knowing anything.
                  confirmedGrantPresent(status),
                  // The backend's mint table outlives this tab's local state, so
                  // a refresh mid-consent still renders the waiting card.
                  status?.status === 'awaiting_consent',
                )
                const cardBusy = busy?.slug === provider.slug ? busy.action : undefined
                // Named only when a DIFFERENT card owns the running test: this
                // card's own in-flight test is already covered by `cardBusy`,
                // and naming a card against itself would read as nonsense
                // ("Vercel is testing" on Vercel's own disabled button).
                const testingProvider = busy?.action === 'test' && busy.slug !== provider.slug
                  ? CONNECTION_PROVIDERS.find(candidate => candidate.slug === busy.slug)?.name
                  : undefined
                return (
                  <ConnectionCard
                    key={provider.slug}
                    provider={provider}
                    server={server}
                    state={state}
                    oauth={oauth}
                    connectedSince={status?.connectedSince}
                    // The same confirmed-only verdict the state fold received:
                    // indeterminate stays undefined so the card keeps the hedge.
                    grantPresent={confirmedGrantPresent(status)}
                    busy={cardBusy}
                    testingProvider={testingProvider}
                    feedbackSlots={feedbackSlots}
                    highlighted={highlightedSlug === provider.slug}
                    onConnect={() => connect(provider)}
                    onCancel={() => cancelConnection(provider, server)}
                    onDisconnect={() => server ? disconnect(provider, server) : Promise.resolve()}
                    onReconnect={() => connect(provider, server)}
                    onTest={() => testConnection(provider)}
                    onRelay={returnAddress => relayReturnAddress(provider, returnAddress)}
                  />
                )
              })}
            </div>
          )}
        </div>
      ) : (
        <div id="connections-mcp-panel" role="tabpanel" aria-labelledby="connections-mcp-tab">
          <McpTab onManagedProviderClick={openProvider} />
        </div>
      )}
    </section>
  )
}
