/**
 * The pending reply while the gateway prepares a message outside any turn
 * (the warm welcome). The caption sits INSIDE a reply bubble, where the
 * welcome will land, not as a muted line under the footer.
 *
 * UX lens 13 (website/AGENTS.md): one element, no hard swap. The bubble is the
 * last thing under the transcript; when the frame drops `preparing` it folds
 * by height and fades while the welcome row enters in the same place, so
 * nothing below jumps.
 */
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion'
import TypingDots from './TypingDots'
import { crewmateBubbleClass } from './crewmateBubbles'

export default function PreparingReplyBubbleView({ caption, shown, testId }: { caption: string; shown: boolean; testId: string }) {
  const reduce = useReducedMotion()
  return (
    <AnimatePresence initial={false}>
      {shown && (
        <motion.div
          key="preparing-reply"
          data-testid={testId}
          className="px-4 mx-auto w-full overflow-hidden"
          style={{ maxWidth: 'var(--mc-content-width, 900px)' }}
          initial={reduce ? false : { opacity: 0, height: 0 }}
          animate={{ opacity: 1, height: 'auto' }}
          exit={reduce ? { opacity: 0 } : { opacity: 0, height: 0 }}
          transition={{ duration: reduce ? 0 : 0.2 }}
        >
          {/* The bubble's PARENT carries the fill token (`:has(>.message-bubble.crewmate-bubble)`
              in index.css), the same way a message row does. */}
          <div className="flex flex-col items-start py-1 mt-3">
            {/* 'message-bubble' is a stable theming hook — see website/docs/theming-contract.md */}
            <div
              className={`message-bubble mc-message-font-scope msg-content leading-relaxed ${crewmateBubbleClass('single')}`}
              style={{ fontSize: 'var(--mc-message-font-size, 14px)' }}
            >
              <p className="my-0 leading-normal flex items-center gap-2 text-muted" role="status" aria-live="polite">
                <span className="min-w-0">{caption}</span>
                <span aria-hidden="true"><TypingDots /></span>
              </p>
            </div>
          </div>
        </motion.div>
      )}
    </AnimatePresence>
  )
}
