import type { ChatMessage } from '../types'

/** Every send id a transcript row stands for. A plain row carries
 *  `meta.sendId`; a row the drain merged from several queued sends
 *  (`merge_queued_messages`) also names each of them in `meta.sendIds`
 *  (see `isOwnUserRow` in `app-sdk/embedSendTails.ts`). Non-string and
 *  empty entries are ignored. */
export function rowSendIds(meta: ChatMessage['meta'] | Record<string, unknown> | undefined | null): string[] {
  if (!meta) return []
  const ids: string[] = []
  const one = meta.sendId
  if (typeof one === 'string' && one) ids.push(one)
  const many = meta.sendIds
  if (Array.isArray(many)) for (const id of many) if (typeof id === 'string' && id && !ids.includes(id)) ids.push(id)
  return ids
}
