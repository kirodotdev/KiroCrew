import { cn } from '../lib/utils'
import { motion, useReducedMotion, type TargetAndTransition, type Transition } from 'framer-motion'
import { ChevronRight, Hand, Target, Zap } from 'lucide-react'
import { useEffect, useState, type ReactNode } from 'react'
import { KiroGhost } from './KiroGhost'
import { KiroGhostMark } from './KiroGhostMark'
import { Btn } from './ui'

import { i18nT } from '../i18n/t'

/** One theme hue per role. Tokens only, so every theme recolors the page. */
type Tone = 'aim' | 'info' | 'ok' | 'accent'

const TONE_TEXT: Record<Tone, string> = { aim: 'text-aim', info: 'text-info', ok: 'text-ok', accent: 'text-accent' }

/** How long the page plays, in seconds. Every animation here runs once and is done
 *  by then; at this point the page also settles on its final frame, so nothing is
 *  left moving while the person types below it. Kept under 4s so that, counted from
 *  the chip press (the page mounts a few hundred ms later), motion ends within 4s. */
export const CREW_WELCOME_PLAY_SECS = 3.5

/** When each chat first showed this page in this tab, by chat key. The entrance
 *  plays once per chat, not once per mount: a later mount for the same chat (the
 *  transcript briefly swapping the page out and back) starts on the final frame. */
const firstShown = new Map<string, number>()

/** Whether the page shows its still final frame: with reduced motion from the
 *  start; for a chat that has already shown it; otherwise once it has played, or
 *  the moment the person starts on the composer (focus in a text field, any input
 *  or key press anywhere on the page), whichever comes first. Settled, it never
 *  plays again. */
function useSettled(reduce: boolean, chatKey: string | undefined): boolean {
  const [settled, setSettled] = useState(() => reduce || (chatKey !== undefined && firstShown.has(chatKey)))
  // Recorded after mount, not while rendering, so a render React throws away does
  // not count as the chat having shown the page.
  useEffect(() => {
    if (chatKey !== undefined && !firstShown.has(chatKey)) firstShown.set(chatKey, Date.now())
  }, [chatKey])
  useEffect(() => {
    if (reduce || settled) return
    const settle = () => setSettled(true)
    const onFocus = (e: FocusEvent) => {
      const t = e.target as HTMLElement | null
      if (t && (t.tagName === 'TEXTAREA' || t.tagName === 'INPUT' || t.isContentEditable)) settle()
    }
    const timer = window.setTimeout(settle, CREW_WELCOME_PLAY_SECS * 1000)
    document.addEventListener('focusin', onFocus)
    document.addEventListener('input', settle, true)
    document.addEventListener('keydown', settle, true)
    return () => {
      window.clearTimeout(timer)
      document.removeEventListener('focusin', onFocus)
      document.removeEventListener('input', settle, true)
      document.removeEventListener('keydown', settle, true)
    }
  }, [reduce, settled])
  return reduce || settled
}

/** The small ghosts that pop up around the big one: where, how big, which hue, when. */
const CREW_GHOSTS: ReadonlyArray<{ x: string; y: string; size: number; tone: Tone; delay: number }> = [
  { x: '12%', y: '18%', size: 26, tone: 'aim', delay: 0.35 },
  { x: '24%', y: '62%', size: 20, tone: 'info', delay: 0.55 },
  { x: '33%', y: '8%', size: 16, tone: 'accent', delay: 0.75 },
  { x: '66%', y: '6%', size: 18, tone: 'ok', delay: 0.45 },
  { x: '80%', y: '30%', size: 28, tone: 'info', delay: 0.65 },
  { x: '72%', y: '68%', size: 18, tone: 'aim', delay: 0.95 },
]

/** The flow's timeline in seconds, in the order Crew Mode works: one goal, this
 *  chat splits it, a crew ghost flies into each task's own chat, the tasks land on
 *  the board, and the one step that waits for you lights up last. */
const AT = { goal: 0.7, split: 1.0, chats: [1.3, 1.45, 1.6], board: 1.9, rows: [2.05, 2.15, 2.25], needsYou: 2.6 }

/** `initial`/`animate` for one entrance: from `from` to `to` after `delay`, or the
 *  final frame at once with reduced motion. */
function enter(still: boolean, delay: number, from: TargetAndTransition, to: TargetAndTransition, transition: Transition = {}) {
  return {
    initial: still ? false as const : from,
    animate: to,
    transition: { delay, type: 'spring' as const, stiffness: 300, damping: 20, ...transition },
  }
}

