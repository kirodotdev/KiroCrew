import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

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
 * must too. Pinned by source scan because the predicates are inline (a
 * `useMemo` body, an IIFE inside a 60-prop JSX element, and a renderer-entry
 * loop) with no seam to call.
 */
const CHAT_PAGE = readFileSync(join(__dirname, '..', 'pages', 'ChatPage.tsx'), 'utf8')
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
    const i = CHAT_PAGE.indexOf('showFooter={(() => {')
    expect(i).toBeGreaterThan(-1)
    const pred = CHAT_PAGE.slice(i, CHAT_PAGE.indexOf('})()}', i))
    expect(pred).toMatch(/if \(isSystemNoticeRow\(later\)\) continue/)
    // The skip decides "does a later row end the turn", so it must run BEFORE
    // the assistant/streaming check that returns false.
    const skipAt = pred.indexOf('if (isSystemNoticeRow(later)) continue')
    const denyAt = pred.indexOf("if (later.role === 'assistant' || later.role === 'streaming') return false")
    expect(skipAt).toBeGreaterThan(-1)
    expect(denyAt).toBeGreaterThan(-1)
    expect(skipAt).toBeLessThan(denyAt)
  })

  it('the app-sdk assistant row walk passes over notice rows', () => {
    // The SDK row set's `assistant` entry runs the same end-of-turn walk for
    // every host built on the factory (ChatPane, SideChat, ChatEmbed), and the
    // dashboard row set does not override it — it needs the same skip.
    const i = SDK_RENDERERS.indexOf('let nextRelevant = false')
    expect(i).toBeGreaterThan(-1)
    const walk = SDK_RENDERERS.slice(i, SDK_RENDERERS.indexOf('if (!nextRelevant)', i))
    expect(walk).toMatch(/if \(isSystemNoticeRow\(ctx\.messages\[j\]\)\) continue/)
    const skipAt = walk.indexOf('if (isSystemNoticeRow(ctx.messages[j])) continue')
    const denyAt = walk.indexOf("if (ctx.messages[j].role === 'assistant' || ctx.messages[j].role === 'streaming')")
    expect(skipAt).toBeGreaterThan(-1)
    expect(denyAt).toBeGreaterThan(-1)
    expect(skipAt).toBeLessThan(denyAt)
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
    expect(CHAT_PAGE).toContain("import { isSystemNoticeRow } from './chat/CompactionCard'")
  })
})
