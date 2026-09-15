/**
 * CompactionCard — the folded system card for a `kind="compaction"` row.
 *
 * Pins three things: the notice parser (the closed set of shapes the gateway
 * writes), the card's collapsed/expanded/failed/empty states, and that BOTH
 * registries — the dashboard row set and the store-free SDK defaults — resolve
 * a compaction row to this card rather than to the assistant bubble. The
 * registry half is the bug this file exists for: the row set had no entry, so
 * the bubble fallback painted the backend's whole context summary as a reply
 * on the main chat and in every Crew DM pane.
 */
import { describe, it, expect } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import CompactionCard, { SystemNoticeRow, isSystemNoticeRow, parseCompactionNotice } from '../pages/chat/CompactionCard'
import { defaultMessageRenderers, mergeRenderers, resolveRenderer, type MessageRenderContext } from '../app-sdk/messageRenderers'
import { createTranscriptRenderers } from '../pages/chat/transcriptRenderers'
import type { ChatMessage } from '../types'

const SUMMARY = '## Goal\nShip the compaction card.\n\n## Status\n- renderer registered\n- i18n filled'
const COMPLETED = `\u2705 Conversation compacted: ${SUMMARY}`

const msg = (role: string, over: Partial<ChatMessage> = {}): ChatMessage =>
  ({ role, content: '', cls: '', ...over }) as ChatMessage

const ctx = (over: Partial<MessageRenderContext> = {}): MessageRenderContext => ({
  index: 0,
  messages: [],
  running: false,
  key: 'k0',
  hideCardOwnedOAuth: false,
  autoDeniedIds: new Set<string>(),
  wrapper: children => children,
  row: children => children,
  ...over,
})

describe('parseCompactionNotice', () => {
  it('splits the completed notice into status + summary, dropping prefix and colon', () => {
    expect(parseCompactionNotice(COMPLETED)).toMatchObject({ status: 'completed', summary: SUMMARY, reason: '' })
  })

  it('treats the no-summary form (trailing period) as completed with an empty summary', () => {
    expect(parseCompactionNotice('\u2705 Conversation compacted.')).toMatchObject({
      status: 'completed',
      summary: '',
      reason: '',
    })
    // A stray whitespace-only tail is not a summary either.
    expect(parseCompactionNotice('\u2705 Conversation compacted:   ').summary).toBe('')
  })

  it('classifies every failure shape as failed, whichever glyph the writer used', () => {
    // ❌ from chat_utils / chat_runner; ⚠ from the manual /compact timeout and
    // state.py's threshold path. The rule classifies by origin (a failed
    // outcome), not by glyph — so all four reach the error surface.
    expect(parseCompactionNotice('\u274C Compaction failed: context too large')).toMatchObject({
      status: 'failed',
      summary: '',
      reason: 'Compaction failed: context too large',
    })
    expect(parseCompactionNotice('\u274C Compaction has failed 3x in a row (boom) — consider `/compact`.').reason).toMatch(
      /^Compaction has failed 3x/,
    )
    expect(parseCompactionNotice('\u26A0\uFE0F Compaction timed out.')).toMatchObject({
      status: 'failed',
      reason: 'Compaction timed out.',
    })
    expect(
      parseCompactionNotice('\u26A0 Auto-compact failed at 92% — will retry after cooldown. You can run `/compact` manually.'),
    ).toMatchObject({ status: 'failed', reason: expect.stringMatching(/^Auto-compact failed at 92%/) })
  })

  it('classifies every other tagged shape as a plain notice, stripping only the status glyphs', () => {
    // The tag is shared by the threshold auto-compact success line, the
    // watchdog recycle notice and the stuck-turn notice (state.py). None is a
    // failure and none is a document to fold. 🔄 / ♻️ / ⏳ are glyphs NoticeCard
    // would keep as content, so they are stripped here (no emoji as icons); a
    // ⚠ that is merely a warning (no failure wording) is left for NoticeCard's
    // own tone selector.
    const warn = '\u26A0\uFE0F Context is at 91% — a compaction is coming up.'
    expect(parseCompactionNotice(warn)).toMatchObject({ status: 'notice', summary: '', reason: '', text: warn })
    expect(parseCompactionNotice('\u{1F504} Auto-compacted at 85%.')).toMatchObject({ status: 'notice', text: 'Auto-compacted at 85%.' })
    expect(parseCompactionNotice('\u267B\uFE0F This session was recycled by the watchdog (idle).')).toMatchObject({
      status: 'notice',
      text: 'This session was recycled by the watchdog (idle).',
    })
    expect(parseCompactionNotice('\u23F3 This turn has produced nothing for 12 min.')).toMatchObject({
      status: 'notice',
      text: 'This turn has produced nothing for 12 min.',
    })
    // Only a LEADING glyph is stripped; one inside the copy is content.
    expect(parseCompactionNotice('see the \u{1F504} marker').text).toBe('see the \u{1F504} marker')
  })
})

