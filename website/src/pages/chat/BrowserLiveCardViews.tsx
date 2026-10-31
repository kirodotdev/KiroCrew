import { useRef, useState, type RefObject } from 'react'
import { motion, useReducedMotion } from 'framer-motion'
import {
  Check, ExternalLink, Globe, Hand, History, Lock, LogIn, Maximize2,
  Monitor, MoreHorizontal, MousePointer2, PanelRight, Play, ShieldCheck, Undo2, X,
} from 'lucide-react'

import { Btn } from '../../components/ui'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '../../components/ui/dropdown-menu'
import ErrorNotice from '../../components/ErrorNotice'
import { i18nT } from '../../i18n/t'
import { fmtDuration } from '../../i18n/format'
import { sanitizeLlmOutput } from '../../utils/sanitize'
import type { BrowserKind, BrowserLiveStep, BrowserLiveTurn } from '../../hooks/useBrowserLive'
import type { BrowserHandbackSaid } from '../../api/client/browserLive'
import type { FrameSize } from '../../hooks/useBrowserLive.input'
import { useRecordedThumb } from '../../hooks/useBrowserLive.replay'
import BrowserLiveCardCanvas, { type BrowserLiveCanvasHandle } from './BrowserLiveCardCanvas'
import { frameBoxStyle, useFrameMaxHeight } from './browserLiveFit'
import { BrowserLiveHandbackBar } from './BrowserLiveHandback'
import { BrowserLiveLoopLine, FoldedSteps, foldLoopRuns, useOpenRuns } from './BrowserLiveLoop'
import { QuickAskZoom } from './quickAsk/QuickAskParts'
import { useQuickAsk } from './quickAsk/useQuickAsk'

/**
 * The three faces of BrowserLiveCard. Each is the INSIDE of the card's one
 * motion element (the card owns the element and the state); these only draw.
 * Row geometry belongs to the card; these fill it.
 */

/** A page URL as the host a person reads, or the raw text when it is not a URL. */
export function hostOf(url: string): string {
  if (!url) return ''
  try {
    const u = new URL(url)
    return u.hostname || url
  } catch {
    return url
  }
}

function isHttps(url: string): boolean {
  try { return new URL(url).protocol === 'https:' } catch { return false }
}

/** "Your Chrome", "Edge", "Kiro's browser (headless)": which browser the frame shows. */
export function browserLabel(kind: BrowserKind | null, headless: boolean): string {
  switch (kind) {
    case 'user_chrome': return i18nT('pages.chat.browserLiveCard.browser_user_chrome')
    case 'edge': return i18nT('pages.chat.browserLiveCard.browser_edge')
    case 'kiro_chromium': return headless ? i18nT('pages.chat.browserLiveCard.browser_kiro_headless') : i18nT('pages.chat.browserLiveCard.browser_kiro')
    default: return i18nT('pages.chat.browserLiveCard.browser_unknown')
  }
}

/** A desktop record (computer use): no takeover, no tab, the application for a host. */
export const isDesktop = (turn: BrowserLiveTurn): boolean => turn.kind === 'computer'

/** What the picture shows, in a few words: the page's host, or the desktop application. */
export function placeOf(turn: BrowserLiveTurn): string {
  return isDesktop(turn) ? sanitizeLlmOutput(turn.title).slice(0, 96) : hostOf(turn.url)
}

/** Which surface the frame comes from: a browser by name, or the desktop. */
export function sourceLabel(turn: BrowserLiveTurn): string {
  return isDesktop(turn) ? i18nT('pages.chat.browserLiveCard.desktop') : browserLabel(turn.browser, turn.headless)
}

/** The window "Open in …" focuses; a headless browser has none. */
function openTarget(turn: BrowserLiveTurn): string | null {
  if (turn.browser === 'user_chrome') return i18nT('pages.chat.browserLiveCard.chrome')
  if (turn.browser === 'edge') return i18nT('pages.chat.browserLiveCard.edge')
  if (turn.browser === 'kiro_chromium' && !turn.headless) return i18nT('pages.chat.browserLiveCard.kiro_browser_short')
  return null
}

function title(turn: BrowserLiveTurn): string {
  return sanitizeLlmOutput(turn.title).slice(0, 120)
}

// ---- Waiting for Allow -------------------------------------------------------

