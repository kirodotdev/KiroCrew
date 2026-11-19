/**
 * `BrowserView` (`@kirocrew/app-sdk`): the live view of the browser in one of the
 * app's own chat slots, with take over and hand back.
 *
 * It is the dashboard's own live view, wrapped: the picture is
 * `BrowserLiveCardCanvas`, the state is `browserLiveStore` (the same record the
 * chat card reads), and input goes through `useBrowserLive.input` on the
 * canvas's ordered chain. So the password boxes, Esc to hand back, the IME proxy
 * and the screencast's visibility work exactly as on the card. Nothing here adds
 * a channel or a route (RFC app-sdk-browser, section 5).
 *
 * Marks are drawn by this component over the picture, in view coordinates, and
 * never enter the page. `useBrowserCoords(slot)` converts to and from the page's
 * frame pixels.
 *
 * Shows a placeholder instead of the page when the `app_browser_sdk` preview is
 * off (`not enabled`) or the slot is not this app's (`denied`); see
 * `browserViewHooks.ts` for that rule and why it is not a security boundary.
 */
import { useCallback, useContext, useEffect, useId, useRef, useState } from 'react'

import { Btn } from '../components/ui'
import ErrorNotice from '../components/ErrorNotice'
import { i18nT } from '../i18n/t'
import { browserLiveStore, useBrowserLiveCurrent } from '../hooks/useBrowserLive'
import type { FrameSize } from '../hooks/useBrowserLive.input'
import type { BrowserHandbackSaid } from '../api/client/browserLive'
import BrowserLiveCardCanvas, { type BrowserLiveCanvasHandle } from '../pages/chat/BrowserLiveCardCanvas'
import { hostOf } from '../pages/chat/BrowserLiveCardViews'
import { PictureBox } from '../pages/chat/BrowserLiveReplayStage'
import { BrowserLiveHandbackBar } from '../pages/chat/BrowserLiveHandback'
import { sanitizeLlmOutput } from '../utils/sanitize'
import { reportBrowserViewGeometry, useBrowserView } from './browserViewHooks'
import { AppSdkContext } from './scopedApi'

/** One annotation, in view coordinates (CSS pixels from the view's top left). */
export interface BrowserViewMark {
  x: number
  y: number
  /** A box when both are given, else a dot at (x, y). */
  width?: number
  height?: number
  /** Plain text shown beside the mark; never HTML. */
  label?: string
}

export interface BrowserViewProps {
  /** One of this app's slot keys (`<app>-<id>`). */
  slot: string
  /** May the person take over; default true. */
  interactive?: boolean
  /** The person took over in this view, once the gateway accepted it. */
  onTakeover?: () => void
  onHandBack?: (note?: string) => void
  onNavigate?: (e: { url: string; title: string }) => void
  marks?: readonly BrowserViewMark[]
  'aria-label': string
  className?: string
}

const LIVE = new Set(['live', 'in_control', 'waiting_allow', 'login_needed'])

function Placeholder({ state, text, className }: { state: string; text: string; className?: string }) {
  return (
    <div
      className={`grid place-items-center rounded-md border border-border bg-card p-6 text-center text-[13px] text-muted ${className ?? ''}`}
      data-testid="app-browser-view"
      data-state={state}
      role="status"
    >
      {text}
    </div>
  )
}

