import type { ReactNode } from 'react'
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion'

import { OFFSTAGE, onstage } from '../lib/offstage'

interface Props {
  hasAgentPanels: boolean
  /** One agent panel is enlarged to fill the workspace (#21179). */
  expanded?: boolean
  agentPanels: ReactNode
  transcript: ReactNode
}

/**
 * The meeting's adaptive content plane.
 *
 * Agent output and transcript share the page while any agent is enabled. With
 * an empty roster, the transcript becomes the primary surface instead of
 * leaving a blank output area beside a narrow right column.
 *
 * While one agent panel is expanded, the transcript column fades out and then
 * goes OFFSTAGE (see ../lib/offstage): out of the flow and invisible, but still
 * laid out and mounted. So its scroll position survives in every engine, and its
 * own follow-new-speech logic keeps working while hidden: on restore the reader
 * is where they were, or at the latest line if they were following.
 */
export default function MeetingWorkspace({
  hasAgentPanels,
  expanded = false,
  agentPanels,
  transcript,
}: Props) {
  const reduceMotion = useReducedMotion()
  const transition = reduceMotion
    ? { duration: 0 }
    : { type: 'spring' as const, stiffness: 300, damping: 32 }
  const panelExpanded = hasAgentPanels && expanded

  return (
    <motion.div
      layout
      data-testid="meeting-workspace"
      data-transcript-layout={
        panelExpanded ? 'hidden' : hasAgentPanels ? 'split' : 'primary'
      }
      className="relative flex-1 min-h-0 overflow-hidden flex flex-col lg:flex-row gap-3 p-6"
      transition={transition}
    >
      <AnimatePresence initial={false}>
        {hasAgentPanels && (
          <motion.div
            key="agent-panels"
            layout
            data-testid="meeting-agent-panels"
            initial={reduceMotion ? false : { opacity: 0, x: -18 }}
            animate={{ opacity: 1, x: 0 }}
            exit={reduceMotion ? { opacity: 0 } : { opacity: 0, x: -18 }}
            transition={transition}
            className="relative flex-1 min-w-0 min-h-0 overflow-y-auto"
          >
            {agentPanels}
          </motion.div>
        )}
      </AnimatePresence>

      <motion.div
        layout
        data-testid="meeting-transcript-slot"
        transition={transition}
        initial={false}
        animate={
          panelExpanded
            ? { opacity: 0, transitionEnd: OFFSTAGE }
            : { ...onstage('block'), opacity: 1 }
        }
        className={
          hasAgentPanels
            ? 'w-full h-[42%] min-h-[260px] lg:h-full lg:w-[360px] lg:flex-none'
            : 'flex-1 min-w-0 min-h-0'
        }
      >
        {transcript}
      </motion.div>
    </motion.div>
  )
}
