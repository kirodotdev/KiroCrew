/**
 * Whether the host's crewmate greeting (ChatPane `crewmateGreeting`) is still
 * the newest thing in the chat. The greeting is drawn after the last row, so
 * once the crewmate says something newer -- a reply on a patrol / wake turn,
 * which does not take the greeting down -- a stale "where we left off" would
 * sit BELOW the fresher reply and read as the latest word. From that row on,
 * the greeting is not drawn again for this showing.
 *
 * "Newer" is a crewmate speech row at the tail that was not the tail when the
 * greeting appeared and is not stamped before that moment. The stamp check
 * keeps a transcript that lands after the greeting (older rows, so an older
 * tail) from hiding it; a live row with no stamp yet counts as new.
 */
import { useState } from 'react'
import type { ChatMessage } from '../../types'

const SPEECH_ROLES: ReadonlySet<string> = new Set(['assistant', 'streaming'])

function tailKey(m: ChatMessage | undefined): string {
  return m ? `${m.role}|${m.ts ?? ''}|${String(m.meta?.mid ?? '')}` : ''
}

/** True when `tail` is crewmate speech newer than a greeting shown at `since`. */
export function supersedesGreeting(tail: ChatMessage | undefined, since: { at: number; tail: string }): boolean {
  if (!tail || !SPEECH_ROLES.has(tail.role) || tailKey(tail) === since.tail) return false
  const stamped = tail.ts ? Date.parse(tail.ts) : NaN
  return !(stamped < since.at)
}

export function useCrewmateGreetingShown(on: boolean, messages: ChatMessage[]): boolean {
  const tail = messages[messages.length - 1]
  const [since, setSince] = useState<{ at: number; tail: string; gone: boolean } | null>(null)
  // Derived state, set during render (React's documented pattern for state
  // that follows a prop): a showing starts when the greeting appears, ends
  // when it goes, and is spent once a newer reply lands.
  if (!on) {
    if (since !== null) setSince(null)
    return false
  }
  if (since === null) {
    setSince({ at: Date.now(), tail: tailKey(tail), gone: false })
    return true
  }
  if (!since.gone && supersedesGreeting(tail, since)) {
    setSince({ ...since, gone: true })
    return false
  }
  return !since.gone
}
