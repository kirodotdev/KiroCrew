/** Healing loaded rows after a redaction allow-list change.
 *
 *  The server applies allowed hosts when it serves a transcript, so allowing or
 *  revoking a host changes rows every tab already holds, in every session of
 *  the workspace. The `dashboard` status frame (sent on connect and every few
 *  seconds) carries `redaction_hosts_gen`, an opaque generation that moves with
 *  the list, so every tab learns of every change -- including one made while its
 *  socket was down, and one made from Settings or another tab. The decision
 *  itself lives in the store (`noteRedactionHostsGen`), which the tab that made
 *  the change also reports its own write to, so the two never handle one
 *  change twice. */
import { useMemo } from 'react'
import type { AppDispatch, RootState } from '../../store'
import { noteRedactionHostsGen } from '../../store/chatSlice'

export interface RedactionHostsHeal {
  /** A `dashboard` status frame's `redaction_hosts_gen`. */
  onStatusGen(gen: unknown): void
}

export function useRedactionHostsHeal(dispatch: AppDispatch, getState: () => RootState): RedactionHostsHeal {
  return useMemo<RedactionHostsHeal>(() => ({
    onStatusGen(gen) {
      noteRedactionHostsGen(dispatch as (action: unknown) => unknown, getState, gen, 'status')
    },
  }), [dispatch, getState])
}