/** A ghost that pops up and floats once. Still, it simply sits there. */
function PoppingGhost({ x, y, size, tone, delay, still }: (typeof CREW_GHOSTS)[number] & { still: boolean }) {
  return (
    <motion.span
      aria-hidden="true"
      className={`absolute ${TONE_TEXT[tone]}`}
      style={{ left: x, top: y, filter: 'drop-shadow(0 0 10px currentColor)' }}
      {...enter(still, delay, { opacity: 0, y: 24, scale: 0.2 }, { opacity: 0.85, y: 0, scale: 1 }, { stiffness: 260, damping: 14 })}
    >
      <motion.span
        className="block"
        animate={still ? undefined : { y: [0, -8, 0] }}
        transition={still ? undefined : { delay: delay + 0.6, duration: 1.9, ease: 'easeInOut' }}
      >
        <KiroGhostMark size={size} />
      </motion.span>
    </motion.span>
  )
}

/** One step of the flow, as a list item: its picture in a fixed-height well, so
 *  every label sits on one baseline, a one- or two-word label under it, and the
 *  arrow to the next step. The picture and the arrow are decoration; the label is
 *  what a screen reader hears. */
function Step({ label, still, arrowAt, children }: { label: string; still: boolean; arrowAt?: number; children: ReactNode }) {
  return (
    <li className="relative flex flex-col items-center gap-1 min-w-0 w-[52px] @[34rem]/crewwelcome:w-[76px] shrink">
      <span aria-hidden="true" className="h-[56px] @[34rem]/crewwelcome:h-[78px] w-full grid place-items-center">{children}</span>
      <span className="w-full text-[11px] @[34rem]/crewwelcome:text-[12px] leading-tight text-muted text-center">{label}</span>
      {arrowAt !== undefined && (
        <motion.span
          aria-hidden="true"
          className="absolute -right-[11px] @[34rem]/crewwelcome:-right-[14px] top-[22px] @[34rem]/crewwelcome:top-[33px] text-aim/60"
          {...enter(still, arrowAt, { opacity: 0, x: -4 }, { opacity: 1, x: 0 }, { type: 'tween', duration: 0.2 })}
        >
          <ChevronRight size={12} />
        </motion.span>
      )}
    </li>
  )
}

/** The goal-to-decision flow, drawn with the crew's own ghosts: an ordered list,
 *  so a screen reader hears the five steps in order under the list's name. */
function Flow({ still }: { still: boolean }) {
  return (
    <ol
      data-testid="crew-mode-flow"
      aria-label={i18nT('components.crewMode.flow_label')}
      className="list-none m-0 p-0 flex items-start justify-center gap-[10px] @[34rem]/crewwelcome:gap-4 w-full min-w-0"
    >
      <Step label={i18nT('components.crewMode.flow_goal')} still={still} arrowAt={AT.goal + 0.15}>
        <motion.span
          className="h-7 px-2.5 @[34rem]/crewwelcome:h-8 @[34rem]/crewwelcome:px-3 rounded-full border border-aim/50 bg-aim-subtle text-aim inline-flex items-center"
          {...enter(still, AT.goal, { opacity: 0, scale: 0.4 }, { opacity: 1, scale: 1 })}
        >
          <Target size={14} />
        </motion.span>
      </Step>
      <Step label={i18nT('components.crewMode.flow_split')} still={still} arrowAt={AT.split + 0.15}>
        <motion.span
          className="w-8 h-8 @[34rem]/crewwelcome:w-10 @[34rem]/crewwelcome:h-10 rounded-full grid place-items-center bg-aim text-aim-fg"
          style={{ boxShadow: '0 0 16px var(--color-aim)' }}
          {...enter(still, AT.split, { opacity: 0, scale: 0.3, rotate: -30 }, { opacity: 1, scale: 1, rotate: 0 })}
        >
          <KiroGhostMark size={16} />
        </motion.span>
      </Step>
      <Step label={i18nT('components.crewMode.flow_parallel')} still={still} arrowAt={AT.board - 0.1}>
        <span className="flex flex-col gap-0.5 @[34rem]/crewwelcome:gap-1">
          {AT.chats.map((at, i) => (
            // A chat bubble each; a crew ghost flies in from the split and settles inside.
            <motion.span
              key={at}
              className={`relative h-[16px] w-9 @[34rem]/crewwelcome:h-[20px] @[34rem]/crewwelcome:w-11 rounded-md border border-aim/40 bg-bg-elevated grid place-items-center ${i === 1 ? 'translate-x-1.5' : ''}`}
              {...enter(still, at - 0.1, { opacity: 0, scale: 0.6 }, { opacity: 1, scale: 1 }, { type: 'tween', duration: 0.18 })}
            >
              <motion.span
                className={TONE_TEXT[(['aim', 'info', 'ok'] as const)[i]]}
                {...enter(still, at, { opacity: 0, x: -30, y: (1 - i) * 18, scale: 0.4 }, { opacity: 1, x: 0, y: 0, scale: 1 }, { stiffness: 220, damping: 16 })}
              >
                <KiroGhostMark size={11} />
              </motion.span>
            </motion.span>
          ))}
        </span>
      </Step>
      <Step label={i18nT('components.crewMode.flow_board')} still={still} arrowAt={AT.needsYou - 0.15}>
        {/* A tiny board: a header bar and one row per task, filling in as each lands. */}
        <motion.span
          className="w-10 h-[48px] @[34rem]/crewwelcome:w-12 @[34rem]/crewwelcome:h-[60px] rounded-md border border-border bg-bg-elevated p-1.5 flex flex-col gap-1"
          {...enter(still, AT.board, { opacity: 0, y: 8 }, { opacity: 1, y: 0 })}
        >
          <span className="h-1 w-1/2 rounded-full bg-muted/40" />
          {AT.rows.map(at => (
            <motion.span
              key={at}
              className="h-1.5 rounded-full bg-aim/70 origin-left"
              {...enter(still, at, { scaleX: 0 }, { scaleX: 1 }, { type: 'tween', duration: 0.22 })}
            />
          ))}
        </motion.span>
      </Step>
      <Step label={i18nT('components.crewMode.flow_needs_you')} still={still}>
        <span className="relative">
          {!still && (
            <motion.span
              className="absolute inset-0 rounded-full border-2 border-warn"
              initial={{ scale: 1, opacity: 0 }}
              animate={{ scale: 1.9, opacity: [0, 0.8, 0] }}
              transition={{ delay: AT.needsYou + 0.1, duration: 0.75, ease: 'easeOut' }}
            />
          )}
          <motion.span
            className="w-8 h-8 @[34rem]/crewwelcome:w-10 @[34rem]/crewwelcome:h-10 rounded-full grid place-items-center relative border border-warn/60 bg-warn/15 text-warn-text"
            style={{ boxShadow: '0 0 14px var(--color-warn)' }}
            {...enter(still, AT.needsYou, { opacity: 0.25, scale: 0.7, filter: 'saturate(0)' }, { opacity: 1, scale: 1, filter: 'saturate(1)' }, { stiffness: 260, damping: 12 })}
          >
            <Hand size={15} />
          </motion.span>
        </span>
      </Step>
    </ol>
  )
}