// No "Switch to Chrome" here: until the person presses Allow there is no debugging
// connection and no tab to bring forward, so the card can only say where to look.
export function BrowserLiveAllowView({ turn, onUseKiroBrowser }: {
  turn: BrowserLiveTurn
  onUseKiroBrowser: () => void
}) {
  const browser = browserLabel(turn.browser, turn.headless)
  return (
    <div className="p-3" data-testid="browser-live-allow">
      <div className="flex items-center gap-2 rounded-lg border border-border bg-bg px-3 py-1.5 text-[13px]">
        <Globe size={14} aria-hidden="true" className="text-muted shrink-0" />
        <span className="font-medium text-text-strong truncate">{hostOf(turn.url)}</span>
        <span className="text-muted truncate">· {i18nT('pages.chat.browserLiveCard.connecting_to', { browser })}</span>
      </div>
      <div className="mt-4 mb-1 flex items-start gap-4 px-1">
        <div className="shrink-0 grid place-items-center size-16 rounded-xl bg-accent-subtle text-accent">
          <ShieldCheck size={30} aria-hidden="true" />
        </div>
        <div className="min-w-0 flex-1">
          <div className="text-[15px] font-semibold text-text-strong">{i18nT('pages.chat.browserLiveCard.wants_browser', { browser })}</div>
          <p className="mt-1 text-[13px] leading-5 text-muted">
            {i18nT('pages.chat.browserLiveCard.allow_explainer')} {i18nT('pages.chat.browserLiveCard.allow_asked_once')}
          </p>
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <Btn onClick={onUseKiroBrowser}>{i18nT('pages.chat.browserLiveCard.use_kiro_browser')}</Btn>
          </div>
        </div>
        <div className="shrink-0 self-center flex items-center gap-1.5 text-[12px] text-muted" role="status">
          <span aria-hidden="true" className="inline-block size-2 rounded-full bg-warn" />
          {i18nT('pages.chat.browserLiveCard.waiting_for_allow')}
        </div>
      </div>
    </div>
  )
}

// ---- Live and In control -----------------------------------------------------

