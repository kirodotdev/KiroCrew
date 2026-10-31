import { useCallback, useEffect, useId, useMemo, useRef, useState, type KeyboardEvent } from 'react'
import { motion, useReducedMotion } from 'framer-motion'
import { ChevronDown, ExternalLink, Globe, Hand, History, Lock, Maximize, Minimize, Play } from 'lucide-react'

import { Btn } from '../../components/ui'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '../../components/ui/dropdown-menu'
import ErrorNotice from '../../components/ErrorNotice'
import { i18nT } from '../../i18n/t'
import { fmtNumber, fmtTime } from '../../i18n/format'
import { sanitizeLlmOutput } from '../../utils/sanitize'
import {
  browserLiveStore, useBrowserLiveCurrent, useBrowserLiveSessionTurns, useBrowserLiveTurnById,
  type BrowserLiveTurn,
} from '../../hooks/useBrowserLive'
import type { FrameSize } from '../../hooks/useBrowserLive.input'
import { activeEvent, currentStep, frameAt, frameForAction, prevOffset, useRecording, useRecordingList, type RecordingSummary } from '../../hooks/useBrowserLive.replay'
import { browserStageStore, type BrowserStage } from '../../hooks/useBrowserLive.stage'
import BrowserLiveCardCanvas, { type BrowserLiveCanvasHandle } from './BrowserLiveCardCanvas'
import { browserLabel, hostOf } from './BrowserLiveCardViews'
import { PictureBox, ReplayCanvas, ReplayOverlay } from './BrowserLiveReplayStage'
import { BrowserLiveHandbackBar } from './BrowserLiveHandback'
import { BrowserLiveLoopLine } from './BrowserLiveLoop'
import type { BrowserHandbackSaid } from '../../api/client/browserLive'
import { ReplayStepList, ReplayTimeline, replayKeyAction, useReplayPlayer } from './BrowserLiveTimeline'
import { QuickAskZoom } from './quickAsk/QuickAskParts'
import { useQuickAsk } from './quickAsk/useQuickAsk'

/**
 * The agent's browser, big: the side panel's "Agent browser" view and the
 * phone's full-screen sheet are both this component.
 *
 * Two modes over one turn. LIVE paints the screencast through the same canvas
 * the chat card uses, so Take over, Hand back, Esc, the password boxes and the
 * frame-ratio fit all work exactly as on the card. REPLAY paints the turn's
 * recording at the play head, with the action at that moment drawn over it, a
 * scrubber with one marker per action, a step list, play/pause, step back and
 * forward, and 1x/2x/4x. Scrubbing or clicking a step while live switches to
 * replay; Live comes back to now. A finished turn is always replay.
 *
 * Full screen goes through the Fullscreen API on this view's root (Esc leaves
 * it, by the browser's own rule); the step list moves to a right-hand column
 * there. Space plays and pauses, the arrow keys step, Home and End jump.
 *
 * Visibility: while shown, this view reports itself on screen for the turn it
 * shows, so the screencast keeps running for it even with the chat card folded
 * to its placeholder.
 */

const RUNNING = new Set(['waiting_allow', 'live', 'in_control', 'login_needed'])
const isRunning = (t: BrowserLiveTurn | null) => !!t && RUNNING.has(t.state) && !(t.turnOver && t.state === 'waiting_allow')

function useFullscreen(ref: React.RefObject<HTMLElement>) {
  const [on, setOn] = useState(false)
  useEffect(() => {
    const sync = () => setOn(!!ref.current && document.fullscreenElement === ref.current)
    document.addEventListener('fullscreenchange', sync)
    return () => document.removeEventListener('fullscreenchange', sync)
  }, [ref])
  const supported = typeof document !== 'undefined' && document.fullscreenEnabled !== false
    && typeof (ref.current ?? (typeof HTMLElement !== 'undefined' ? HTMLElement.prototype : null))?.requestFullscreen === 'function'
  const toggle = useCallback(() => {
    const el = ref.current
    if (!el) return
    if (document.fullscreenElement === el) { void document.exitFullscreen?.().catch(() => {}); return }
    void el.requestFullscreen?.().catch(() => {})
  }, [ref])
  return { on, supported, toggle }
}

interface TurnChoice { turnId: string; startedTs: number; host: string }

/** The turn picker's rows: the gateway's list when it has one, merged with this page's turns. */
export function turnChoices(server: RecordingSummary[] | null | undefined, local: readonly BrowserLiveTurn[]): TurnChoice[] {
  const by = new Map<string, TurnChoice>()
  for (const t of local) by.set(t.turnId, { turnId: t.turnId, startedTs: t.startedAt, host: hostOf(t.url) })
  for (const r of server ?? []) {
    const prev = by.get(r.turnId)
    by.set(r.turnId, { turnId: r.turnId, startedTs: r.startedTs, host: prev?.host || hostOf(r.startUrl) })
  }
  return [...by.values()].sort((a, b) => a.startedTs - b.startedTs)
}

