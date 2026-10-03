import { useId, useState } from 'react'
import { AnimatePresence, MotionConfig, motion, useReducedMotion } from 'framer-motion'
import { Briefcase, Lightbulb, Settings2, UserPlus } from 'lucide-react'
import { KiroGhost } from './KiroGhost'
import { Btn } from './ui'
import { useContainerWidth } from '../hooks/useContainerWidth'
import { i18nT } from '../i18n/t'

/** Below this measured width the expanded opening stacks its illustration
 *  above the copy. Measured on the card itself, not the viewport: the pane
 *  hosting it can be narrow inside a wide window (website/docs/narrow-viewport.md). */
const NARROW_PX = 560

/** A starter the user can pick. `prompt` is text for the NORMAL composer;
 *  nothing here sends, schedules or saves anything. */
type Starter = { id: 'setup' | 'work' | 'suggest'; icon: typeof Briefcase; label: string; prompt: string }

export interface AssistantWelcomeProps {
  /** Compact once the conversation has a user turn: the SAME card shrinks to a
   *  one-line header; expanded is the first-visit welcome. */
  compact: boolean
  /** The assistant's current display name (respects a user rename). */
  name?: string
  /** Put a starter's text into the composer. Returns false when the host kept
   *  an unsent draft instead of replacing it. */
  onStarter: (prompt: string) => boolean
  /** Open the crewmate creation flow. */
  onCreate: () => void
}

/**
 * The Assistant's opening, drawn as the first thing in its chat transcript.
 *
 * One element in both states: `compact` animates the same card between its
 * expanded welcome and its compact header (Framer `layout`), never a swap of
 * two components. Reduced motion drops the motion, not the continuity.
 */
