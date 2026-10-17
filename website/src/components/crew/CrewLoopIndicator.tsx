/**
 * "On watch" mark for a crewmate whose automation loop is active.
 *
 * A crewmate with a live monitor or goal loop on its own thread will act again
 * without anyone asking. This mark says only that: no nudge text, no cycle
 * count, no banner. It is a solid accent ring around the avatar with one dot
 * orbiting it slowly, so it reads at a glance even on the compact header pill
 * where no word sits beside the face.
 *
 * It must never read like the WORKING state, which animates the face itself and
 * shows the green presence dot bottom-right. So this mark uses the accent
 * colour and moves around the face rather than in it. Under
 * prefers-reduced-motion the ring stays (it does not move) and the orbit is
 * swapped for one static accent dot at the top-right corner (the opposite
 * corner from presence).
 *
 * It fades in and out as a loop starts or stops, so a change mid-glance does
 * not read as a glitch; under reduced motion it cuts straight.
 *
 * The host wraps the avatar in a `relative` box; this renders absolutely over
 * it and nothing when `on` is false. It is decorative (tooltip only): every
 * host carries the label as text too (the roster row's badge, the header
 * pill's activity line and its screen-reader copy), so assistive tech reads
 * that word once.
 */
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion'
import { useTranslation } from 'react-i18next'

export default function CrewLoopIndicator({ on, testId = 'crew-loop-indicator' }: {
  on: boolean
  testId?: string
}) {
  const { t } = useTranslation()
  const reduceMotion = useReducedMotion()
  const label = t('pages.membersPage.loop_on')
  return (
    <AnimatePresence initial={false}>
      {on && (
        <motion.span
          key="loop-on"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: reduceMotion ? 0 : 0.2 }}
          aria-hidden="true"
          title={label}
          className="absolute -inset-[3px] rounded-full"
          data-testid={testId}
        >
          {/* Ring: still, so it stays under reduced motion too. */}
          <span
            aria-hidden="true"
            className="absolute inset-0 rounded-full border-2 border-accent/70"
            data-part="ring"
          />
          {/* Orbit: motion-safe only. */}
          <span
            aria-hidden="true"
            className="absolute inset-0 rounded-full animate-spin [animation-duration:4s] motion-reduce:hidden"
            data-part="orbit"
          >
            <span className="absolute left-1/2 -top-[3px] -ml-1 w-2 h-2 rounded-full border border-bg bg-accent" />
          </span>
          {/* Reduced motion: one static dot in place of the orbit. */}
          <span
            aria-hidden="true"
            className="hidden motion-reduce:block absolute -right-px -top-px w-2.5 h-2.5 rounded-full border-2 border-bg bg-accent"
            data-part="static-dot"
          />
        </motion.span>
      )}
    </AnimatePresence>
  )
}
