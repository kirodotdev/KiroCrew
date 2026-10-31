import { memo, useCallback, useContext, useEffect, useId, useRef, useState } from 'react'
import { QueryClientContext, useQuery } from '@tanstack/react-query'
import { motion, useReducedMotion } from 'framer-motion'
import { ShieldOff } from 'lucide-react'
import { useInRouterContext, useNavigate } from 'react-router-dom'

import type { ChatMessage } from '../../types'
import { fetchDashboardConfig } from '../../api/dashboardConfigQuery'
import { i18nT } from '../../i18n/t'
import { browsePolicyOff } from '../../lib/browsePolicy'
import type { BrowserHandbackSaid } from '../../api/client/browserLive'
import { browserLiveStore, useBrowserLiveCurrent, useBrowserLiveIsCurrent, useBrowserLiveTurn, type BrowserLiveTurn } from '../../hooks/useBrowserLive'
import { browserStageStore, stageTurnId, useBrowserStage } from '../../hooks/useBrowserLive.stage'
import { useIsMobile } from '../../hooks/useIsMobile'
import { useLanguageGeneration } from '../../i18n/useLanguageGeneration'
import type { BrowserLiveCanvasHandle } from './BrowserLiveCardCanvas'
import { clearTopOverlay, holdPinnedPromptAside } from './browserLiveClearance'
import { BrowserLiveAllowView, BrowserLiveDoneRow, BrowserLiveLiveView, BrowserLiveStagedRow, hostOf } from './BrowserLiveCardViews'
import BrowserLiveSheet from './BrowserLiveSheet'

/** How long the takeover's grow animation takes to settle before the card re-checks its place. */
const GROW_SETTLE_MS = 400

/**
 * BrowserLiveCard: the agent's browser, in the chat, while it browses.
 *
 * One card per turn, attached to that turn's FIRST `browser` tool call
 * (TurnBlock mounts it after the turn's last browser row, so it sits where the
 * eye already is: between the work and the live "Thinking" line). The card is
 * ONE element through every state. Waiting for Allow, live, hovered, in
 * control, done: the same `motion.div` with one `layoutId` changes size and
 * contents, never swaps for another component, so a takeover reads as "the
 * card grew" and a finished turn as "the card folded to a line". Reduced motion
 * drops the animation and keeps the continuity.
 *
 * State comes from `useBrowserLive` (the `browser_live` websocket event and the
 * binary frame stream); this component only decides what to show for it.
 * Frames are painted by `BrowserLiveCardCanvas` outside React. Visibility is
 * reported from here: an IntersectionObserver on the root plus the document's
 * own visibility, so an off-screen or background card stops the screencast.
 *
 * Big view. Expand and Replay do not grow the card: they move the picture to
 * the stage (`useBrowserLive.stage`), the side panel's Preview tab or, on a
 * phone or a page with no side panel, a full-screen sheet. While the stage shows
 * this card's turn, the same motion element folds to one line that says where
 * the picture went and offers Bring back. Takeover still grows the card in place.
 *
 * Trust: `title` and every step label are agent- or page-authored text and go
 * through `sanitizeLlmOutput` in the views; the URL shows as its host. Nothing
 * typed during takeover passes through this component at all.
 */

/** The `browser` tool row: the trusted name when the gateway stamped one, else the pill text.
 *
 * The tool is a managed-MCP tool, so a real row reads `🔧 @kirocrew-core/browser`
 * (the `@server/tool` form every MCP call arrives in) and a stamped name may carry
 * the server too. Both spellings are the one tool; a bare `browser` is kept for the
 * built-in shape and the fixtures. */
const BROWSER_ROW_RE = /^🔧\s*(?:@(?:[\w.-]+\/)?)?browser(?:\s|$)/
const NAMESPACED_BROWSER_RE = /(?:^|\/|::|___)browser$/
export function isBrowserToolMessage(msg: ChatMessage): boolean {
  if (msg.role !== 'tool') return false
  const trusted = msg.meta?.tool_name
  if (typeof trusted === 'string' && trusted) return NAMESPACED_BROWSER_RE.test(trusted)
  return BROWSER_ROW_RE.test(msg.content)
}

/** What a turn's card anchors on: the first browser row's call id, else its timestamp. */
export function browserLiveAnchor(msg: ChatMessage): { key: string; ts: number | null } {
  const id = msg.meta?.tool_call_id
  const key = typeof id === 'string' && id ? `call:${id}` : `ts:${msg.ts ?? ''}`
  const n = msg.ts ? Number(msg.ts) : NaN
  // Server timestamps are seconds (a float string); an ISO string parses too.
  const ts = Number.isFinite(n) ? n * 1000 : msg.ts ? Date.parse(msg.ts) : NaN
  return { key, ts: Number.isFinite(ts) ? ts : null }
}