export default function AssistantWelcome({ compact, name, onStarter, onCreate }: AssistantWelcomeProps) {
  const reduce = useReducedMotion()
  const [measureRef, width] = useContainerWidth<HTMLElement>()
  const [draftKept, setDraftKept] = useState(false)
  const startersHintId = useId()
  const narrow = width !== null && width < NARROW_PX
  const shownName = name?.trim() || i18nT('components.assistantWelcome.default_name')
  const transition = reduce ? { duration: 0 } : { duration: 0.38, ease: [0.22, 1, 0.36, 1] as const }

  const starters: Starter[] = [
    { id: 'setup', icon: Settings2, label: i18nT('components.assistantWelcome.starter_setup'), prompt: i18nT('components.assistantWelcome.prompt_setup') },
    { id: 'work', icon: Briefcase, label: i18nT('components.assistantWelcome.starter_work'), prompt: i18nT('components.assistantWelcome.prompt_work') },
    { id: 'suggest', icon: Lightbulb, label: i18nT('components.assistantWelcome.starter_suggest'), prompt: i18nT('components.assistantWelcome.prompt_suggest') },
  ]

  const stacked = narrow && !compact

  return (
    <MotionConfig transition={transition}>
      <motion.section
        ref={measureRef}
        layout
        data-testid="assistant-welcome"
        data-state={compact ? 'compact' : 'expanded'}
        aria-label={i18nT('components.assistantWelcome.region_label', { name: shownName })}
        className="mx-4 mt-2 mb-4 grid overflow-hidden border border-border bg-bg"
        style={{ gridTemplateColumns: compact ? (narrow ? '52px minmax(0,1fr)' : '64px minmax(0,1fr)') : stacked ? 'minmax(0,1fr)' : '34% minmax(0,1fr)', borderRadius: compact ? 12 : 20 }}
      >
        {/* Illustration band: decorative, so hidden from assistive tech. */}
        <motion.div
          layout
          aria-hidden
          className={`bg-accent text-accent-fg flex ${stacked ? 'flex-row items-center justify-between' : 'flex-col'} ${compact ? 'items-center justify-center' : 'justify-between'}`}
          style={{ padding: compact ? 10 : stacked ? '16px 20px' : 24, minHeight: compact ? 72 : stacked ? 112 : 300 }}
        >
          <AnimatePresence initial={false}>
            {!compact && (
              <motion.div key="eyebrow" layout="position" initial={{ opacity: 0 }} animate={{ opacity: 0.85 }} exit={{ opacity: 0 }} className="text-[12px] font-semibold uppercase tracking-wider">
                {i18nT('components.assistantWelcome.art_eyebrow')}
              </motion.div>
            )}
          </AnimatePresence>
          <motion.div
            layout
            className="flex items-center justify-center self-center"
            animate={{ rotate: compact ? 0 : -10 }}
            initial={false}
          >
            <span data-testid="assistant-welcome-mark">
              <KiroGhost size={compact ? 30 : stacked ? 64 : 112} />
            </span>
          </motion.div>
          <AnimatePresence initial={false}>
            {!compact && !stacked && (
              <motion.div key="tagline" layout="position" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} className="text-[22px] font-semibold leading-tight">
                {i18nT('components.assistantWelcome.art_tagline')}
              </motion.div>
            )}
          </AnimatePresence>
        </motion.div>

        <motion.div layout="position" className={`min-w-0 self-center ${compact ? 'px-4 py-3' : narrow ? 'px-5 py-5' : 'px-7 py-7'}`}>
          <motion.h2
            layout="position"
            className="m-0 font-semibold text-text leading-tight"
            initial={false}
            animate={{ fontSize: compact ? '15px' : narrow ? '22px' : '26px' }}
          >
            {i18nT('components.assistantWelcome.title', { name: shownName })}
          </motion.h2>
          <motion.p layout="position" className={`m-0 mt-1.5 leading-relaxed ${compact ? 'text-[13px] text-muted' : 'text-sm text-text'}`}>
            {i18nT('components.assistantWelcome.description')}
          </motion.p>

          <AnimatePresence initial={false}>
            {!compact && (
              <motion.div
                key="expanded-body"
                data-testid="assistant-welcome-body"
                initial={{ height: 0, opacity: 0 }}
                animate={{ height: 'auto', opacity: 1 }}
                exit={{ height: 0, opacity: 0 }}
                className="overflow-hidden"
              >
                <p className="m-0 mt-3 text-[13px] text-muted leading-relaxed">{i18nT('components.assistantWelcome.detail')}</p>
                {/* One starter per line (a list, not a button row): each fills
                    the composer below; creating a crewmate opens its flow. */}
                <ul className="list-none m-0 mt-4 p-0 flex flex-col gap-2" aria-label={i18nT('components.assistantWelcome.starters_label')}>
                  {starters.map(({ id, icon: Icon, label, prompt }) => (
                    <li key={id}>
                      <Btn
                        type="button"
                        data-testid={`assistant-welcome-starter-${id}`}
                        className="w-full min-h-11 justify-start text-left"
                        aria-describedby={startersHintId}
                        onClick={() => setDraftKept(!onStarter(prompt))}
                      >
                        <Icon size={16} aria-hidden className="shrink-0" />
                        <span className="min-w-0">{label}</span>
                      </Btn>
                    </li>
                  ))}
                </ul>
                {/* Said under the starters, before the first click: a starter
                    only fills the composer, it never sends. Without this line
                    the rows read as "send this now". */}
                <p id={startersHintId} className="m-0 mt-2 text-[12px] text-muted" data-testid="assistant-welcome-starters-hint">
                  {i18nT('components.assistantWelcome.starters_hint')}
                </p>
                {/* Creating a crewmate opens a flow instead of filling the
                    composer, so it sits apart from the starters above. */}
                <Btn
                  type="button"
                  data-testid="assistant-welcome-create"
                  className="mt-4 w-full min-h-11 justify-start text-left"
                  onClick={onCreate}
                >
                  <UserPlus size={16} aria-hidden className="shrink-0" />
                  <span className="min-w-0">{i18nT('components.assistantWelcome.starter_create')}</span>
                </Btn>
                {/* The live region stays mounted so the draft-kept notice is announced when it appears. */}
                <p
                  className={draftKept ? 'm-0 mt-3 text-[12px] text-muted' : 'sr-only'}
                  role="status"
                  aria-live="polite"
                  data-testid="assistant-welcome-status"
                >
                  {draftKept ? i18nT('components.assistantWelcome.draft_kept') : null}
                </p>
              </motion.div>
            )}
          </AnimatePresence>
        </motion.div>
      </motion.section>
    </MotionConfig>
  )
}