export function BrowserLiveLiveView({ turn, onScreen, canvasRef, onClaim, onRelease, onOpenInBrowser, onExpand, onCloseLive }: {
  turn: BrowserLiveTurn
  onScreen: boolean
  canvasRef: RefObject<BrowserLiveCanvasHandle>
  onClaim: () => void
  /** Hand back; with what the person said on the hand-back bar, when they said it. */
  onRelease: (said?: BrowserHandbackSaid) => void
  onOpenInBrowser: () => void
  /** Show the picture big: the side panel's Preview tab, or a full-screen sheet. */
  onExpand: () => void
  /** A reopened view (the turn is over): fold it back to the done line. */
  onCloseLive?: () => void
}) {
  const reduce = useReducedMotion()
  const inControl = turn.state === 'in_control'
  const loginNeeded = turn.state === 'login_needed'
  // The desktop has no takeover: the card only shows it.
  const desktop = isDesktop(turn)
  const target = openTarget(turn)
  const pageTitle = desktop ? '' : title(turn)
  const step = turn.step ? sanitizeLlmOutput(turn.step.label).slice(0, 120) : ''
  const frameLabel = desktop
    ? i18nT('pages.chat.browserLiveCard.desktop_frame_label', { app: placeOf(turn) || sourceLabel(turn) })
    : i18nT('pages.chat.browserLiveCard.frame_label', { host: hostOf(turn.url) })
  const rootRef = useRef<HTMLDivElement>(null)
  const boxRef = useRef<HTMLDivElement>(null)
  const [frameSize, setFrameSize] = useState<FrameSize | null>(null)
  const maxHeight = useFrameMaxHeight(rootRef, boxRef)
  const [openRuns, toggleRun] = useOpenRuns()
  const quick = useQuickAsk(turn.sessionKey, turn.ask, 1)

  // Narrow-first: one column at every width. The header's address pill is the
  // only flexible item (min-w-0 + truncate), the browser chip leaves below `sm`,
  // the Take over label collapses to its icon below `sm`, and the step strip
  // wraps. The frame box takes the frame's own ratio and fits the visible
  // transcript height (`browserLiveFit`), so the whole page shows without scrolling.
  return (
    <div ref={rootRef} data-testid="browser-live-live">
      {/* Header */}
      <div className="flex flex-wrap items-center gap-2 px-3 py-2 border-b border-border min-h-[44px]">
        {inControl ? (
          <>
            <Hand size={16} aria-hidden="true" className="text-accent shrink-0" />
            <span className="text-[13px] font-semibold text-text-strong whitespace-nowrap">{i18nT('pages.chat.browserLiveCard.you_are_in_control')}</span>
            <span className="hidden sm:inline text-[12px] text-muted truncate min-w-0">{i18nT('pages.chat.browserLiveCard.kiro_paused')}</span>
            <span className="flex-1" />
            {target && (
              <Btn onClick={onOpenInBrowser} className="whitespace-nowrap shrink-0">
                <ExternalLink size={14} aria-hidden="true" />
                <span className="hidden sm:inline">{i18nT('pages.chat.browserLiveCard.open_in', { browser: target })}</span>
                <span className="sr-only sm:hidden">{i18nT('pages.chat.browserLiveCard.open_in', { browser: target })}</span>
              </Btn>
            )}
            <Btn primary onClick={() => onRelease()} className="whitespace-nowrap shrink-0">
              {i18nT('pages.chat.browserLiveCard.hand_back')}
              <kbd className="ml-0.5 rounded border border-accent-fg/40 px-1 py-px font-sans text-[10px] leading-4">Esc</kbd>
            </Btn>
          </>
        ) : (
          <>
            <span className="flex items-center gap-1.5 text-[12px] font-semibold text-ok whitespace-nowrap" role="status">
              <motion.span
                aria-hidden="true"
                className="inline-block size-2 rounded-full bg-ok"
                animate={reduce ? undefined : { opacity: [1, 0.35, 1] }}
                transition={reduce ? undefined : { duration: 1.6, repeat: Infinity, ease: 'easeInOut' }}
              />
              {i18nT('pages.chat.browserLiveCard.live')}
            </span>
            <div className="flex min-w-0 flex-1 items-center gap-2 rounded-md border border-border bg-bg px-2.5 py-1 text-[13px]">
              {desktop
                ? <Monitor size={13} aria-hidden="true" className="text-muted shrink-0" />
                : isHttps(turn.url)
                  ? <Lock size={13} aria-hidden="true" className="text-muted shrink-0" />
                  : <Globe size={13} aria-hidden="true" className="text-muted shrink-0" />}
              <span className="font-medium text-text-strong truncate">{placeOf(turn) || sourceLabel(turn)}</span>
              {pageTitle && <span className="text-muted truncate">· {pageTitle}</span>}
              <span className="flex-1" />
              <span className="hidden sm:flex items-center gap-1 text-[12px] text-muted whitespace-nowrap">
                <Monitor size={13} aria-hidden="true" />
                {sourceLabel(turn)}
              </span>
            </div>
            {!desktop && (
              <Btn primary onClick={onClaim} data-testid="browser-live-take-over">
                <Hand size={14} aria-hidden="true" />
                <span className="hidden sm:inline">{i18nT('pages.chat.browserLiveCard.take_over')}</span>
                <span className="sr-only sm:hidden">{i18nT('pages.chat.browserLiveCard.take_over')}</span>
              </Btn>
            )}
            {/* Two controls per row: Take over, then everything else behind one menu. */}
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Btn className="!px-1.5" aria-label={i18nT('pages.chat.browserLiveCard.more_actions')} title={i18nT('pages.chat.browserLiveCard.more_actions')}>
                  <MoreHorizontal size={14} aria-hidden="true" />
                </Btn>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" className="min-w-[190px]">
                <DropdownMenuItem onSelect={onExpand} data-testid="browser-live-expand">
                  <Maximize2 size={14} aria-hidden="true" />
                  {i18nT('pages.chat.browserLiveCard.expand')}
                </DropdownMenuItem>
                {target && (
                  <DropdownMenuItem onSelect={onOpenInBrowser}>
                    <ExternalLink size={14} aria-hidden="true" />
                    {i18nT('pages.chat.browserLiveCard.open_in', { browser: target })}
                  </DropdownMenuItem>
                )}
                {turn.turnOver && onCloseLive && (
                  <DropdownMenuItem onSelect={onCloseLive} data-testid="browser-live-close-live">
                    <X size={14} aria-hidden="true" />
                    {i18nT('pages.chat.browserLiveCard.close_live')}
                  </DropdownMenuItem>
                )}
              </DropdownMenuContent>
            </DropdownMenu>
          </>
        )}
      </div>

      {/* A control write the gateway refused: a hand back or a keystroke (the lock
          is still held, so the card stays in control and says why; Hand back above
          is the retry), or a take over (the card stays live and says why; Take over
          above is the retry). Without this the click would simply do nothing. */}
      {turn.controlError && (
        <div className="px-3 py-1.5 border-b border-border">
          <ErrorNotice variant="inline" message={turn.controlError} askAgent testId="browser-live-control-error" />
        </div>
      )}

      {loginNeeded && !inControl && (
        <div className="flex items-center gap-2 px-3 py-1.5 bg-accent-subtle text-[13px] text-text border-b border-border" role="status">
          <LogIn size={14} aria-hidden="true" className="text-accent shrink-0" />
          <span className="flex-1">{i18nT('pages.chat.browserLiveCard.take_over_to_sign_in')}</span>
          <Btn onClick={onClaim}>{i18nT('pages.chat.browserLiveCard.take_over')}</Btn>
        </div>
      )}

      {/* Frame */}
      <div ref={boxRef} data-testid="browser-live-frame-box" className={`group relative bg-card ${quick.zoom ? 'overflow-hidden' : ''}`} style={frameBoxStyle(frameSize, maxHeight)}>
        {turn.hasFrame ? (
          <QuickAskZoom zoom={quick.zoom}>
            <BrowserLiveCardCanvas
              ref={canvasRef}
              sessionKey={turn.sessionKey}
              turnId={turn.turnId}
              visible={onScreen}
              interactive={inControl || quick.scoped}
              onHandBack={() => onRelease()}
              onClaim={desktop || inControl || quick.scoped ? undefined : onClaim}
              onFrameSize={setFrameSize}
              aria-label={inControl || quick.scoped ? i18nT('pages.chat.browserLiveCard.frame_label_in_control', { host: hostOf(turn.url) }) : frameLabel}
            />
          </QuickAskZoom>
        ) : (
          <div className="absolute inset-0 grid place-items-center text-[13px] text-muted" role="status">
            {i18nT('pages.chat.browserLiveCard.waiting_for_first_frame')}
          </div>
        )}
        {!desktop && !inControl && !quick.scoped && turn.hasFrame && (
          // The hover cue. Pointer-events off: the click lands on the canvas.
          <div
            aria-hidden="true"
            className="pointer-events-none absolute inset-0 grid place-items-center bg-bg/40 opacity-0 transition-opacity duration-150 motion-reduce:transition-none group-hover:opacity-100"
          >
            <span className="flex items-center gap-1.5 rounded-full border border-border-strong bg-card/95 px-3 py-1.5 text-[13px] font-medium text-text-strong shadow-md">
              <Hand size={14} aria-hidden="true" />
              {i18nT('pages.chat.browserLiveCard.click_to_take_over')}
            </span>
          </div>
        )}
        {!inControl && step && (
          <div className="pointer-events-none absolute left-3 bottom-3 flex max-w-[80%] items-center gap-1.5 rounded-full bg-bg-elevated/95 border border-border px-3 py-1 text-[12px] text-text shadow-md" role="status">
            <MousePointer2 size={12} aria-hidden="true" className="text-accent shrink-0" />
            <span className="truncate">{step}</span>
          </div>
        )}
        {!inControl && quick.frameOverlay}
      </div>

      {(!inControl || quick.scoped) && quick.askSlot}

      {inControl && !quick.scoped && <BrowserLiveHandbackBar offered={turn.handbackOptions} onHandBack={onRelease} />}

      {!inControl && turn.loop && <BrowserLiveLoopLine loop={turn.loop} className="px-3 py-2 border-t border-border" />}

      {/* Step strip */}
      {!inControl && turn.steps.length > 0 && (
        <ol className="flex flex-wrap items-center gap-1.5 px-3 py-2 border-t border-border" aria-label={i18nT('pages.chat.browserLiveCard.steps')}>
          {foldLoopRuns(turn.steps, st => !!st.loop, openRuns).map(entry => entry.kind === 'folded'
            ? <li key={`f${entry.run}`}><FoldedSteps count={entry.count} onOpen={() => toggleRun(entry.run)} /></li>
            : <StepChip key={`${entry.index}-${entry.item.label}`} step={entry.item} />)}
        </ol>
      )}
    </div>
  )
}

