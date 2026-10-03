import { useCallback, useEffect, useState } from 'react'
import { type MemberRosterRow } from '../api/client'
import { START_MEET_CREWMATES_EVENT } from '../components/MeetCrewmatesFlow'
import { useTheme } from './useTheme'

/** No crewmate beyond the always-present `default` row. The built-in
 *  Assistant (`assistant` / `kirocrew-assistant`) IS a crewmate, so a roster
 *  holding it is not empty: the page opens the Assistant instead. */
export function hasNoCrewmates(rows: readonly Pick<MemberRosterRow, 'name'>[] | undefined): boolean {
  return Array.isArray(rows) && rows.every(r => r.name === 'default')
}

/** Creation is an explicit, page-owned action. First visits open the Assistant,
 * not this flow. The legacy completion flag remains a record, not an entry gate. */
export function useMeetCrewmatesGate() {
  const { markCrewmatesOnboarded } = useTheme()
  const [open, setOpen] = useState(false)
  const [persistFailed, setPersistFailed] = useState(false)
  useEffect(() => {
    const start = () => setOpen(true)
    window.addEventListener(START_MEET_CREWMATES_EVENT, start)
    return () => window.removeEventListener(START_MEET_CREWMATES_EVENT, start)
  }, [])
  const persist = useCallback(async () => {
    try {
      await markCrewmatesOnboarded()
      setPersistFailed(false)
    } catch {
      setPersistFailed(true)
    }
  }, [markCrewmatesOnboarded])
  const onCreated = useCallback(() => { void persist() }, [persist])
  const onDone = useCallback((outcome: 'completed' | 'dismissed') => {
    setOpen(false)
    if (outcome === 'completed') void persist()
  }, [persist])
  return { open, onDone, onCreated, persistFailed }
}