export default function BrowserView({
  slot, interactive = true, onTakeover, onHandBack, onNavigate, marks, 'aria-label': ariaLabel, className,
}: BrowserViewProps) {
  const view = useBrowserView(slot)
  const { access } = view
  const ok = access === 'ok'
  const turn = useBrowserLiveCurrent(ok ? slot : null)
  const instanceId = useId()
  const [frame, setFrame] = useState<FrameSize | null>(null)
  const boxRef = useRef<HTMLDivElement>(null)
  const live = !!turn && LIVE.has(turn.state)
  const inControl = turn?.state === 'in_control'
  const turnId = turn?.turnId ?? null

  // On screen for the slot's current turn, so the gateway keeps streaming it, and
  // only while it is: a hidden host surface (a side-panel tab another tab covers,
  // `info.active` false) or a view scrolled off reports false, so neither the
  // screencast nor a takeover's keep-alive outlives what the person can see.
  const hostActive = useContext(AppSdkContext)?.info.active ?? true
  const [rootEl, setRootEl] = useState<HTMLDivElement | null>(null)
  const [onScreen, setOnScreen] = useState(true)
  useEffect(() => {
    if (!rootEl || typeof IntersectionObserver !== 'function') { setOnScreen(true); return }
    const io = new IntersectionObserver((entries) => setOnScreen(entries.some(e => e.isIntersecting)), { threshold: 0.05 })
    io.observe(rootEl)
    return () => io.disconnect()
  }, [rootEl])
  const shown = hostActive && onScreen
  useEffect(() => {
    if (!ok || !turnId) return
    browserLiveStore.setVisible(slot, instanceId, shown, turnId)
    return () => browserLiveStore.setVisible(slot, instanceId, false, turnId)
  }, [ok, slot, instanceId, turnId, shown])

  // The picture box's size, for useBrowserCoords.
  useEffect(() => {
    const el = boxRef.current
    if (!ok || !el) return
    const report = () => reportBrowserViewGeometry(slot, { width: el.clientWidth, height: el.clientHeight, frame })
    report()
    if (typeof ResizeObserver === 'undefined') return () => reportBrowserViewGeometry(slot, null)
    const ro = new ResizeObserver(report)
    ro.observe(el)
    return () => { ro.disconnect(); reportBrowserViewGeometry(slot, null) }
  }, [ok, slot, frame, turn?.hasFrame])

  // Where the keyboard goes. In control (a Take over from the button, a click on
  // the frame, a second takeover after a hand back): the frame, so typing and Esc
  // reach the page at once, as on the chat card. Back from control, or a live page
  // shown again: the Take over button, but only when focus was inside this view
  // or nowhere, so a person who moved on is not pulled back.
  const canvasRef = useRef<BrowserLiveCanvasHandle>(null)
  const takeOverRef = useRef<HTMLButtonElement>(null)
  const wasInControl = useRef(inControl)
  const wasLive = useRef(live)
  const hasFrame = !!turn?.hasFrame
  const wasFramed = useRef(hasFrame)
  useEffect(() => {
    const before = { inControl: wasInControl.current, live: wasLive.current, framed: wasFramed.current }
    wasInControl.current = inControl
    wasLive.current = live
    wasFramed.current = hasFrame
    if (!ok) return
    // The frame mounts with the first picture, so a takeover before it focuses then.
    if (inControl && interactive && (!before.inControl || (hasFrame && !before.framed))) { canvasRef.current?.focus(); return }
    if (!live || inControl) return
    if (!before.inControl && before.live) return
    const active = typeof document === 'undefined' ? null : document.activeElement
    const focusIsOurs = !active || active === document.body || (!!rootEl && rootEl.contains(active))
    if (focusIsOurs) takeOverRef.current?.focus({ preventScroll: true })
  }, [ok, inControl, live, interactive, rootEl, hasFrame])

  const callbacks = useRef({ onTakeover, onHandBack, onNavigate })
  callbacks.current = { onTakeover, onHandBack, onNavigate }
  const { url, title } = view
  useEffect(() => {
    if (ok && url) callbacks.current.onNavigate?.({ url, title })
  }, [ok, url, title])

  const { claim: claimView, release: releaseView } = view
  // Told only once the gateway took the claim: the store shows in control at once
  // and rolls back on a refusal, and a failed hand back also returns to in control.
  const claim = useCallback(() => {
    void claimView().then((done) => { if (done) callbacks.current.onTakeover?.() })
  }, [claimView])
  const release = useCallback((said?: BrowserHandbackSaid) => {
    void releaseView(said).then((done) => {
      if (done) callbacks.current.onHandBack?.(said?.note || said?.choice || undefined)
    })
  }, [releaseView])
  const [reopenError, setReopenError] = useState<{ turnId: string; message: string } | null>(null)
  const reopen = useCallback(async (id: string) => {
    setReopenError(null)
    if (await browserLiveStore.reopen(slot, id)) return
    const rec = browserLiveStore.turn(slot, id)
    setReopenError({ turnId: id, message: rec?.controlError || i18nT('appSdk.browserView.tab_gone') })
  }, [slot])

  if (access === 'off') return <Placeholder state="off" text={i18nT('appSdk.browserView.not_enabled')} className={className} />
  if (access === 'app_off') return <Placeholder state="app_off" text={i18nT('appSdk.browserView.app_off')} className={className} />
  if (access === 'not_granted') return <Placeholder state="not_granted" text={i18nT('appSdk.browserView.not_granted')} className={className} />
  if (access === 'denied') return <Placeholder state="denied" text={i18nT('appSdk.browserView.denied')} className={className} />
  // Off the live view, a failed control write (take over, hand back, a hand-back
  // note) is shown on every face; nothing on those faces is unsaved.
  const controlNotice = turn?.controlError
    ? <ErrorNotice variant="inline" message={turn.controlError} askAgent testId="app-browser-view-control-error" />
    : null
  if (!turn || !live) {
    // A Show the live page the gateway refused, kept here because a closed tab
    // also clears the record's own error (`tab_gone`).
    if (turn && reopenError?.turnId === turn.turnId) {
      return (
        <div className={`rounded-md border border-border bg-card p-4 ${className ?? ''}`} data-testid="app-browser-view" data-state="reopen_failed">
          <ErrorNotice variant="inline" message={reopenError.message} askAgent testId="app-browser-view-reopen-error" />
        </div>
      )
    }
    // The browser failed (a denied start, a crashed engine): say so, in the
    // gateway's own words when it sent some. Nothing on this surface is unsaved.
    if (turn && turn.state === 'error') {
      return (
        <div className={`rounded-md border border-border bg-card p-4 ${className ?? ''}`} data-testid="app-browser-view" data-state="error">
          <ErrorNotice variant="inline" message={turn.detail || i18nT('appSdk.browserView.browser_error')} askAgent testId="app-browser-view-error" />
          {controlNotice}
        </div>
      )
    }
    // The Bot's turn is over but its tab is still open: one click streams it again
    // (the chat card's "Open live", the same owner route).
    if (turn && turn.tab === 'open' && browserLiveStore.isCurrentTurn(slot, turn.turnId)) {
      return (
        <div className={`grid place-items-center gap-3 rounded-md border border-border bg-card p-6 text-center text-[13px] text-muted ${className ?? ''}`} data-testid="app-browser-view" data-state="done">
          {controlNotice}
          <span>{i18nT('appSdk.browserView.turn_over')}</span>
          <Btn primary onClick={() => { void reopen(turn.turnId) }} data-testid="app-browser-view-show-live">
            {i18nT('appSdk.browserView.show_live')}
          </Btn>
        </div>
      )
    }
    if (controlNotice) {
      // A failed write after the tab closed (a hand-back note that did not go out)
      // still says so, whatever the tab and the browser now show.
      return (
        <div className={`grid gap-3 rounded-md border border-border bg-card p-4 text-[13px] text-muted ${className ?? ''}`} data-testid="app-browser-view" data-state={turn?.state ?? 'idle'}>
          {controlNotice}
        </div>
      )
    }
    return <Placeholder state="idle" text={i18nT('appSdk.browserView.no_page')} className={className} />
  }

  const host = hostOf(url)
  const ratio = frame && frame.w > 0 && frame.h > 0 ? frame.w / frame.h : 16 / 10
  const canInteract = interactive && inControl

  return (
    <div
      className={`flex flex-col min-h-0 ${className ?? ''}`}
      ref={setRootEl}
      data-testid="app-browser-view"
      data-state={turn.state}
      role="region"
      aria-label={ariaLabel}
    >
      <div className="flex items-center gap-2 px-2 py-1.5 text-[13px]">
        <span className="min-w-0 flex-1 truncate">
          <span className="font-medium text-text-strong">{host}</span>
          {title && <span className="text-muted"> · {sanitizeLlmOutput(title).slice(0, 120)}</span>}
        </span>
        {interactive && (inControl ? (
          <Btn primary onClick={() => release()} data-testid="app-browser-view-hand-back">
            {i18nT('appSdk.browserView.hand_back')}
          </Btn>
        ) : (
          <Btn primary ref={takeOverRef} onClick={claim} data-testid="app-browser-view-take-over">
            {i18nT('appSdk.browserView.take_over')}
          </Btn>
        ))}
      </div>
      {turn.controlError && (inControl ? (
        // No hand-off: the person holds the page and the hand-back bar below may
        // hold their unsent note; opening the chat would unmount both.
        <ErrorNotice variant="inline" message={turn.controlError} testId="app-browser-view-control-error" />
      ) : (
        <ErrorNotice variant="inline" message={turn.controlError} askAgent testId="app-browser-view-control-error" />
      ))}
      <div className="flex-1 min-h-0 flex">
        <PictureBox ratio={ratio} testId="app-browser-view-picture">
          <div ref={boxRef} className="absolute inset-0">
            {turn.hasFrame ? (
              <BrowserLiveCardCanvas
                ref={canvasRef}
                sessionKey={slot}
                turnId={turn.turnId}
                visible={shown}
                interactive={canInteract}
                onHandBack={() => release()}
                onClaim={interactive && !inControl ? claim : undefined}
                onFrameSize={setFrame}
                aria-label={ariaLabel}
              />
            ) : (
              <div className="absolute inset-0 grid place-items-center text-[13px] text-muted" role="status">
                {i18nT('appSdk.browserView.waiting_for_frame')}
              </div>
            )}
            {marks && marks.length > 0 && (
              <div className="pointer-events-none absolute inset-0" data-testid="app-browser-view-marks" aria-hidden="true">
                {marks.map((m, i) => (
                  <div
                    key={i}
                    className={m.width !== undefined && m.height !== undefined
                      ? 'absolute rounded-sm border-2 border-accent'
                      : 'absolute size-3 -translate-x-1/2 -translate-y-1/2 rounded-full bg-accent'}
                    style={{ left: m.x, top: m.y, ...(m.width !== undefined && m.height !== undefined ? { width: m.width, height: m.height } : {}) }}
                  >
                    {m.label && (
                      <span className="absolute left-full top-0 ml-1 whitespace-nowrap rounded bg-accent px-1 text-[11px] text-accent-fg">
                        {m.label}
                      </span>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        </PictureBox>
      </div>
      {inControl && interactive && (
        <BrowserLiveHandbackBar
          key={turn.turnId}
          offered={turn.handbackOptions}
          onHandBack={release}
          draft={browserLiveStore.handBackDraft(slot, turn.turnId)}
        />
      )}
    </div>
  )
}