function StepChip({ step: s }: { step: BrowserLiveStep }) {
  return (
    <li
      aria-current={s.status === 'active' ? 'step' : undefined}
      className={`flex items-center gap-1 rounded-full border px-2 py-0.5 text-[12px] leading-4 ${
        s.status === 'done' ? 'border-border text-text'
        : s.status === 'active' ? 'border-accent text-accent bg-accent-subtle'
        : 'border-border text-muted'
      }`}
    >
      {s.status === 'done' && <Check size={11} aria-hidden="true" className="text-ok" />}
      {s.status === 'active' && <span aria-hidden="true" className="inline-block size-1.5 rounded-full bg-accent" />}
      <span className="truncate max-w-[180px]">{sanitizeLlmOutput(s.label).slice(0, 60)}</span>
    </li>
  )
}

// ---- Done ----------------------------------------------------------------------

function durationLabel(turn: BrowserLiveTurn): string {
  const ms = Math.max(0, (turn.endedAt ?? Date.now()) - turn.startedAt)
  const s = Math.round(ms / 1000)
  if (s < 60) return fmtDuration([[s, 'second']])
  return fmtDuration([[Math.floor(s / 60), 'minute'], [s % 60, 'second']], { dropZero: true })
}

export function BrowserLiveDoneRow({ turn, onScreen, canvasRef, onReplay, onOpenInBrowser, canOpenLive, onOpenLive }: {
  turn: BrowserLiveTurn
  onScreen: boolean
  canvasRef: RefObject<BrowserLiveCanvasHandle>
  /** Open the big view on this turn's replay, from its first step. */
  onReplay: () => void
  onOpenInBrowser: () => void
  /** The tab is still open and this is the session's newest card: "Open live" works. */
  canOpenLive: boolean
  onOpenLive: () => void
}) {
  const count = turn.steps.length
  const desktop = isDesktop(turn)
  const target = openTarget(turn)
  const host = placeOf(turn)
  const parts = [
    i18nT('pages.chat.browserLiveCard.steps_count', { count }),
    durationLabel(turn),
    sourceLabel(turn),
  ]
  const Icon = desktop ? Monitor : Globe
  const headline = desktop
    ? host ? i18nT('pages.chat.browserLiveCard.used_app', { app: host }) : i18nT('pages.chat.browserLiveCard.used_desktop')
    : host ? i18nT('pages.chat.browserLiveCard.browsed_host', { host }) : i18nT('pages.chat.browserLiveCard.browsed')
  if (turn.tab === 'closed') parts.push(i18nT('pages.chat.browserLiveCard.tab_closed'))
  else if (turn.browser === 'user_chrome' || turn.browser === 'edge') parts.push(i18nT('pages.chat.browserLiveCard.tab_kept_open'))
  // The thumbnail is a shortcut to the first button: Open live when the tab is
  // still there, else Replay.
  // No frame in this page's memory (rebuilt after a reload or reconnect, or the
  // frames went to the recorder while no card showed): the recording's last one.
  // A desktop record has no gateway recording to ask for.
  const recorded = useRecordedThumb(turn.sessionKey, turn.turnId, !desktop && !turn.hasFrame && onScreen)
  // A desktop record's only replay is the frames this page kept, which an older
  // record no longer has (`evictRings`).
  const pictured = desktop ? turn.ring.length > 0 : turn.hasFrame
  const replayable = pictured || (!desktop && !!recorded)
  const thumbAction = canOpenLive ? onOpenLive : replayable ? onReplay : undefined
  const thumbLabel = canOpenLive
    ? i18nT('pages.chat.browserLiveCard.open_live')
    : i18nT('pages.chat.browserLiveCard.replay_steps', { count })

  // Narrow-first: the thumbnail is 112px wide on a phone and 200px from `sm`;
  // the text column truncates; the two buttons wrap under it when they must.
  return (
    <div className="flex items-stretch gap-3 p-2" data-testid="browser-live-done">
      <div className="relative shrink-0 w-[112px] sm:w-[200px] overflow-hidden rounded-md border border-border bg-bg" style={{ aspectRatio: '16 / 10' }}>
        {thumbAction && (
          <button
            type="button"
            onClick={thumbAction}
            aria-label={thumbLabel}
            title={thumbLabel}
            data-testid="browser-live-done-thumb"
            // A pointer shortcut for the Open live button beside it: out of the
            // tab order so a keyboard user meets that action once.
            tabIndex={-1}
            className="absolute inset-0 z-10 cursor-pointer rounded-md focus-ring-accent-inset"
          />
        )}
        {pictured ? (
          <BrowserLiveCardCanvas
            ref={canvasRef}
            sessionKey={turn.sessionKey}
            turnId={turn.turnId}
            visible={onScreen}
            interactive={false}
            aria-label={i18nT('pages.chat.browserLiveCard.last_frame_label', { host })}
          />
        ) : recorded ? (
          <img
            src={recorded}
            alt={i18nT('pages.chat.browserLiveCard.last_frame_label', { host })}
            className="absolute inset-0 h-full w-full object-cover object-top"
            data-testid="browser-live-done-recorded"
          />
        ) : (
          <div className="absolute inset-0 grid place-items-center text-muted"><Icon size={22} aria-hidden="true" /></div>
        )}
      </div>
      <div className="min-w-0 flex-1 flex flex-col justify-center gap-1 py-1">
        <div className="flex items-center gap-2 text-[14px] font-semibold text-text-strong">
          <Icon size={15} aria-hidden="true" className="text-muted shrink-0" />
          <span className="truncate">{headline}</span>
        </div>
        <div className="text-[12px] text-muted truncate">{parts.join(' · ')}</div>
        {turn.state === 'error' && (
          <ErrorNotice variant="inline" message={turn.detail || i18nT('pages.chat.browserLiveCard.error_generic')} askAgent />
        )}
        {turn.controlError && (
          <ErrorNotice variant="inline" message={turn.controlError} askAgent testId="browser-live-control-error" />
        )}
        {/* Two actions, never three: Open live (whose live view carries "Open in
            Chrome" in its menu) and Replay; else Replay and Open in. */}
        <div className="mt-1 flex flex-wrap items-center gap-2" data-testid="browser-live-done-actions">
          {canOpenLive && (
            <Btn primary onClick={onOpenLive} data-testid="browser-live-open-live">
              <Hand size={13} aria-hidden="true" />
              {i18nT('pages.chat.browserLiveCard.open_live')}
            </Btn>
          )}
          {replayable && (
            <Btn onClick={onReplay} data-testid="browser-live-replay">
              <Play size={13} aria-hidden="true" />
              {i18nT('pages.chat.browserLiveCard.replay_steps', { count })}
            </Btn>
          )}
          {target && !canOpenLive && (
            <Btn onClick={onOpenInBrowser} className="border-transparent">
              <ExternalLink size={13} aria-hidden="true" />
              {i18nT('pages.chat.browserLiveCard.open_in', { browser: target })}
            </Btn>
          )}
        </div>
      </div>
    </div>
  )
}

