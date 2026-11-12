/** When another person settles an approval or question this tab was showing,
 *  the card goes away; this reports the one line that says how it ended
 *  ("Bob approved"). Only cards this tab actually displayed count, so opening a
 *  session never replays old decisions, and a decision settled while the first
 *  read was still in flight is still reported. Decisions the viewer settled, the
 *  gateway settled by its approval mode, an unknown actor settled, or a question
 *  nobody answered say nothing.
 *
 *  `cards` are the request ids on screen: a decision id ends in its approval's
 *  request id, and a question's is `question:<ask id>`. A request id can recur,
 *  and the gateway then reopens the same decision id at a higher revision, so a
 *  settlement is told apart by id and revision together; one already in the
 *  cache when its card appears belongs to an earlier use of the id. Returns
 *  `markSeen(decision)` for a path that already reported a decision itself (a
 *  click that lost the race), so the line shows once, never twice. */
import { useCallback, useEffect, useRef } from 'react'
import type { SharedDecision } from '../../api/sharedCrew'
import { isUnknownPrincipal } from '../../api/sharedCrewUi'
import { useSessionDecisions } from '../../hooks/sharedCrew/useSharedCrew'
import { resolvedByLine } from './sharedDecision'

type Settlement = Pick<SharedDecision, 'decision_id' | 'revision'>
const settlementKey = (d: Settlement) => `${d.decision_id}@${d.revision}`
const forCard = (d: Settlement, id: string) => d.decision_id === `question:${id}` || d.decision_id.endsWith(`:${id}`)

export function useResolvedElsewhere(sessionKey: string | null, enabled: boolean, viewerId: string, cards: readonly string[], onSettled: (line: string, decision: SharedDecision) => void) {
  const items = useSessionDecisions(enabled ? sessionKey : null, false).data?.items
  const latest = useRef(items)
  latest.current = items
  const state = useRef<{ key: string | null; shown: Set<string>; reported: Set<string> }>({ key: null, shown: new Set(), reported: new Set() })
  const report = useRef(onSettled)
  report.current = onSettled
  const shownKey = cards.join('\n')
  useEffect(() => {
    if (state.current.key !== sessionKey) state.current = { key: sessionKey, shown: new Set(), reported: new Set() }
    const { shown, reported } = state.current
    for (const id of shownKey ? shownKey.split('\n') : []) {
      if (shown.has(id)) continue
      shown.add(id)
      for (const d of latest.current ?? []) if (d.state === 'resolved' && forCard(d, id)) reported.add(settlementKey(d))
    }
  }, [sessionKey, shownKey])
  useEffect(() => {
    if (!sessionKey || !items || !viewerId || state.current.key !== sessionKey) return
    const { shown, reported } = state.current
    for (const d of items) {
      if (d.state !== 'resolved' || reported.has(settlementKey(d))) continue
      if (!Array.from(shown).some(id => forCard(d, id))) continue
      reported.add(settlementKey(d))
      if (d.settled_by === 'host' || !d.responder || isUnknownPrincipal(d.responder) || d.responder.id === viewerId) continue
      if (d.kind === 'question' && d.outcome !== 'answered') continue
      report.current(resolvedByLine(d, viewerId), d)
    }
  }, [sessionKey, items, viewerId])
  return useCallback((d: Settlement) => { state.current.reported.add(settlementKey(d)) }, [])
}
