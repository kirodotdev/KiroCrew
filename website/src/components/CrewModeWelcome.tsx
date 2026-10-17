import { motion, useReducedMotion } from 'framer-motion'
import { ArrowLeftRight, CircleHelp, Eye, Inbox, Plus, type LucideIcon } from 'lucide-react'
import { KiroGhost } from './KiroGhost'
import { KiroGhostMark } from './KiroGhostMark'

import { i18nT } from '../i18n/t'

/** One theme hue per role. Tokens only, so every theme recolors the page. */
type Tone = 'aim' | 'info' | 'ok' | 'warn' | 'accent' | 'muted'

const TONE_TEXT: Record<Tone, string> = {
  aim: 'text-aim', info: 'text-info', ok: 'text-ok', warn: 'text-warn', accent: 'text-accent', muted: 'text-muted',
}
/** How many extra times a looping animation plays before it rests, so the art
 *  is still while the person types below it. */
const ART_REPEATS = 2
const TONE_BORDER: Record<Tone, string> = {
  aim: 'border-aim', info: 'border-info', ok: 'border-ok', warn: 'border-warn', accent: 'border-accent', muted: 'border-border-strong',
}

/** The small ghosts that pop up around the big one: where, how big, which hue, when. */
const CREW_GHOSTS: ReadonlyArray<{ x: string; y: string; size: number; tone: Tone; delay: number }> = [
  { x: '8%', y: '16%', size: 30, tone: 'aim', delay: 0.35 },
  { x: '20%', y: '62%', size: 24, tone: 'info', delay: 0.55 },
  { x: '30%', y: '6%', size: 18, tone: 'accent', delay: 0.75 },
  { x: '70%', y: '4%', size: 22, tone: 'ok', delay: 0.45 },
  { x: '86%', y: '28%', size: 32, tone: 'info', delay: 0.65 },
  { x: '76%', y: '66%', size: 20, tone: 'aim', delay: 0.95 },
  { x: '6%', y: '76%', size: 16, tone: 'ok', delay: 1.1 },
  { x: '92%', y: '80%', size: 14, tone: 'accent', delay: 1.25 },
]

/** The four things Crew Mode does, as one big icon and two words each. */
function capabilities(): ReadonlyArray<{ Icon: LucideIcon; label: string; tone: Tone }> {
  return [
    { Icon: Plus, label: i18nT('components.crewMode.does_new_chat'), tone: 'aim' },
    { Icon: ArrowLeftRight, label: i18nT('components.crewMode.does_pass_notes'), tone: 'info' },
    { Icon: Inbox, label: i18nT('components.crewMode.does_take_asks'), tone: 'ok' },
    { Icon: Eye, label: i18nT('components.crewMode.does_check_in'), tone: 'warn' },
  ]
}

/** The sample crew on the Preview board: one ghost per task, the hue is the state. */
function sampleCrew(): ReadonlyArray<{ name: string; state: string; tone: Tone }> {
  return [
    { name: i18nT('components.crewMode.sample_email'), state: i18nT('components.crewMode.state_done'), tone: 'ok' },
    { name: i18nT('components.crewMode.sample_layout'), state: i18nT('components.crewMode.state_working'), tone: 'info' },
    { name: i18nT('components.crewMode.sample_room'), state: i18nT('components.crewMode.state_needs_you'), tone: 'warn' },
    { name: i18nT('components.crewMode.sample_screenshot'), state: i18nT('components.crewMode.state_waiting'), tone: 'muted' },
  ]
}

/** A ghost that pops up and then floats. With reduced motion it simply sits there. */
function PoppingGhost({ x, y, size, tone, delay, still }: (typeof CREW_GHOSTS)[number] & { still: boolean }) {
  return (
    <motion.span
      aria-hidden="true"
      className={`absolute ${TONE_TEXT[tone]}`}
      style={{ left: x, top: y, filter: 'drop-shadow(0 0 10px currentColor)' }}
      initial={still ? false : { opacity: 0, y: 24, scale: 0.2 }}
      animate={{ opacity: 0.85, y: 0, scale: 1 }}
      transition={{ delay, type: 'spring', stiffness: 260, damping: 14 }}
    >
      <motion.span
        className="block"
        animate={still ? undefined : { y: [0, -8, 0] }}
        transition={still ? undefined : { delay: delay + 0.7, duration: 3.4, repeat: ART_REPEATS, ease: 'easeInOut' }}
      >
        <KiroGhostMark size={size} />
      </motion.span>
    </motion.span>
  )
}

/** The welcome page an empty chat shows while its Crew Mode switch is on.
 *
 *  Big type says what this is; four icons say what it does; a board marked
 *  Preview shows what tracking looks like. The board is sample data on purpose:
 *  a new chat has no tasks yet, so nothing here pretends to be real. */