// ---- Shown big elsewhere ------------------------------------------------------

/**
 * The card while its picture is in the big view: one line saying what it shows
 * and where, with Bring back. Same motion element as every other face, so the
 * fold reads as the card shrinking, not as a swap.
 */
export function BrowserLiveStagedRow({ turn, replaying, surface, onBringBack }: {
  turn: BrowserLiveTurn
  replaying: boolean
  surface: 'panel' | 'sheet'
  onBringBack: () => void
}) {
  const reduce = useReducedMotion()
  const inControl = turn.state === 'in_control'
  const live = !replaying && (turn.state === 'live' || inControl || turn.state === 'login_needed')
  const where = surface === 'panel'
    ? i18nT('pages.chat.browserLiveCard.showing_in_side_panel')
    : i18nT('pages.chat.browserLiveCard.showing_full_screen')
  return (
    <div className="flex items-center gap-2 px-3 py-2 min-h-[44px]" data-testid="browser-live-staged">
      {live ? (
        <span className="flex items-center gap-1.5 text-[12px] font-semibold text-ok whitespace-nowrap" role="status">
          <motion.span
            aria-hidden="true"
            className="inline-block size-2 rounded-full bg-ok"
            animate={reduce ? undefined : { opacity: [1, 0.35, 1] }}
            transition={reduce ? undefined : { duration: 1.6, repeat: Infinity, ease: 'easeInOut' }}
          />
          {inControl ? i18nT('pages.chat.browserLiveCard.you_are_in_control') : i18nT('pages.chat.browserLiveCard.live')}
        </span>
      ) : (
        <span className="flex items-center gap-1.5 text-[12px] font-semibold text-text whitespace-nowrap">
          <History size={13} aria-hidden="true" className="text-muted" />
          {i18nT('pages.chat.browserLiveCard.replaying')}
        </span>
      )}
      <span className="min-w-0 flex-1 flex items-center gap-1.5 text-[13px] text-muted">
        <PanelRight size={13} aria-hidden="true" className="shrink-0" />
        <span className="truncate">{placeOf(turn) ? `${placeOf(turn)} · ${where}` : where}</span>
      </span>
      <Btn onClick={onBringBack} className="shrink-0" data-testid="browser-live-bring-back">
        <Undo2 size={13} aria-hidden="true" />
        {i18nT('pages.chat.browserLiveStage.bring_back')}
      </Btn>
    </div>
  )
}
