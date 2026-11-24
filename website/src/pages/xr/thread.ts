/**
 * A chat transcript as the lines an XR panel draws. A message is plain text
 * and a run of tool rows is one short line, so the panel reads like the
 * conversation it is. Nothing is dropped without a mark: a message cut for
 * length says how much is missing, and code inside a fence is kept exactly.
 */
import type { ChatMessage } from '../../types'
import type { XrLine } from './panels'
import { plainText } from './text'

/** Messages asked for per page. "Load earlier" asks for one more page. */
export const XR_THREAD_PAGE = 60
/** A single message longer than this is cut, and the line says by how much. */
export const XR_MESSAGE_MAX = 6000

export function linesFrom(messages: readonly ChatMessage[], max = XR_THREAD_PAGE): XrLine[] {
  const out: XrLine[] = []
  for (const m of messages) {
    const ts = m.ts ? Math.floor(Date.parse(m.ts) / 1000) || 0 : 0
    const raw = plainText(m.rawText ?? m.content)
    if (!raw) continue
    if (m.role === 'user' || m.role === 'assistant') {
      const chars = [...raw]
      const cut = Math.max(0, chars.length - XR_MESSAGE_MAX)
      out.push({ who: m.role === 'user' ? 'you' : 'bot', text: cut ? chars.slice(0, XR_MESSAGE_MAX).join('') : raw, ts, cut })
    } else {
      const line = raw.split('\n')[0].slice(0, 160)
      // A run of tool rows says little; keep the last one and count the run.
      const prev = out[out.length - 1]
      if (prev?.who === 'tool') out[out.length - 1] = { who: 'tool', text: line, ts, cut: 0, run: (prev.run ?? 1) + 1 }
      else out.push({ who: 'tool', text: line, ts, cut: 0, run: 1 })
    }
  }
  return out.slice(-max)
}
