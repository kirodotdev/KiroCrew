// Which agent panel, if any, is expanded to fill the meeting workspace (#21179).
//
// View state only: nothing is sent to the server, and the agents and the
// transcription keep running underneath. Null is the compact split layout, the
// default.
//
// A choice whose agent leaves the roster is CLEARED, not just ignored. Ignoring
// it would leave it waiting: re-enabling that agent later would re-expand the
// workspace on its own and hide the transcript nobody asked to hide. The
// returned id is already null on the render the agent leaves, so the layout
// never shows a frame with every panel hidden; the effect then forgets it.

import { useCallback, useEffect, useState } from 'react'

export function useExpandedAgent(enabledIds: readonly string[]) {
  const [chosen, setChosen] = useState<string | null>(null)
  const present = chosen !== null && enabledIds.includes(chosen)

  useEffect(() => {
    if (chosen !== null && !present) setChosen(null)
  }, [chosen, present])

  const toggle = useCallback(
    (agentId: string) => setChosen(current => (current === agentId ? null : agentId)),
    [],
  )

  return { expandedId: present ? chosen : null, toggle }
}
