/**
 * The demo's one canned assistant reply, streamed over the fake dashboard
 * socket in the same frame order a real turn uses: a status label, `chat_chunk`
 * text with a rising `seq`, the final `chat_message`, then `chat_done`.
 */
import { pushFrame } from './fakeSocket'
import { nextSeq, updateState } from './state'

// Content the "gateway" returns, so it is data rather than a UI string.
const REPLY = [
  "Thanks for trying Kiro Crew! This is a preview build, so I'm not connected to a real agent right now and can't act on your message.",
  '',
  'In the full app I can work through tasks with you, run jobs on a schedule, remember what you tell me across sessions, and hand larger pieces of work to helper agents.',
].join('\n')

const THINK_MS = 900
const CHUNK_MS = 45
const CHUNK_WORDS = 3

function chunks(text: string): string[] {
  const words = text.split(/(\s+)/)
  const out: string[] = []
  for (let i = 0; i < words.length; i += CHUNK_WORDS * 2) out.push(words.slice(i, i + CHUNK_WORDS * 2).join(''))
  return out
}

export function streamReply(slot: string): void {
  const parts = chunks(REPLY)
  setTimeout(() => pushFrame('chat_status', { slot, status: 'Thinking' }), 150)
  parts.forEach((content, i) => {
    setTimeout(() => pushFrame('chat_chunk', { slot, seq: nextSeq(), content }), THINK_MS + i * CHUNK_MS)
  })
  setTimeout(() => {
    const ts = new Date().toISOString()
    const mid = `demo-a-${nextSeq()}`
    const message = { role: 'assistant', content: REPLY, cls: 'msg msg-a', ts, meta: { mid } }
    updateState((s) => {
      const row = s.slots.find((x) => x.key === slot)
      if (!row) return
      row.messages.push(message)
      row.last_ts = ts
    })
    pushFrame('chat_message', { slot, ...message })
    pushFrame('chat_done', { slot })
  }, THINK_MS + parts.length * CHUNK_MS + 100)
}
