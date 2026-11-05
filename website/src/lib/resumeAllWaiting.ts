/** The sessions a Resume-all run this tab is waiting on has not reached yet.
 *
 *  The run resumes a few sessions at a time and starts the next as an earlier one
 *  ends, so a candidate can sit interrupted for a while after the press. Its
 *  sidebar row would still say "Turn interrupted · Resume" and invite a press the
 *  run is about to make. The row reads this set instead and says the session is
 *  waiting. `useResumeAll` writes it from the keys it counted at the press and
 *  empties it when the run's outcome arrives or the wait ends; a row reads only
 *  its own key, so a change re-renders the rows whose answer changed. */
import { useSyncExternalStore } from 'react'

let waiting: ReadonlySet<string> = new Set()
const listeners = new Set<() => void>()

function subscribe(listener: () => void): () => void {
  listeners.add(listener)
  return () => { listeners.delete(listener) }
}

/** Replace the waiting set; an empty list clears it. */
export function setResumeAllWaiting(keys: readonly string[]): void {
  if (keys.length === 0 && waiting.size === 0) return
  waiting = new Set(keys)
  for (const listener of listeners) listener()
}

/** Whether the session *key* is a candidate of the run this tab waits on. */
export function useResumeAllWaiting(key: string): boolean {
  return useSyncExternalStore(subscribe, () => waiting.has(key), () => false)
}