describe('isSystemNoticeRow', () => {
  it('covers the whole SYSTEM_NOTICE_KINDS set, on either tag carrier', () => {
    expect(isSystemNoticeRow(msg('assistant', { kind: 'compaction' }))).toBe(true)
    expect(isSystemNoticeRow(msg('assistant', { meta: { kind: 'compaction' } }))).toBe(true)
    expect(isSystemNoticeRow(msg('assistant', { kind: 'session_reload' }))).toBe(true)
    expect(isSystemNoticeRow(msg('assistant', { meta: { kind: 'session_reload' } }))).toBe(true)
  })

  it('never matches an untagged assistant row (even with compaction-shaped text), a tagged non-assistant row, or an unknown kind', () => {
    expect(isSystemNoticeRow(msg('assistant', { content: COMPLETED }))).toBe(false)
    expect(isSystemNoticeRow(msg('assistant', { content: 'hi' }))).toBe(false)
    expect(isSystemNoticeRow(msg('notice', { kind: 'compaction' }))).toBe(false)
    expect(isSystemNoticeRow(msg('assistant', { kind: 'stop_event' }))).toBe(false)
  })
})

describe('CompactionCard', () => {
  it('renders collapsed by default: title visible, summary not rendered', () => {
    const { container } = render(<CompactionCard content={COMPLETED} />)
    const card = container.querySelector('[data-testid="compaction-card"]')!
    expect(card.getAttribute('data-status')).toBe('completed')
    expect(card.getAttribute('data-expanded')).toBe('false')
    expect(screen.getByText('Context compacted')).toBeInTheDocument()
    expect(container.querySelector('[data-testid="compaction-card-body"]')).toBeNull()
    expect(container.textContent).not.toContain('Ship the compaction card')
    // The ✅ is the card's job now, not the copy's.
    expect(container.textContent).not.toContain('\u2705')
    expect(container.textContent).not.toContain('Conversation compacted')
  })

  it('expands on click to a markdown body and collapses again', () => {
    const { container } = render(<CompactionCard content={COMPLETED} />)
    const toggle = screen.getByTestId('compaction-card-toggle')
    expect(toggle.getAttribute('aria-expanded')).toBe('false')
    fireEvent.click(toggle)
    expect(toggle.getAttribute('aria-expanded')).toBe('true')
    const body = container.querySelector('[data-testid="compaction-card-body"]')!
    expect(body).not.toBeNull()
    // Markdown, not raw text: the `## Goal` heading becomes an h2.
    expect(body.querySelector('h2')?.textContent).toBe('Goal')
    expect(body.textContent).toContain('Ship the compaction card.')
    // The body is a capped, internally scrolling, keyboard-reachable region.
    expect(body.classList.contains('overflow-y-auto')).toBe(true)
    expect(body.className).toMatch(/max-h-\[/)
    expect(body.getAttribute('role')).toBe('region')
    expect(body.getAttribute('tabindex')).toBe('0')
    fireEvent.click(toggle)
    expect(container.querySelector('[data-testid="compaction-card-body"]')).toBeNull()
  })

  it('draws no chevron and no button when there is no summary to expand', () => {
    const { container } = render(<CompactionCard content={'\u2705 Conversation compacted.'} />)
    expect(screen.getByText('Context compacted')).toBeInTheDocument()
    // Self-identifies as a label so it cannot be mistaken for the interactive twin.
    expect(screen.getByText('Done — no summary to show')).toBeInTheDocument()
    expect(container.textContent).not.toContain('expand to read')
    expect(container.querySelector('[data-testid="compaction-card-toggle"]')).toBeNull()
    expect(container.querySelector('button')).toBeNull()
    expect(container.querySelector('svg.lucide-chevron-right')).toBeNull()
    expect(container.querySelector('[data-testid="compaction-card"]')!.hasAttribute('data-expanded')).toBe(false)
  })

  it('renders a failure through ErrorNotice unfolded and WITHOUT the agent hand-off', () => {
    const { container } = render(<CompactionCard content={'\u274C Compaction failed: context too large'} />)
    expect(container.querySelector('[data-testid="compaction-card"]')).toBeNull()
    expect(container.querySelector('[data-testid="notice-card"]')).toBeNull()
    const alert = container.querySelector('[data-testid="compaction-card-error"]')!
    expect(alert).not.toBeNull()
    expect(alert.getAttribute('role')).toBe('alert')
    expect(alert.textContent).toContain('Compaction failed: context too large')
    expect(container.textContent).not.toContain('\u274C')
    // No hand-off: this row also renders inside embed hosts (Crew DM, SideChat,
    // ChatEmbed) whose composer draft the navigation would destroy.
    expect(screen.queryByRole('button', { name: /Ask the agent/i })).toBeNull()
    expect(container.querySelector('button')).toBeNull()
    expect(container.querySelector('[data-testid="compaction-card-toggle"]')).toBeNull()
  })

  it('draws the threshold auto-compact / recycle / stuck-turn shapes on NoticeCard, glyph stripped, not as failures', () => {
    const { container } = render(<CompactionCard content={'\u{1F504} Auto-compacted at 85%.'} />)
    const notice = container.querySelector('[data-testid="notice-card"]')!
    expect(notice).not.toBeNull()
    expect(notice.getAttribute('data-tone')).toBe('info')
    expect(screen.getByText('Auto-compacted at 85%.')).toBeInTheDocument()
    // The lucide glyph is the icon; the emoji must not survive beside it.
    expect(container.textContent).not.toContain('\u{1F504}')
    expect(container.querySelector('svg.lucide-info')).not.toBeNull()
    expect(container.querySelector('[role="alert"]')).toBeNull()
    expect(container.querySelector('[data-testid="compaction-card"]')).toBeNull()
  })

  it('routes the ⚠-led threshold failure and the timeout to ErrorNotice too (origin decides, not the glyph)', () => {
    for (const content of [
      '\u26A0 Auto-compact failed at 92% — will retry after cooldown. You can run `/compact` manually.',
      '\u26A0\uFE0F Compaction timed out.',
    ]) {
      const { container, unmount } = render(<CompactionCard content={content} />)
      expect(container.querySelector('[data-testid="notice-card"]')).toBeNull()
      const alert = container.querySelector('[data-testid="compaction-card-error"]')!
      expect(alert).not.toBeNull()
      expect(container.textContent).not.toContain('\u26A0')
      expect(container.querySelector('button')).toBeNull()
      unmount()
    }
  })

  it('drops the "expand to read" hint once the summary is open', () => {
    const { container } = render(<CompactionCard content={COMPLETED} />)
    expect(container.textContent).toContain('expand to read')
    fireEvent.click(screen.getByTestId('compaction-card-toggle'))
    expect(container.textContent).not.toContain('expand to read')
    expect(screen.getByText('Context compacted')).toBeInTheDocument()
  })

  it('shares the NoticeCard / RecoveryCard chrome so the three rows read as one family', () => {
    const { container } = render(<CompactionCard content={COMPLETED} />)
    const card = container.querySelector('[data-testid="compaction-card"]')!
    for (const cls of ['self-center', 'w-full', 'rounded-md', 'ring-1', 'ring-inset', 'ring-border', 'bg-card', 'text-muted']) {
      expect(card.classList.contains(cls)).toBe(true)
    }
    const row = card.firstElementChild!
    for (const cls of ['px-3', 'py-2', 'text-[13px]', 'leading-5', 'gap-2']) {
      expect(row.classList.contains(cls)).toBe(true)
    }
  })
})

describe('registry resolution', () => {
  const live = msg('assistant', { content: COMPLETED, kind: 'compaction' })
  const reloaded = msg('assistant', { content: COMPLETED, meta: { kind: 'compaction' } })

  it('the dashboard row set (ChatPage + ChatPane) resolves a compaction row ahead of the bubble', () => {
    const registry = mergeRenderers(createTranscriptRenderers({ slot: 's1' }))
    expect(resolveRenderer(live, registry)?.id).toBe('system_notice')
    expect(resolveRenderer(reloaded, registry)?.id).toBe('system_notice')
    // The reload confirmation is the other member of the set.
    expect(resolveRenderer(msg('assistant', { content: 'Reloading session…', meta: { kind: 'session_reload' } }), registry)?.id).toBe('system_notice')
    // Ordinary assistant rows still take the assistant entry.
    expect(resolveRenderer(msg('assistant', { content: 'hi' }), registry)?.id).toBe('assistant')
  })

  it('the SDK defaults (ChatEmbed / SideChat) resolve it too, not the assistant bubble', () => {
    expect(resolveRenderer(live, defaultMessageRenderers)?.id).toBe('system_notice')
    expect(resolveRenderer(reloaded, defaultMessageRenderers)?.id).toBe('system_notice')
  })

  it('a host entry that claims assistant with no match (ChatPage bubble) does not outrank it', () => {
    // Mirrors ChatPage.mergeRenderers([...shared, ..., bubble]).
    const bubble = { id: 'bubble', roles: ['user', 'assistant', 'streaming', 'inject'], render: () => null }
    const registry = mergeRenderers([...createTranscriptRenderers({ slot: 's1' }), bubble])
    expect(resolveRenderer(live, registry)?.id).toBe('system_notice')
  })

  it('SystemNoticeRow routes a session_reload row to NoticeCard, not CompactionCard', () => {
    const reload = msg('assistant', { content: 'Reloading session: relaunching the agent process.', meta: { kind: 'session_reload' } })
    const { container } = render(<SystemNoticeRow message={reload} />)
    expect(container.querySelector('[data-testid="notice-card"]')).not.toBeNull()
    expect(container.querySelector('[data-testid="compaction-card"]')).toBeNull()
    expect(screen.getByText('Reloading session: relaunching the agent process.')).toBeInTheDocument()
  })

  it('renders the card, not markdown prose, through the row set', () => {
    const registry = mergeRenderers(createTranscriptRenderers({ slot: 's1' }))
    const entry = resolveRenderer(live, registry)!
    const { container } = render(<>{entry.render(live, ctx({ messages: [live] }))}</>)
    expect(container.querySelector('[data-testid="compaction-card"]')).not.toBeNull()
    expect(container.textContent).not.toContain('Ship the compaction card')
  })
})
