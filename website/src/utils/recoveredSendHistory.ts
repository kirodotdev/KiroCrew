/** Whether the server already holds sends an earlier page load was still warning about.
 *
 *  The loaded transcript is a bounded window, so a send whose row was pushed out of it by later
 *  output looks undelivered there. Handing such a send back as unconfirmed invites the user to
 *  send it twice, so the check reads the server's history itself, newest page first. */
import { api } from '../api/client'
import { SLOT_DETAIL_MAX_LIMIT } from '../store/chatSlice'
import type { ChatMessage } from '../types'
import { rowSendIds } from './rowSendIds'

/** Most pages one check reads. A send is parked seconds after it went out, so the rows that can
 *  carry it are the newest ones. A check that reaches the cap proves nothing either way. */
export const RECOVERY_HISTORY_MAX_PAGES = 8

/** Every send a history or queue row stands for (see `rowSendIds`). */
const sendIdsOf = (v: unknown): string[] => {
  const meta = v && typeof v === 'object' ? (v as { meta?: unknown }).meta : undefined
  return meta && typeof meta === 'object' ? rowSendIds(meta as Record<string, unknown>) : []
}

const acceptingRow = (m: ChatMessage): boolean => (m.role === 'user' || m.role === 'queued') && !m.meta?.optimistic

/** The ids among `sendIds` the server has accepted, as a user row or a queued item.
 *
 *  `null` when the read could not prove the rest absent: a failed request, or the page cap reached
 *  before every id was found or the history ended. Row times are not used to stop early: they are
 *  the gateway's clock and a record's park time is this browser's, which can disagree. */
export async function acceptedInHistory(slot: string, sendIds: readonly string[]): Promise<Set<string> | null> {
  const wanted = new Set(sendIds)
  const found = new Set<string>()
  let before: number | undefined
  try {
    for (let page = 0; page < RECOVERY_HISTORY_MAX_PAGES; page++) {
      const d = await api.chatSlotDetail(slot, SLOT_DETAIL_MAX_LIMIT, before)
      for (const m of d.messages ?? []) {
        if (acceptingRow(m)) for (const id of sendIdsOf(m)) if (wanted.has(id)) found.add(id)
      }
      if (page === 0) {
        for (const q of d.queue ?? []) {
          for (const id of sendIdsOf(q)) if (wanted.has(id)) found.add(id)
        }
      }
      if (found.size === wanted.size || !d.has_more) return found
      const next = d.next_before
      if (typeof next !== 'number' || next <= 0 || (before !== undefined && next >= before)) return null
      before = next
    }
  } catch {
    return null
  }
  return null
}
