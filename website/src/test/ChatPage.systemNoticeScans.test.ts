import { describe, it, expect } from 'vitest'
import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import type { ChatMessage } from '../types'
import { replyShowsFooter } from '../pages/chat/replyFooter'

/**
 * REGRESSION GUARD — every "last assistant in turn" scan skips system-notice rows.
 *
 * The gateway injects compaction / session-reload notices as assistant-role
 * rows (`kind` live, `meta.kind` reloaded). A trailing notice used to (a) make
 * the `showFooter` walks return false for the real reply — hiding its
 * Copy/footer — and (b) become `lastTextIdx`, anchoring Regenerate/variant
 * switching on the notice card (issue #10115). `CompactionCard.tsx` exports
 * `isSystemNoticeRow` as "one predicate shared with the last-real-message
 * scans", and `completedTurns.ts` already follows that convention; these scans
 * must too. Pinned by source scan where the predicate is inline (a `useMemo`
 * body and a callback) with no seam to call; the footer walk is a function
 * (`replyShowsFooter`), so it is exercised directly.
 */
// ChatPage composes owners under pages/chat/page/ (and its useChatPage* siblings),
// so the pins read the page and those owners.
const PAGES_DIR = join(__dirname, '..', 'pages')
const OWNER_DIR = join(PAGES_DIR, 'chat', 'page')
const CHAT_PAGE = [
  readFileSync(join(PAGES_DIR, 'ChatPage.tsx'), 'utf8'),
  ...readdirSync(OWNER_DIR).filter(f => /\.tsx?$/.test(f) && !/\.test\./.test(f)).sort()
    .map(f => readFileSync(join(OWNER_DIR, f), 'utf8')),
  ...readdirSync(PAGES_DIR).filter(f => /^useChatPage.*\.tsx?$/.test(f) && !/\.test\./.test(f)).sort()
    .map(f => readFileSync(join(PAGES_DIR, f), 'utf8')),
].join('\n')
const SDK_RENDERERS = readFileSync(join(__dirname, '..', 'app-sdk', 'messageRenderers.tsx'), 'utf8')

describe('last-assistant scans skip system-notice rows', () => {
  it('lastTextIdx passes over notice rows', () => {
    const i = CHAT_PAGE.indexOf('const lastTextIdx = useMemo(() => {')
    expect(i).toBeGreaterThan(-1)
    const body = CHAT_PAGE.slice(i, CHAT_PAGE.indexOf('}, [messages])', i))
    expect(body).toContain('!isSystemNoticeRow(messages[i])')
    // The invisible-row skip must survive alongside the new one.
    expect(body).toContain('!isHiddenInvisibleAssistantRow(messages[i])')
  })

  it('showFooter walk passes over notice rows', () => {
    // A notice row between the reply and the row that ends its turn must not
    // read as "another reply follows". Both surfaces draw from this one rule.
    const reply = { role: 'assistant', content: 'zzq-answer', cls: '' } as ChatMessage
    const notice = { role: 'assistant', content: 'zzq-summary', cls: '', meta: { kind: 'compaction' } } as ChatMessage
    const user = { role: 'user', content: 'zzq-next', cls: '' } as ChatMessage
    expect(replyShowsFooter(reply, [reply, notice, user], 0, true)).toBe(true)
    expect(replyShowsFooter(reply, [reply, notice], 0, false)).toBe(true)
  })

  it('the app-sdk assistant row and ChatPage share the footer walk', () => {
    // The SDK row set's `assistant` entry runs the end-of-turn walk for every
    // host built on the factory (ChatPane, SideChat, ChatEmbed), and the
    // dashboard row set does not override it: it must be the same rule.
    expect(SDK_RENDERERS).toContain('replyShowsFooter(m, ctx.messages, ctx.index, ctx.running)')
    expect(CHAT_PAGE).toContain('replyShowsFooter(m, messagesRef.current, i, slotRunning)')
  })

  it('the regenerate truncation walk passes over notice rows', () => {
    // handleRegenerate mirrors chat_regenerate.py's scan: both sides must
    // skip system notices or the variant stash captures the notice and the
    // real reply is silently dropped from variant history.
    const i = CHAT_PAGE.indexOf('const handleRegenerate = useCallback(')
    expect(i).toBeGreaterThan(-1)
    const body = CHAT_PAGE.slice(i, CHAT_PAGE.indexOf('api.regenerateSlot', i))
    expect(body).toContain('if (isSystemNoticeRow(messages[i])) continue')
  })

  it('the shared predicate is imported from CompactionCard', () => {
    expect(CHAT_PAGE).toContain("import { isSystemNoticeRow } from '../CompactionCard'")
  })
})