export interface BrowserLiveCardProps {
  sessionKey: string
  anchorKey: string
  anchorTs: number | null
  /** The turn is still running, so a card with no binding takes the session's current record. */
  running: boolean
  /** Navigate to a dashboard path (Settings -> Browser). */
  goTo: (to: string) => void
  /** A policy turned browsing off (`browse_enabled: false`): the card is one line,
   *  with no takeover, live, replay or expand control. The gateway refuses those
   *  routes on its own; this only stops offering them. */
  policyOff?: boolean
}

type View = 'allow' | 'live' | 'done'

function viewFor(turn: BrowserLiveTurn): View {
  switch (turn.state) {
    case 'waiting_allow': return 'allow'
    case 'done':
    case 'error': return 'done'
    default: return 'live'
  }
}

function BrowserLiveCard({ sessionKey, anchorKey, anchorTs, running, goTo, policyOff = false }: BrowserLiveCardProps) {
  useLanguageGeneration()
  const turn = useBrowserLiveTurn(sessionKey, anchorKey, running, anchorTs)
  const reduce = useReducedMotion()
  const instanceId = useId()
  const rootRef = useRef<HTMLDivElement>(null)
  const canvasRef = useRef<BrowserLiveCanvasHandle>(null)
  const [onScreen, setOnScreen] = useState(false)
  const isMobile = useIsMobile()
  const stage = useBrowserStage(sessionKey)
  const current = useBrowserLiveCurrent(sessionKey)

  // On-screen tracking. An observer where the host has one; a host without
  // (an old shim) counts the card as visible so the stream is never stuck off.
  // The report names this card's turn: a finished card still on screen must not
  // keep a later turn's screencast running while that turn's card is scrolled off.
  const turnId = turn?.turnId ?? ''
  const isCurrent = useBrowserLiveIsCurrent(sessionKey, turnId)
  useEffect(() => {
    const el = rootRef.current
    if (!el || !turnId) return
    if (typeof IntersectionObserver !== 'function') {
      setOnScreen(true)
      browserLiveStore.setVisible(sessionKey, instanceId, true, turnId)
      return () => browserLiveStore.setVisible(sessionKey, instanceId, false, turnId)
    }
    const io = new IntersectionObserver((entries) => {
      const v = entries.some(e => e.isIntersecting)
      setOnScreen(v)
      browserLiveStore.setVisible(sessionKey, instanceId, v, turnId)
    }, { threshold: 0.05 })
    io.observe(el)
    return () => {
      io.disconnect()
      browserLiveStore.setVisible(sessionKey, instanceId, false, turnId)
    }
    // `policyOff` swaps the root for the policy line and back: observe the new root.
  }, [sessionKey, instanceId, turnId, policyOff])

  const inControl = turn?.state === 'in_control'
  // Focus the frame the moment control arrives, so keys go to the page at once,
  // and keep the grown card's header (who is in control, Hand back) visible: the
  // pinned-prompt banner steps aside until the hand-back, and the card scrolls
  // out from under the title row, once now and once the grow has settled.
  useEffect(() => {
    if (!inControl) return
    canvasRef.current?.focus()
    const releaseAside = holdPinnedPromptAside()
    const clear = () => { if (rootRef.current) clearTopOverlay(rootRef.current) }
    const raf = requestAnimationFrame(clear)
    const settle = setTimeout(clear, GROW_SETTLE_MS)
    return () => { cancelAnimationFrame(raf); clearTimeout(settle); releaseAside() }
  }, [inControl])

  const claim = useCallback(() => { void browserLiveStore.claim(sessionKey) }, [sessionKey])
  const release = useCallback((said?: BrowserHandbackSaid) => { void browserLiveStore.release(sessionKey, said) }, [sessionKey])
  const openInBrowser = useCallback(() => browserLiveStore.openInBrowser(sessionKey), [sessionKey])
  // "Open live" restarts the screencast on the tab this card showed; the server's
  // event turns the same card live again (same turn id), so Take over and Hand
  // back work exactly as during the turn. Close folds it back.
  const openLive = useCallback(() => {
    if (turnId) void browserLiveStore.reopen(sessionKey, turnId)
  }, [sessionKey, turnId])
  const closeLive = useCallback(() => browserLiveStore.closeLive(sessionKey), [sessionKey])

  // A running turn's Expand follows "now" (null), so the big view moves on to the
  // next turn with the agent; a reopened, finished turn pins its own id.
  const turnOver = turn?.turnOver ?? false
  const expand = useCallback(() => {
    if (!turnId) return
    browserStageStore.open(sessionKey, { turnId: turnOver ? turnId : null, mode: 'live', preferSheet: isMobile })
  }, [sessionKey, turnId, turnOver, isMobile])
  const replay = useCallback(() => {
    if (!turnId) return
    browserStageStore.open(sessionKey, { turnId, mode: 'replay', preferSheet: isMobile })
  }, [sessionKey, turnId, isMobile])
  const bringBack = useCallback(() => browserStageStore.close(sessionKey), [sessionKey])

  if (!turn) return null
  if (policyOff) {
    return (
      <div className="px-4 mx-auto w-full py-1" style={{ maxWidth: 'var(--mc-content-width, 900px)' }}>
        <div
          data-testid="browser-live-policy-off"
          className="flex items-center gap-1.5 rounded-xl border border-border bg-card px-3 py-2 text-[13px] text-muted"
        >
          <ShieldOff size={14} className="shrink-0" aria-hidden />
          {i18nT('pages.settings.browserPanel.policy_off')}
        </div>
      </div>
    )
  }
  const view = viewFor(turn)
  const staged = !!stage && stageTurnId(stage, current?.turnId ?? null) === turn.turnId
  const grown = inControl && !staged
  const transition = reduce ? { duration: 0 } : { type: 'spring' as const, stiffness: 420, damping: 38 }

  return (
    <div className="px-4 mx-auto w-full py-1" style={{ maxWidth: 'var(--mc-content-width, 900px)' }}>
      <motion.div
        ref={rootRef}
        layout
        layoutId={`browser-live-${sessionKey}-${turn.turnId}`}
        transition={transition}
        data-testid="browser-live-card"
        data-state={turn.state}
        data-grown={grown ? 'true' : undefined}
        className={`relative overflow-hidden rounded-xl border bg-card text-text ${
          inControl
            ? 'border-accent ring-2 ring-accent/40 shadow-[0_0_28px_var(--accent-glow)]'
            : 'border-border shadow-sm'
        }`}
        style={grown ? { marginInline: 'calc(-1 * clamp(0px, (100vw - var(--mc-content-width, 900px) - 160px) / 2, 56px))' } : undefined}
      >
        {staged && stage && (
          <BrowserLiveStagedRow turn={turn} replaying={stage.mode === 'replay'} surface={stage.surface} onBringBack={bringBack} />
        )}
        {!staged && view === 'allow' && (
          <BrowserLiveAllowView
            turn={turn}
            onUseKiroBrowser={() => goTo('/settings/browser')}
          />
        )}
        {!staged && view === 'live' && (
          <BrowserLiveLiveView
            turn={turn}
            onScreen={onScreen}
            canvasRef={canvasRef}
            onClaim={claim}
            onRelease={release}
            onOpenInBrowser={openInBrowser}
            onExpand={expand}
            onCloseLive={closeLive}
          />
        )}
        {!staged && view === 'done' && (
          <BrowserLiveDoneRow
            turn={turn}
            onScreen={onScreen}
            canvasRef={canvasRef}
            onReplay={replay}
            onOpenInBrowser={openInBrowser}
            canOpenLive={turn.state === 'done' && turn.tab === 'open' && isCurrent}
            onOpenLive={openLive}
          />
        )}
      </motion.div>
      {/* The sheet surface of the stage, for this card's turn. */}
      {staged && stage?.surface === 'sheet' && <BrowserLiveSheet sessionKey={sessionKey} />}
    </div>
  )
}