export default function CrewModeWelcome() {
  const still = useReducedMotion() ?? false
  return (
    <motion.div
      data-testid="crew-mode-welcome"
      className="w-full max-w-[620px] mx-auto pt-0 sm:pt-2 pb-1 sm:pb-4 flex flex-col items-center gap-2 sm:gap-4 text-center"
      initial={still ? false : { opacity: 0 }}
      animate={{ opacity: 1 }}
      transition={{ duration: 0.25 }}
    >
      <div aria-hidden="true" className="relative w-full h-[64px] sm:h-[170px]">
        {/* A phone shrinks the art row, the centre disc and its rings so the
            whole sample board, border included, fits above the composer. */}
        <div className="absolute inset-0 scale-[0.45] sm:scale-100">
          {!still && [0, 1.4].map(d => (
            <motion.span
              key={d}
              className="absolute left-1/2 top-1/2 -ml-[56px] -mt-[56px] w-[112px] h-[112px] rounded-full border-2 border-aim"
              initial={{ scale: 0.7, opacity: 0.7 }}
              animate={{ scale: 1.7, opacity: 0 }}
              transition={{ delay: d, duration: 2.8, repeat: ART_REPEATS, ease: 'easeOut' }}
            />
          ))}
          {/* The ghost is white artwork, so it sits on an aim-colored disc: on a light
              theme a bare white ghost would vanish into the page. */}
          <motion.span
            className="absolute left-1/2 top-1/2 -ml-[48px] -mt-[48px] w-24 h-24 rounded-full bg-aim grid place-items-center"
            style={{ boxShadow: '0 0 32px var(--color-aim)' }}
            initial={still ? false : { opacity: 0, scale: 0.3 }}
            animate={{ opacity: 1, scale: 1 }}
            transition={{ type: 'spring', stiffness: 220, damping: 14 }}
          >
            <KiroGhost size={52} />
          </motion.span>
        </div>
        {CREW_GHOSTS.map(g => <PoppingGhost key={`${g.x}-${g.y}`} {...g} still={still} />)}
      </div>

      <h2 className="m-0 text-4xl sm:text-6xl font-bold tracking-tight leading-none text-aim">
        {i18nT('components.crewMode.title')}
      </h2>
      <p className="m-0 text-base sm:text-xl text-text">
        {i18nT('components.crewMode.tagline_lead')}{' '}
        <strong className="font-semibold text-text-strong">{i18nT('components.crewMode.tagline_strong')}</strong>
      </p>

      <ul className="list-none m-0 p-0 w-full grid grid-cols-2 sm:grid-cols-4 gap-x-2 gap-y-1.5 sm:gap-3">
        {capabilities().map(({ Icon, label, tone }) => (
          <li key={label} className="flex flex-col items-center gap-1 sm:gap-2">
            <Icon size={30} aria-hidden="true" className={TONE_TEXT[tone]} />
            <span className="text-[13px] font-medium text-text-strong">{label}</span>
          </li>
        ))}
      </ul>

      {/* Sample content: the ghost circles and the question's icon tile are
          muted, the question row is neutral rather than warn-toned, and nothing
          in it looks clickable, so a reader does not take it for a crew that is
          already working. Names and state words stay at full contrast to stay legible. */}
      <section aria-label={i18nT('components.crewMode.board_title')} className="w-full rounded-2xl border border-dashed border-border p-2.5 sm:p-4 flex flex-col gap-2 sm:gap-3 text-left">
        <div className="flex items-baseline justify-between">
          <span className="text-[15px] font-semibold text-text-strong">{i18nT('components.crewMode.board_title')}</span>
          <span className="rounded-full bg-bg-hover px-2 text-[11px] font-semibold uppercase tracking-wider text-muted">{i18nT('components.crewMode.board_preview')}</span>
        </div>
        <div data-testid="crew-mode-sample-question" className="flex items-center gap-3 rounded-xl bg-bg-hover px-3 py-2 sm:py-2.5">
          <span aria-hidden="true" data-testid="crew-mode-sample-question-icon" className="shrink-0 w-9 h-9 rounded-lg grid place-items-center bg-bg text-muted opacity-70"><CircleHelp size={20} /></span>
          <div className="min-w-0 text-[15px] font-semibold text-text-strong">{i18nT('components.crewMode.sample_question')}</div>
        </div>
        <ul className="list-none m-0 p-0 grid grid-cols-2 sm:grid-cols-4 gap-2">
          {sampleCrew().map(m => (
            <li key={m.name} className="flex flex-col items-center gap-1 text-center">
              <span aria-hidden="true" className={`w-10 h-10 sm:w-12 sm:h-12 rounded-full grid place-items-center border-2 bg-bg ${TONE_BORDER[m.tone]} ${TONE_TEXT[m.tone]} ${m.tone === 'muted' ? 'opacity-60' : 'opacity-70'}`}>
                <KiroGhostMark size={22} />
              </span>
              <span className="text-[12px] font-medium text-text-strong">{m.name}</span>
              {/* State words are muted on every theme: some themes' tone tokens miss
                  4.5:1 as small text. The tone stays on the circle beside the word. */}
              <span className="text-[12px] text-muted">{m.state}</span>
            </li>
          ))}
        </ul>
      </section>
    </motion.div>
  )
}