/** The welcome page an empty chat shows once Crew Mode is on.
 *
 *  Pictures, not prose: the mode's own hero (big solid type, the Kiro ghost on an
 *  aim disc with rings and a few crew ghosts popping up around it), the slogan, and
 *  a flow that shows what the mode does in the order it does it, with one or two
 *  words per step: one goal, this chat splits it, a crew ghost flies into each
 *  task's own chat, the tasks land on the board, and the step that waits for you
 *  lights up. Then one "Board" button, drawn like the side panel's Board tab (its
 *  icon alone where room is tight). Every word is generic, so nothing
 *  here reads as live work: the real board is the side panel's Board tab, read
 *  from this chat's own work ledger.
 *
 *  Everything plays once and is done within CREW_WELCOME_PLAY_SECS; the moment the
 *  person starts on the composer the page jumps to its final frame, so nothing moves
 *  while they type. With reduced motion the final frame is all it ever draws.
 *  Sized by the chat column, not the window (beside an open side panel a wide window
 *  still leaves a narrow column); a short screen (a phone held sideways) drops the
 *  art and the slogan and puts the button beside the title, and a page taller than
 *  the room scrolls, title in reach.
 *
 *  `onOpenTaskBoard` opens that Board tab. Unset (a session with no board of its
 *  own), the button is left out. */
