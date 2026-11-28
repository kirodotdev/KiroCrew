/** `ChatEmbed`'s read signal for hosts: `onRead` once per landed read, and
 *  the time of the newest landed read of the slot on screen. */
import { useEffect, useRef } from 'react'

/** What `onRead` reports about one landed read. */
export interface ChatEmbedRead {
  at: number
  running: boolean
}

interface ReadState {
  slotKey: string
  limit: number
  /** The query's data; undefined before the first read of this key. */
  data: { running?: boolean } | undefined
  isPlaceholderData: boolean
  dataUpdatedAt: number
}

/** Report each landed read to *onRead* and return the newest landed read time
 *  of this slot, or 0 before one lands. A failed read moves only
 *  `errorUpdatedAt`, and a placeholder is not a read of its key, so neither
 *  reports; the time stays on the last page that landed, as the rows do. */
export function useEmbedReadReport(state: ReadState, onRead?: (read: ChatEmbedRead) => void): number {
  const { slotKey, limit, data, isPlaceholderData, dataUpdatedAt } = state
  const readAt = !isPlaceholderData && data ? dataUpdatedAt : 0
  const running = readAt > 0 && data?.running === true

  const lastRef = useRef<{ slot: string; at: number } | null>(null)
  if (readAt) lastRef.current = { slot: slotKey, at: readAt }

  // Keyed on the read, so a host passing a fresh callback hears it once.
  const onReadRef = useRef(onRead)
  onReadRef.current = onRead
  const reportedRef = useRef('')
  useEffect(() => {
    if (!readAt) return
    const id = [slotKey, limit, readAt].join('\u0000')
    if (reportedRef.current === id) return
    reportedRef.current = id
    onReadRef.current?.({ at: readAt, running })
  }, [slotKey, limit, readAt, running])

  return lastRef.current?.slot === slotKey ? lastRef.current.at : 0
}
