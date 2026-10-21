import type { ReactNode } from 'react'

/**
 * An open Slack code channel crew's workers, drawn right under their conductor
 * row (`codeChannelCrew.ts`).
 *
 * They share the sidebar's one content column (no per-depth step, per
 * docs/decisions/2026-10-09-folder-glyph-sits-on-its-connector-line.md): a
 * rail drawn out of flow inside the rows' own left pad marks them as the
 * conductor's without moving their content. Inside this wrapper (and only
 * here) a worker's agent name yields before anything else on its meta line,
 * as a code channel conductor's does: the time keeps its room.
 */
export function CodeChannelCrewWorkers({ conductorId, children }: { conductorId: string; children: ReactNode }) {
  return (
    <div
      className="relative [&_[data-testid=session-row-agent]]:min-w-0 [&_[data-testid=session-row-agent]]:shrink-[1000]"
      data-code-channel-crew={conductorId}
    >
      <span aria-hidden="true" className="absolute left-0.5 inset-y-1 w-px bg-border" />
      {children}
    </div>
  )
}
