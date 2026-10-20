/** Whether the server already holds sends an earlier page load was still warning about.
 *
 *  The loaded transcript is a bounded window, so a send whose row was pushed out of it by later
 *  output looks undelivered there. Handing such a send back as unconfirmed invites the user to
 *  send it twice, so the check reads the server's history itself, newest page first. */
import { api } from '../api/client'
import { SLOT_DETAIL_MAX_LIMIT } from '../store/chatSlice'
import type { ChatMessage } from '../types'

/** Most pages one check reads. A send is parked seconds after it went out, so the rows that can
 *  carry it are the newest ones; the cap only bounds a transcript whose rows carry no usable `ts`. */
export const RECOVERY_HISTORY_MAX_PAGES = 8

/** How far before a record's park time its row may sit: the send's own deadline, plus clock skew
 *  between this browser and the gateway that stamped the row. */
export const RECOVERY_HISTORY_SLACK_MS = 10 * 60_000

/** Every send a row stands for. A queue drain that merged several sends keeps the last one in
 *  `sendId` and names all of them in `sendIds`, so both are read. */
const sendIdsOf = (v: unknown): string[] => {
  const meta = v && typeof v === 'object' ? (v as { meta?: unknown }).meta : undefined
  if (!meta || typeof meta !== 'object') return []
  const { sendId, sendIds } = meta as { sendId?: unknown; sendIds?: unknown }
  const all = [sendId, ...(Array.isArray(sendIds) ? sendIds : [])]
  return all.filter((id): id is string => typeof id === 'string' && !!id)
}

const acceptingRow = (m: ChatMessage): boolean => (m.role === 'user' || m.role === 'queued') && !m.meta?.optimistic

/** The ids among `sendIds` the server has accepted, as a user row or a queued item.
 *
 *  `null` when the read could not prove the rest absent: a failed request, or the page cap reached
 *  before the history went back past `parkedSince - RECOVERY_HISTORY_SLACK_MS`. */
export async function acceptedInHistory(slot: string, sendIds: readonly string[], parkedSince: number): Promise<Set<string> | null> {
  const wanted = new Set(sendIds)
  const found = new Set<string>()
  const floor = parkedSince - RECOVERY_HISTORY_SLACK_MS
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
      const oldest = Date.parse(d.messages?.[0]?.ts ?? '')
      if (Number.isFinite(oldest) && oldest < floor) return found
      const next = d.next_before
      if (typeof next !== 'number' || next <= 0 || (before !== undefined && next >= before)) return null
      before = next
    }
  } catch {
    return null
  }
  return null
}