export default function CrewModeWelcome({ onOpenTaskBoard, chatKey }: {
  onOpenTaskBoard?: () => void
  /** The chat this page belongs to; its entrance plays once per chat. */
  chatKey?: string
}) {
  const still = useSettled(useReducedMotion() ?? false, chatKey)
  const lead = i18nT('components.crewMode.tagline_lead')
  const short = '[@media(max-height:480px)]:hidden'
  // Tight room: the same short-screen query, or a chat column narrower than 18rem.
  const tight = cn(short, '@max-[18rem]/crewwelcome:hidden')
  // The side panel's Board tab, as its strip draws it: the same glyph and name.
  const boardLabel = i18nT('commandCenter.workers')
  return (
    <motion.div
      // Re-mounted when it settles: every part then mounts on its final frame, so an
      // animation still in flight (or a loop) stops at once instead of finishing.
      key={still ? 'still' : 'playing'}
      data-testid="crew-mode-welcome"
      data-motion={still ? 'still' : 'playing'}
      // `pt-12`: the chat header floats over the top of this box, so the page
      // starts below it; a page taller than the room then scrolls with its title
      // still in reach instead of tucked under the header. `pb-6`: the button
      // keeps clear of the scroll box's bottom edge. On a short screen the
      // button moves up beside the title (a wrapping row, the flow ordered last
      // on its own line), so it is never left at or under that edge.
      className="@container/crewwelcome w-full max-w-[640px] mx-auto px-1 pt-12 pb-6 [@media(max-height:480px)]:pb-3 flex flex-col [@media(max-height:480px)]:flex-row [@media(max-height:480px)]:flex-wrap justify-center items-center gap-3 @[34rem]/crewwelcome:gap-5 [@media(max-height:480px)]:gap-2 text-center"
      initial={still ? false : { opacity: 0 }}
      animate={{ opacity: 1 }}
      transition={{ duration: 0.25 }}
    >
      <div aria-hidden="true" className={`relative w-full h-[76px] @[34rem]/crewwelcome:h-[150px] ${short}`}>
        <div className="absolute inset-0 scale-[0.5] @[34rem]/crewwelcome:scale-100">
          {!still && [0, 0.7].map(d => (
            <motion.span
              key={d}
              className="absolute left-1/2 top-1/2 -ml-[56px] -mt-[56px] w-[112px] h-[112px] rounded-full border-2 border-aim"
              initial={{ scale: 0.7, opacity: 0.7 }}
              animate={{ scale: 1.7, opacity: 0 }}
              transition={{ delay: d, duration: 2.4, ease: 'easeOut' }}
            />
          ))}
          {/* The ghost is white artwork, so it sits on an aim-colored disc: on a light
              theme a bare white ghost would vanish into the page. */}
          <motion.span
            className="absolute left-1/2 top-1/2 -ml-[48px] -mt-[48px] w-24 h-24 rounded-full bg-aim grid place-items-center"
            style={{ boxShadow: '0 0 32px var(--color-aim)' }}
            {...enter(still, 0, { opacity: 0, scale: 0.3 }, { opacity: 1, scale: 1 }, { stiffness: 220, damping: 14 })}
          >
            <KiroGhost size={52} />
          </motion.span>
        </div>
        {CREW_GHOSTS.map(g => <PoppingGhost key={`${g.x}-${g.y}`} {...g} still={still} />)}
      </div>

      <div className="flex flex-col items-center gap-1.5">
        <motion.h2
          className="m-0 text-[36px] @[34rem]/crewwelcome:text-6xl [@media(max-height:480px)]:text-[28px] font-bold tracking-tight leading-none ultra-title-text"
          {...enter(still, 0.15, { opacity: 0, y: 10 }, { opacity: 1, y: 0 })}
        >
          {i18nT('components.crewMode.title')}
        </motion.h2>
        <motion.p
          className={`m-0 text-[15px] @[34rem]/crewwelcome:text-lg text-text ${short}`}
          {...enter(still, 0.25, { opacity: 0, y: 8 }, { opacity: 1, y: 0 })}
        >
          {/* A sentence ending in a full-width stop (Chinese, Japanese) already
              carries its own space; a second one reads as a gap. */}
          {lead}{/[。！？]$/.test(lead) ? '' : ' '}
          <strong className="font-semibold text-text-strong">{i18nT('components.crewMode.tagline_strong')}</strong>
        </motion.p>
      </div>

      {/* No frame: a soft wash under the flow, so it reads as part of the hero. */}
      <motion.div
        className="w-full max-w-[560px] rounded-3xl bg-aim-subtle/15 px-1 py-2 @[34rem]/crewwelcome:py-4 [@media(max-height:480px)]:py-1 [@media(max-height:480px)]:order-last"
        {...enter(still, 0.5, { opacity: 0, y: 10 }, { opacity: 1, y: 0 }, { type: 'tween', duration: 0.3 })}
      >
        <Flow still={still} />
      </motion.div>

      {onOpenTaskBoard && (
        <motion.div {...enter(still, 0.4, { opacity: 0 }, { opacity: 1 }, { type: 'tween', duration: 0.3 })}>
          {/* Where room is tight (a short screen, a narrow column) the button is its
              icon alone; its name stays on aria-label and the title tooltip, and a
              touch screen keeps a 44px target. */}
          <Btn
            type="button"
            data-testid="crew-mode-open-board"
            onClick={onOpenTaskBoard}
            aria-label={boardLabel}
            title={boardLabel}
            className="justify-center min-h-[44px] sm:min-h-0 [@media(pointer:coarse)]:min-h-11 [@media(pointer:coarse)]:min-w-11"
          >
            {/* The Board tab's glyph (`SidePanelTabIcons`, kind `workers`). */}
            <Zap size={16} aria-hidden="true" className="shrink-0" />
            <span data-testid="crew-mode-open-board-label" aria-hidden="true" className={tight}>{boardLabel}</span>
          </Btn>
        </motion.div>
      )}
    </motion.div>
  )
}
