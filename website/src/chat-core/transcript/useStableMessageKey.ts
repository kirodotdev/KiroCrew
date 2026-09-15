/**
 * Per-message identity for transcript rows: the optimistic client timestamp,
 * else the server timestamp, else an id minted once per message OBJECT and
 * remembered in a WeakMap — never the array index, which a history prepend
 * renumbers. The same rule ChatPage's `stableMsgKey` applies; hoisted here
 * (chat-core P5-e) so every virtualized host keys rows identically and a row's
 * cached height, DOM node and scroll anchor survive a regroup or a page landing.
 */
import { useCallback, useRef } from 'react'
import type { ChatMessage } from '../../types'

export type MessageKeyFn = (m: ChatMessage) => string

export function useStableMessageKey(): MessageKeyFn {
  const seq = useRef(0)
  const ids = useRef(new WeakMap<ChatMessage, string>())
  return useCallback((m: ChatMessage): string => {
    const explicit = (m.meta?.clientTs as string | undefined) || m.ts
    if (explicit) return explicit
    let id = ids.current.get(m)
    if (!id) {
      id = `mid-${seq.current++}`
      ids.current.set(m, id)
    }
    return id
  }, [])
}