export interface BrowserLiveStageProps {
  sessionKey: string
  stage: BrowserStage
  /** The view is on screen (its tab is the selected one, its panel open). */
  active: boolean
}

export default function BrowserLiveStage({ sessionKey, stage, active }: BrowserLiveStageProps) {
  const reduce = useReducedMotion()
  const instanceId = useId()
  const rootRef = useRef<HTMLDivElement>(null)
  const canvasRef = useRef<BrowserLiveCanvasHandle>(null)
  const current = useBrowserLiveCurrent(sessionKey)
  const turnId = stage.turnId ?? current?.turnId ?? null
  const turn = useBrowserLiveTurnById(sessionKey, turnId)
  const running = isRunning(turn) && !turn?.turnOver
  const reopenedLive = !!turn?.turnOver && !!turn && (turn.state === 'live' || turn.state === 'in_control')
  const canLive = running || reopenedLive
  const mode = stage.mode === 'live' && canLive ? 'live' : 'replay'
  const inControl = turn?.state === 'in_control'
  const fs = useFullscreen(rootRef)

  // On screen for this turn, so the screencast keeps feeding it.
  useEffect(() => {
    if (!turnId) return
    browserLiveStore.setVisible(sessionKey, instanceId, active, turnId)
    return () => browserLiveStore.setVisible(sessionKey, instanceId, false, turnId)
  }, [sessionKey, instanceId, active, turnId])

  useEffect(() => { if (inControl && mode === 'live') canvasRef.current?.focus() }, [inControl, mode])

  const sessionTurns = useBrowserLiveSessionTurns(sessionKey)
  const list = useRecordingList(sessionKey, active)
  const choices = useMemo(() => turnChoices(list.data, sessionTurns), [list.data, sessionTurns])
  const { recording, loading, error: recordingError } = useRecording(sessionKey, turnId, turn, active)
  const queryErrors = (
    <>
      {recordingError && <ErrorNotice variant="inline" message={recordingError.message} askAgent testId="browser-live-stage-recording-error" />}
      {list.error && <ErrorNotice variant="inline" message={list.error.message} askAgent testId="browser-live-stage-recordings-error" />}
    </>
  )
  // A fresh Replay (nonce) or another turn starts from the beginning; a live view
  // parks the head at the end, so the first scrub reads as "look back".
  const restartKey = `${stage.nonce}:${turnId ?? ''}`
  const player = useReplayPlayer(recording, restartKey, mode === 'live' ? 'end' : 'start')

  const claim = useCallback(() => { void browserLiveStore.claim(sessionKey) }, [sessionKey])
  const release = useCallback((said?: BrowserHandbackSaid) => { void browserLiveStore.release(sessionKey, said) }, [sessionKey])
  const openInBrowser = useCallback(() => browserLiveStore.openInBrowser(sessionKey), [sessionKey])
  const goLive = useCallback(() => browserStageStore.update(sessionKey, { mode: 'live' }), [sessionKey])
  const seekStep = useCallback((offset: number) => {
    if (mode === 'live') browserStageStore.update(sessionKey, { mode: 'replay', turnId })
    player.pause()
    player.seek(offset)
  }, [mode, sessionKey, turnId, player])
  const pickTurn = useCallback((id: string) => {
    const t = browserLiveStore.turn(sessionKey, id)
    browserStageStore.update(sessionKey, { turnId: id, mode: isRunning(t) && !t?.turnOver ? 'live' : 'replay' })
  }, [sessionKey])

  const onKeyDown = useCallback((e: KeyboardEvent<HTMLDivElement>) => {
    if (inControl && mode === 'live') return
    // The turn picker's menu (portalled, still in this React tree) owns its own keys.
    if ((e.target as HTMLElement).closest?.('[role="menu"]')) return
    const tag = (e.target as HTMLElement).tagName
    if (tag === 'INPUT' && (e.key === 'ArrowLeft' || e.key === 'ArrowRight')) return
    if ((tag === 'BUTTON' || tag === 'INPUT') && e.key === ' ') return
    const act = replayKeyAction(e.key)
    if (!act || !recording) return
    e.preventDefault()
    // Any replay key while live leaves live for the same turn's replay.
    if (mode === 'live') {
      browserStageStore.update(sessionKey, { mode: 'replay', turnId })
      if (act === 'toggle') { player.seek(0); player.play() } else if (act === 'prev') player.seek(prevOffset(recording, player.duration))
      else if (act === 'start') player.seek(0)
      return
    }
    if (act === 'toggle') player.toggle()
    else if (act === 'next') player.next()
    else if (act === 'prev') player.prev()
    else if (act === 'start') player.seek(0)
    else player.seek(player.duration)
  }, [inControl, mode, recording, player, sessionKey, turnId])

  const [frameSize, setFrameSize] = useState<FrameSize | null>(null)
  // The big view outranks the card for the ask's sheet while it is showing.
  const quick = useQuickAsk(sessionKey, mode === 'live' ? turn?.ask ?? null : null, active ? 2 : 0)
  if (!turn && !recording) {
    return (
      <div className="h-full grid place-items-center p-6 text-center text-[13px] text-muted" data-testid="browser-live-stage-empty">
        {queryErrors}
        {loading ? null : i18nT('pages.chat.browserLiveStage.nothing_yet')}
      </div>
    )
  }

  const url = turn?.url || [...(recording?.events ?? [])].reverse().find(e => e.url)?.url || ''
  const host = hostOf(url)
  const title = turn ? sanitizeLlmOutput(turn.title).slice(0, 120) : ''
  const space = recording && recording.width > 0 ? { w: recording.width, h: recording.height } : (frameSize ?? { w: 16, h: 10 })
  const liveRatio = frameSize && frameSize.w > 0 ? frameSize.w / frameSize.h : 16 / 10
  const replayRatio = space.w > 0 && space.h > 0 ? space.w / space.h : 16 / 10
  const evIdx = recording && mode === 'replay' ? activeEvent(recording, player.t) : -1
  // A drawn point belongs to the page as it was after the action scrolled its
  // target into view, so that frame shows while the mark is up.
  const frameIdx = !recording ? -1
    : evIdx >= 0 && recording.events[evIdx].point ? frameForAction(recording, player.t, evIdx)
    : frameAt(recording, player.t)
  // An event's point is in the pixels of the frame that was streaming when it
  // happened (the gateway can step the stream's size down mid-turn), while the
  // canvas stretches every frame to the recording's last size. So the overlay is
  // placed in the showing frame's own space, which is a share of the same box.
  const shown = recording && frameIdx >= 0 ? recording.frames[frameIdx] : null
  const ev = evIdx >= 0 && recording ? recording.events[evIdx] : null
  const overlaySpace = ev?.fw && ev.fh ? { w: ev.fw, h: ev.fh }
    : shown && shown.w > 0 && shown.h > 0 ? { w: shown.w, h: shown.h } : space
  const stepIdx = recording ? currentStep(recording, mode === 'live' ? player.duration : player.t) : -1
  const currentChoice = choices.findIndex(c => c.turnId === turnId)
  const showSteps = !!recording && !(inControl && mode === 'live')

  return (
    // The region owns the replay's keyboard shortcuts (Space, arrows, Home, End)
    // for whatever control inside it has focus; it is not itself a control.
    // eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions
    <div
      ref={rootRef}
      className={`flex flex-col h-full min-h-0 bg-bg outline-none ${fs.on ? 'p-2' : ''}`}
      data-testid="browser-live-stage"
      data-mode={mode}
      data-fullscreen={fs.on ? 'true' : undefined}
      tabIndex={-1}
      onKeyDown={onKeyDown}
      role="region"
      aria-label={i18nT('pages.chat.browserLiveStage.region_label', { host: host || i18nT('pages.chat.browserLiveCard.browsed') })}
    >
      {/* Header */}
      <div className="flex items-center gap-2 px-3 py-2 border-b border-border min-h-[44px] shrink-0" data-stage-header="">
        {mode === 'live' ? (
          inControl ? (
            <span className="flex items-center gap-1.5 text-[13px] font-semibold text-text-strong whitespace-nowrap">
              <Hand size={15} aria-hidden="true" className="text-accent" />
              {i18nT('pages.chat.browserLiveCard.you_are_in_control')}
            </span>
          ) : (
            <span className="flex items-center gap-1.5 text-[12px] font-semibold text-ok whitespace-nowrap" role="status">
              <motion.span
                aria-hidden="true"
                className="inline-block size-2 rounded-full bg-ok"
                animate={reduce ? undefined : { opacity: [1, 0.35, 1] }}
                transition={reduce ? undefined : { duration: 1.6, repeat: Infinity, ease: 'easeInOut' }}
              />
              {i18nT('pages.chat.browserLiveCard.live')}
            </span>
          )
        ) : (
          <span className="flex items-center gap-1.5 text-[12px] font-semibold text-text whitespace-nowrap" data-testid="browser-live-stage-replay-badge">
            <History size={14} aria-hidden="true" className="text-muted" />
            {i18nT('pages.chat.browserLiveStage.replay')}
          </span>
        )}
        <div className="flex min-w-0 flex-1 items-center gap-2 rounded-md border border-border bg-bg-elevated px-2.5 py-1 text-[13px]">
          {url.startsWith('https:') ? <Lock size={13} aria-hidden="true" className="text-muted shrink-0" /> : <Globe size={13} aria-hidden="true" className="text-muted shrink-0" />}
          {/* The host is what the person reads first, so it keeps its width; the
              title and the browser's name give way in a narrow panel. */}
          <span className="font-medium text-text-strong whitespace-nowrap shrink-0 max-w-[60%] overflow-hidden text-ellipsis">{host}</span>
          {title && <span className="text-muted truncate min-w-0">· {title}</span>}
          {turn && <span className="hidden md:inline text-[12px] text-muted truncate min-w-0 ml-auto">{browserLabel(turn.browser, turn.headless)}</span>}
        </div>
        {mode === 'live' && (inControl ? (
          <Btn primary onClick={() => release()} className="whitespace-nowrap shrink-0" data-testid="browser-live-stage-hand-back">
            {i18nT('pages.chat.browserLiveCard.hand_back')}
            <kbd className="ml-0.5 rounded border border-accent-fg/40 px-1 py-px font-sans text-[10px] leading-4">Esc</kbd>
          </Btn>
        ) : (
          <Btn primary onClick={claim} className="shrink-0" data-testid="browser-live-stage-take-over">
            <Hand size={14} aria-hidden="true" />
            <span className="hidden sm:inline">{i18nT('pages.chat.browserLiveCard.take_over')}</span>
            <span className="sr-only sm:hidden">{i18nT('pages.chat.browserLiveCard.take_over')}</span>
          </Btn>
        ))}
        {fs.supported && (
          <Btn
            className="!px-1.5 shrink-0"
            onClick={fs.toggle}
            aria-pressed={fs.on}
            aria-label={fs.on ? i18nT('pages.chat.browserLiveStage.exit_full_screen') : i18nT('pages.chat.browserLiveStage.full_screen')}
            title={fs.on ? i18nT('pages.chat.browserLiveStage.exit_full_screen') : i18nT('pages.chat.browserLiveStage.full_screen')}
            data-testid="browser-live-stage-fullscreen"
          >
            {fs.on ? <Minimize size={14} aria-hidden="true" /> : <Maximize size={14} aria-hidden="true" />}
          </Btn>
        )}
        {/* Two controls in this row at most (AUTOSDE `max-two-buttons-per-row`):
            Bring back lives on the Preview tab's mode row, the sheet has its own close. */}
      </div>

      {(recordingError || list.error) && (
        <div className="px-3 py-1.5 border-b border-border shrink-0">{queryErrors}</div>
      )}

      {turn?.controlError && (
        <div className="px-3 py-1.5 border-b border-border shrink-0">
          <ErrorNotice variant="inline" message={turn.controlError} askAgent testId="browser-live-stage-control-error" />
        </div>
      )}

      {/* Fullscreen puts the steps beside the picture only when there is room
          for both (md and up); a narrow screen keeps them stacked below it. */}
      <div className={`flex-1 min-h-0 flex ${fs.on ? 'flex-col md:flex-row' : 'flex-col'}`} data-testid="browser-live-stage-body">
        <div className="flex-1 min-h-0 min-w-0 flex flex-col">
          {/* Picture */}
          <div className="flex-1 min-h-0 flex p-2.5">
            {mode === 'live' && turn ? (
              <PictureBox ratio={liveRatio} testId="browser-live-stage-picture">
                {turn.hasFrame ? (
                  <QuickAskZoom zoom={quick.zoom}>
                    <BrowserLiveCardCanvas
                      ref={canvasRef}
                      sessionKey={sessionKey}
                      turnId={turn.turnId}
                      visible={active}
                      interactive={inControl || quick.scoped}
                      onHandBack={() => release()}
                      onClaim={inControl || quick.scoped ? undefined : claim}
                      onFrameSize={setFrameSize}
                      aria-label={inControl || quick.scoped
                        ? i18nT('pages.chat.browserLiveCard.frame_label_in_control', { host })
                        : i18nT('pages.chat.browserLiveCard.frame_label', { host })}
                    />
                  </QuickAskZoom>
                ) : (
                  <div className="absolute inset-0 grid place-items-center text-[13px] text-muted" role="status">
                    {i18nT('pages.chat.browserLiveCard.waiting_for_first_frame')}
                  </div>
                )}
                {!inControl && quick.frameOverlay}
              </PictureBox>
            ) : recording && recording.frames.length > 0 ? (
              <PictureBox ratio={replayRatio} testId="browser-live-stage-picture">
                <ReplayCanvas rec={recording} index={frameIdx} label={i18nT('pages.chat.browserLiveStage.replay_frame_label', { host })} />
                <ReplayOverlay event={evIdx >= 0 ? recording.events[evIdx] : null} space={overlaySpace} />
              </PictureBox>
            ) : (
              <div className="flex-1 grid place-items-center text-[13px] text-muted" role="status" data-testid="browser-live-stage-no-frames">
                {loading ? null : i18nT('pages.chat.browserLiveStage.no_frames')}
              </div>
            )}
          </div>

          {mode === 'live' && (!inControl || quick.scoped) && quick.askSlot}
          {inControl && !quick.scoped && mode === 'live' && turn && (
            <BrowserLiveHandbackBar offered={turn.handbackOptions} onHandBack={release} className="shrink-0" />
          )}
          {mode === 'live' && !inControl && turn?.loop && (
            <BrowserLiveLoopLine loop={turn.loop} className="px-3 py-2 border-t border-border shrink-0" />
          )}

          {/* Timeline: hidden while the person holds the browser (only "now" matters then). */}
          {recording && !(inControl && mode === 'live') && (
            <ReplayTimeline
              rec={recording}
              player={mode === 'live' ? {
                ...player,
                // Live: the head sits at "now"; any control leaves live for this turn's replay.
                t: player.duration,
                playing: false,
                toggle: () => { browserStageStore.update(sessionKey, { mode: 'replay', turnId }); player.seek(0); player.play() },
                prev: () => { browserStageStore.update(sessionKey, { mode: 'replay', turnId }); player.seek(prevOffset(recording, player.duration)) },
                next: () => {},
              } : player}
              live={canLive && mode === 'replay'}
              onLive={goLive}
              onSeekStep={seekStep}
            />
          )}

          <div className="flex items-center gap-2 px-3 pb-2 shrink-0">
            {choices.length > 1 && (
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <Btn className="text-[12px]" data-testid="browser-live-stage-turns">
                    {currentChoice >= 0
                      ? i18nT('pages.chat.browserLiveStage.turn_n_at', { n: fmtNumber(currentChoice + 1), time: fmtTime(choices[currentChoice].startedTs) })
                      : i18nT('pages.chat.browserLiveStage.pick_turn')}
                    <ChevronDown size={13} aria-hidden="true" />
                  </Btn>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="start" className="min-w-[220px]">
                  {choices.map((c, i) => (
                    <DropdownMenuItem key={c.turnId} onSelect={() => pickTurn(c.turnId)} aria-current={c.turnId === turnId ? 'true' : undefined}>
                      <Play size={13} aria-hidden="true" />
                      <span className="flex-1 truncate">{i18nT('pages.chat.browserLiveStage.turn_n_at', { n: fmtNumber(i + 1), time: fmtTime(c.startedTs) })}</span>
                      {c.host && <span className="text-[11px] text-muted truncate max-w-[120px]">{c.host}</span>}
                    </DropdownMenuItem>
                  ))}
                </DropdownMenuContent>
              </DropdownMenu>
            )}
            <span className="flex-1" />
            {turn && (turn.browser === 'user_chrome' || turn.browser === 'edge' || (turn.browser === 'kiro_chromium' && !turn.headless)) && mode === 'live' && (
              <Btn onClick={openInBrowser} className="text-[12px] border-transparent">
                <ExternalLink size={13} aria-hidden="true" />
                {i18nT('pages.chat.browserLiveStage.open_real_window')}
              </Btn>
            )}
          </div>
        </div>

        {showSteps && recording && (
          <div className={fs.on ? 'max-h-[34%] shrink-0 border-t border-border flex flex-col min-h-0 md:max-h-none md:w-[320px] md:border-t-0 md:border-l' : 'max-h-[34%] shrink-0 border-t border-border flex flex-col min-h-0'}>
            <div className="px-3 pt-2 pb-1 text-[12px] font-semibold text-text-strong">
              {i18nT('pages.chat.browserLiveStage.steps_n', { count: recording.events.length })}
            </div>
            <ReplayStepList rec={recording} current={stepIdx} onSeekStep={seekStep} className="flex-1 min-h-0" />
          </div>
        )}
      </div>
    </div>
  )
}
