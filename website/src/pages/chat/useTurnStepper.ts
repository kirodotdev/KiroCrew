import { type Dispatch, type RefObject, type SetStateAction, useEffect, useRef, useState } from 'react'

import type { ChatSection } from '../../hooks/useChatNavigation'
import { i18nT } from '../../i18n/t'
import { isTranscriptRow } from './usePinnedPrompt'
import { readingPosition, registerTurnStepper, turnStepTarget, type MountedRow } from './turnStep'

export interface UseTurnStepperOptions {
  /** True while the single-chat transcript is on screen and is the session the keyboard acts on. */
  active: boolean
  /** The transcript's turns in order — the same list the turn minimap draws. */
  turns: readonly ChatSection[]
  /** True while older history is not loaded yet, so positions count loaded turns only. */
  windowed: boolean
  /** The transcript scroll container. Rows inside it carry `data-display-index`. */
  scrollerRef: RefObject<HTMLElement | null>
  /** Distance below the scroller's top edge at which a jump lands a row (usePinnedPrompt's `pinnedJumpChrome`). */
  landingInset: () => number
  /** The pinned-prompt jump: glide the transcript to a turn's prompt. */
  jump: (displayIdx: number) => void
  /** The transcript's jump highlight (`highlightTs`): the landed prompt's row flashes the ring a pinned-message jump draws. */
  setHighlightTs?: Dispatch<SetStateAction<string | null>>
}

/** How long the landed prompt keeps the jump highlight: the length of its `msg-highlight` fade. */
const LANDED_HIGHLIGHT_MS = 2000

function mountedRows(scroller: HTMLElement): MountedRow[] {
  const rows: MountedRow[] = []
  for (const node of scroller.querySelectorAll('[data-display-index]')) {
    if (!isTranscriptRow(node, scroller)) continue
    const idx = parseInt(node.getAttribute('data-display-index') || '', 10)
    if (Number.isFinite(idx)) rows.push({ idx, top: node.getBoundingClientRect().top })
  }
  return rows
}

/**
 * Alt+↑ / Alt+↓ for the chat page's transcript: register the stepper the global
 * shortcut handler calls (see turnStep), and return the sentence a polite live
 * region should speak for the turn a step landed on.
 *
 * A step is the pinned-prompt jump aimed at the previous or next turn, so it
 * lands where clicking the banner lands. Where it starts from:
 *
 * - The turn the last step landed on, for as long as nothing else happens. The
 *   screen alone cannot say which turn that is: a jump near the end of the
 *   transcript stops at the bottom with several prompts below the reading line,
 *   a prompt sent straight after another lands on the head of that pair, and a
 *   key held down fires again before the glide arrives. Any pointer, wheel or
 *   touch input, or any other key, forgets it.
 * - Otherwise the reading line: the first row at or below the line a jump lands
 *   rows on. Inside a long reply, Alt+↑ therefore goes to that reply's prompt
 *   first, as the banner does.
 *
 * The landed prompt flashes the transcript's jump highlight, so a sighted reader
 * sees where the step went even when the transcript could not scroll (the last
 * prompts of a chat all fit on screen).
 *
 * The keystroke is claimed whenever the transcript has turns, including a step
 * past either end that has nowhere to go, so a Mac browser does not scroll a
 * page for it instead.
 */
export function useTurnStepper({ active, turns, windowed, scrollerRef, landingInset, jump, setHighlightTs }: UseTurnStepperOptions): string {
  const [announcement, setAnnouncement] = useState('')
  // Read at keypress: registering once per activation keeps the stepper off the
  // per-token rebuild of `turns` while a reply streams.
  const latest = useRef({ turns, windowed, landingInset, jump, setHighlightTs })
  latest.current = { turns, windowed, landingInset, jump, setHighlightTs }
  // `ChatSection.id` of the turn the last step landed on. By id, not display
  // index, so older history loading in above it does not move the reference.
  const landedRef = useRef<string | null>(null)
  const highlightTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  useEffect(() => {
    if (!active) return
    const forget = () => { landedRef.current = null }
    const onKeyDown = (e: KeyboardEvent) => {
      // A modifier on its own (location left/right) starts the next chord rather
      // than ending the run of steps, and the step chord itself continues it.
      if (e.location === 1 || e.location === 2) return
      if (e.altKey && (e.code === 'ArrowUp' || e.code === 'ArrowDown')) return
      forget()
    }
    const passiveCapture = { capture: true, passive: true } as const
    document.addEventListener('keydown', onKeyDown, true)
    document.addEventListener('pointerdown', forget, true)
    document.addEventListener('wheel', forget, passiveCapture)
    document.addEventListener('touchstart', forget, passiveCapture)
    const unregister = registerTurnStepper((dir) => {
      const { turns: list, windowed: partial, landingInset: inset, jump: go, setHighlightTs: highlight } = latest.current
      const scroller = scrollerRef.current
      if (!scroller || list.length === 0) return false
      const stops = list.map(turn => turn.displayIdx)
      const landed = landedRef.current === null ? -1 : list.findIndex(turn => turn.id === landedRef.current)
      let at: number
      let onStop: boolean
      if (landed >= 0) {
        at = list[landed].displayIdx
        onStop = true
      } else {
        const position = readingPosition(mountedRows(scroller), scroller.getBoundingClientRect().top + inset())
        if (!position) return true
        at = position.at
        onStop = position.onLine
      }
      const target = turnStepTarget(stops, at, onStop, dir)
      if (target < 0) return true
      const n = stops.indexOf(target)
      const turn = list[n]
      landedRef.current = turn.id
      go(turn.displayIdx)
      const ts = turn.ts
      if (highlight && ts) {
        if (highlightTimerRef.current) clearTimeout(highlightTimerRef.current)
        highlight(ts)
        // Clears only its own highlight: another jump may have set one since.
        highlightTimerRef.current = setTimeout(() => {
          highlightTimerRef.current = null
          highlight(current => (current === ts ? null : current))
        }, LANDED_HIGHLIGHT_MS)
      }
      setAnnouncement(i18nT('pages.chatPage.turn_minimap_active', {
        position: i18nT(partial ? 'pages.chatPage.turn_minimap_meta_position_windowed' : 'pages.chatPage.turn_minimap_meta_position', { n: n + 1, count: list.length }),
        prompt: turn.label,
      }))
      return true
    })
    return () => {
      unregister()
      document.removeEventListener('keydown', onKeyDown, true)
      document.removeEventListener('pointerdown', forget, true)
      document.removeEventListener('wheel', forget, passiveCapture)
      document.removeEventListener('touchstart', forget, passiveCapture)
      forget()
      if (highlightTimerRef.current) clearTimeout(highlightTimerRef.current)
      highlightTimerRef.current = null
      setAnnouncement('')
    }
  }, [active, scrollerRef])

  return announcement
}
