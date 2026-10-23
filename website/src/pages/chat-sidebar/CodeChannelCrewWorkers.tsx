import type { ReactNode } from 'react'

/**
 * An open Slack code channel crew's workers, drawn right under their conductor
 * row (`codeChannelCrew.ts`).
 *
 * Stepped in a little, with a rail down the left, so they read as the
 * conductor's. The step costs width every worker row then lacks, so inside this
 * wrapper (and only here) a worker's agent name yields before anything else on
 * its meta line, as a code channel conductor's does: the time keeps its room.
 */
export function CodeChannelCrewWorkers({ conductorId, children }: { conductorId: string; children: ReactNode }) {
  return (
    <div
      className="relative pl-2 [&_[data-testid=session-row-agent]]:min-w-0 [&_[data-testid=session-row-agent]]:shrink-[1000]"
      data-code-channel-crew={conductorId}
    >
      <span aria-hidden="true" className="absolute left-0.5 inset-y-1 w-px bg-border" />
      {children}
    </div>
  )
}
