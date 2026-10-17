import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync } from 'node:fs'
import { join } from 'node:path'
import type { ChatMessage } from '../types'
import { replyShowsFooter } from '../pages/chat/replyFooter'

/**
 * REGRESSION GUARD — a completed turn's footer survives a LATER, unrelated run.
 *
 * A footer rule that ends at a bare "is the session idle" check lets any run
 * starting in the slot — a cron, a monitor cycle, another tab, a background job
 * with nothing to do with this turn — retract the footer of a turn that has
 * already finished.
 *
 * Measured frame by frame from a 60fps phone recording (1320x2868): the stats
 * line, the timestamp row and the overflow trigger vanished for 3 frames (50ms),
 * removing ~108px at the transcript's very bottom edge. Content shrinking THERE
 * is the one place it costs the reader their position: a bottom-parked reader is
 * clamped down by the engine, and when the footer returns nothing ever pushes
 * them back up. Each flicker ratcheted them 108px away from the bottom.
 *
 * A message carrying turn stats is proof its turn ended, so that is the
 * condition that makes the footer permanent. The rule lives in
 * `pages/chat/replyFooter.ts`; this pins that ChatPage draws from it.
 */
// ChatPage composes owners under pages/chat/page/ (and its useChatPage* siblings), so
// the pins read the page and those owners.
const PAGES_DIR = join(__dirname, '..', 'pages')
const OWNER_DIR = join(PAGES_DIR, 'chat', 'page')
const SRC = [
  readFileSync(join(PAGES_DIR, 'ChatPage.tsx'), 'utf8'),
  ...readdirSync(OWNER_DIR).filter(f => /\.tsx?$/.test(f) && !/\.test\./.test(f)).sort()
    .map(f => readFileSync(join(OWNER_DIR, f), 'utf8')),
  ...readdirSync(PAGES_DIR).filter(f => /^useChatPage.*\.tsx?$/.test(f) && !/\.test\./.test(f)).sort()
    .map(f => readFileSync(join(PAGES_DIR, f), 'utf8')),
].join('\n')

const row = (role: string, meta?: Record<string, unknown>): ChatMessage =>
  ({ role, content: `zzq-${role}`, cls: '', meta }) as ChatMessage

describe('completed-turn footer', () => {
  it('ChatPage draws its reply footer from the shared rule', () => {
    expect(SRC).toContain('showFooter={replyShowsFooter(m, messagesRef.current, i, slotRunning)}')
  })

  it('is not retracted by a run that started later', () => {
    const done = row('assistant', { turn_stats: { elapsed_ms: 59_000, credits: 1.98 } })
    expect(replyShowsFooter(done, [row('user'), done], 1, true)).toBe(true)
  })

  it('still withholds the footer while the turn that owns it is running', () => {
    // The guard must not become "always show": a streaming turn has no stats yet,
    // and its footer would claim a measurement that does not exist.
    const running = row('assistant')
    const streaming = row('streaming')
    expect(replyShowsFooter(running, [row('user'), running], 1, true)).toBe(false)
    expect(replyShowsFooter(streaming, [row('user'), streaming], 1, false)).toBe(false)
  })
})