/** The Allow card's "use Kiro's own browser" goes to Settings -> Browser. The
 *  router is optional here (the app-sdk embed has none), so the navigation is
 *  resolved once, above, and handed down as a plain function. */
function BrowserLiveCardWithRouter(props: Omit<BrowserLiveCardProps, 'goTo'>) {
  const navigate = useNavigate()
  const goTo = useCallback((to: string) => navigate(to), [navigate])
  return <BrowserLiveCard {...props} goTo={goTo} />
}

const assignLocation = (to: string) => { window.location.assign(to) }

type RootProps = Omit<BrowserLiveCardProps, 'goTo' | 'policyOff'>

function BrowserLiveCardRouted(props: Omit<BrowserLiveCardProps, 'goTo'>) {
  return useInRouterContext() ? <BrowserLiveCardWithRouter {...props} /> : <BrowserLiveCard {...props} goTo={assignLocation} />
}

/** The governance answer, from the dashboard config query every chat page already holds. */
function BrowserLiveCardGoverned(props: RootProps) {
  const { data } = useQuery<{ browse_enabled?: boolean }>({
    queryKey: ['dashboardConfig'],
    queryFn: fetchDashboardConfig,
    staleTime: 30_000,
  })
  return <BrowserLiveCardRouted {...props} policyOff={browsePolicyOff(data)} />
}

/** A host with no query client (an embed) cannot read the answer; the gateway still refuses. */
function BrowserLiveCardRoot(props: RootProps) {
  return useContext(QueryClientContext) != null
    ? <BrowserLiveCardGoverned {...props} />
    : <BrowserLiveCardRouted {...props} />
}

export { hostOf }
export default memo(BrowserLiveCardRoot)
