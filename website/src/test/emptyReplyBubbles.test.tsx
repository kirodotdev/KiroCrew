/**
 * A reply row with nothing left to draw renders no `message-bubble`.
 *
 * Two shapes reached a Bot's chat as empty gray bubbles:
 * - a quiet turn (`nothing_to_do`) that wrote a file: the runner anchors the
 *   turn's file chips to a content-less assistant row, and a Bot's chat hides
 *   file chips, so nothing was left inside the bubble;
 * - a reply that is only the `[STEERING …]` note a policy-block notice asks
 *   for: the note is stripped and that turn's Steered chip is dropped.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import type { ChatMessage } from '../types'
import { mergeRenderers, resolveRenderer, type MessageRenderContext } from '../app-sdk/messageRenderers'
import { isHiddenReplyAt } from '../app-sdk/turnPolicyBlock'
import { createTranscriptRenderers } from '../pages/chat/transcriptRenderers'
import { filterCrewmateChat } from '../components/chat/crewmateBubbles'
import { replyShowsFooter, replyTurnStats } from '../pages/chat/replyFooter'
import { deriveFollowUpOptions } from '../app-sdk/protocol/options'

vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <div data-testid="md">{content}</div>,
}))
vi.mock('../hooks/useSmoothStream', () => ({ useSmoothStream: (content: string) => content }))
vi.mock('../components/FileChangeChips', () => ({
  default: ({ fileChanges }: { fileChanges: { path: string }[] }) => <div data-testid="fcc">{fileChanges.map(fc => fc.path).join(',')}</div>,
}))

afterEach(() => cleanup())

const row = (role: string, content: string, meta?: Record<string, unknown>): ChatMessage =>
  ({ role, content, cls: '', ts: '2026-11-13T12:00:00Z', meta } as unknown as ChatMessage)

const STATS = { turn_stats: { elapsed_ms: 229_000, credits: 23.35 } }
const STEER_NOTE = '[STEERING steer-0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d: Stopped retrying the blocked command.]'
const BLOCKED_NOTICE =
  '[Tool blocked — reason sent to the agent] policy\n[Kiro Crew host notice] The tool call you just made ' +
  'was blocked by a Kiro Crew safety policy.\n\nBlocked: Running: bash -c x: Blocked by security policy: deny-rule\n'

/** A heartbeat wake that wrote its scratch file and ended on `nothing_to_do`. */
const QUIET_WROTE_FILE: ChatMessage[] = [
  row('nudge', '[auto-nudge cycle 7]\nPatrol.', { nudge: true }),
  row('tool', '🔧 Write File', { tool_call_id: 't1' }),
  row('tool', '🔧 @kirocrew-core/nothing_to_do', { tool_call_id: 't2', ends_turn: true }),
  row('assistant', '', { file_changes: [{ path: 'scratch/briefing.md', before: '', after: 'x' }] }),
]

/** A turn whose shell call a safety policy blocked, answered only by the note. */
const POLICY_STEER_ONLY: ChatMessage[] = [
  row('nudge', '[work-ledger wake]', { nudge: true }),
  row('tool', '🔧 Check the scratch state', { tool_call_id: 't1' }),
  row('inject', BLOCKED_NOTICE),
  row('tool', '🚫 bash -c x', { tool_call_id: 't2' }),
  row('assistant', STEER_NOTE, STATS),
]

const ctx = (messages: ChatMessage[], index: number): MessageRenderContext => ({
  index,
  messages,
  running: false,
  key: `k${index}`,
  hideCardOwnedOAuth: false,
  autoDeniedIds: new Set<string>(),
  wrapper: (children) => children,
  row: (children) => children,
})

function draw(messages: ChatMessage[], opts: Parameters<typeof createTranscriptRenderers>[0]) {
  const index = messages.length - 1
  const m = messages[index]
  const entry = resolveRenderer(m, mergeRenderers(createTranscriptRenderers(opts)))
  render(<>{entry?.render(m, ctx(messages, index))}</>)
}

const bot = (messages: ChatMessage[]) => ({ slot: 'member-bot', crewmate: { name: 'Bot' }, crewmateTranscript: () => messages })

describe('a quiet turn that only wrote files', () => {
  it("draws no bubble in a Bot's chat, and the chat filter drops the row", () => {
    expect(filterCrewmateChat(QUIET_WROTE_FILE)).toEqual([])
    draw(QUIET_WROTE_FILE, bot(QUIET_WROTE_FILE))
    expect(screen.queryByTestId('message-bubble')).toBeNull()
  })

  it('keeps the row on an ordinary transcript, where its file chips are the content', () => {
    draw(QUIET_WROTE_FILE, { slot: 's1' })
    expect(screen.getByTestId('fcc')).toHaveTextContent('scratch/briefing.md')
  })
})

describe('a reply that is only a steering note after a policy block', () => {
  it("draws no bubble on an ordinary transcript or in a Bot's chat", () => {
    draw(POLICY_STEER_ONLY, { slot: 's1' })
    expect(screen.queryByTestId('message-bubble')).toBeNull()
    expect(screen.queryByTestId('turn-stats')).toBeNull()
    cleanup()
    expect(filterCrewmateChat(POLICY_STEER_ONLY).filter(m => m.role === 'assistant')).toEqual([])
    draw(POLICY_STEER_ONLY, bot(POLICY_STEER_ONLY))
    expect(screen.queryByTestId('message-bubble')).toBeNull()
  })

  it('hands the footer to the visible reply above it in the same turn', () => {
    const messages = [...POLICY_STEER_ONLY.slice(0, 4), row('assistant', 'Checked the docs.'), POLICY_STEER_ONLY[4]]
    expect(isHiddenReplyAt(messages[5], messages, 5)).toBe(true)
    expect(replyShowsFooter(messages[4], messages, 4, false)).toBe(true)
    expect(replyTurnStats(messages[4], messages, 4)).toEqual(STATS.turn_stats)
  })

  it('keeps the relocated footer while a recovery continuation streams', () => {
    const visible = row('assistant', 'Checked the docs.')
    const messages = [
      ...POLICY_STEER_ONLY.slice(0, 4),
      visible,
      POLICY_STEER_ONLY[4],
      row('inject', '[Interrupted turn — automatic recovery]', { injectKind: 'recovery' }),
      row('streaming', 'Continuing.'),
    ]

    expect(replyTurnStats(visible, messages, 4)).toEqual(STATS.turn_stats)
    expect(replyShowsFooter(visible, messages, 4, true)).toBe(true)
  })

  it("draws the turn's elapsed time and cost under the visible reply above it", () => {
    const messages = [...POLICY_STEER_ONLY.slice(0, 4), row('assistant', 'Checked the docs.'), POLICY_STEER_ONLY[4]]
    const entry = resolveRenderer(messages[4], mergeRenderers(createTranscriptRenderers({ slot: 's1' })))
    render(<>{entry?.render(messages[4], ctx(messages, 4))}</>)
    expect(screen.getByTestId('turn-stats')).toBeInTheDocument()
  })

  it("reads hidden turn stats from a Bot chat's unfiltered transcript", () => {
    const visible = row('assistant', 'Checked the docs.')
    const unfiltered = [...POLICY_STEER_ONLY.slice(0, 4), visible, POLICY_STEER_ONLY[4]]
    const filtered = filterCrewmateChat(unfiltered)
    const index = filtered.indexOf(visible)
    const entry = resolveRenderer(visible, mergeRenderers(createTranscriptRenderers(bot(unfiltered))))

    expect(index).toBeGreaterThanOrEqual(0)
    expect(filtered).not.toContain(POLICY_STEER_ONLY[4])
    render(<>{entry?.render(visible, ctx(filtered, index))}</>)
    expect(screen.getByTestId('turn-stats')).toBeInTheDocument()
  })

  it('classifies a deferred visible reply instead of the shifted live row', () => {
    const hidden = row('assistant', '\u200b')
    const visible = row('assistant', 'Visible reply.')
    const deferred = [hidden, visible]
    const liveMessages = [row('user', 'Prepended history.'), ...deferred]

    expect(isHiddenReplyAt(visible, liveMessages, 1)).toBe(false)
  })

  it('still draws, with its Steered chip, when the person sent the steer', () => {
    const messages = [row('user', 'Do the read-only check instead.'), row('tool', '🔧 Check', { tool_call_id: 't1' }), row('assistant', STEER_NOTE, STATS)]
    expect(isHiddenReplyAt(messages[2], messages, 2)).toBe(false)
    draw(messages, { slot: 's1' })
    expect(screen.getByTestId('message-bubble')).toBeInTheDocument()
    expect(screen.getByText('Steered')).toBeInTheDocument()
  })

  it('leaves the option chips of the question above it on offer', () => {
    const messages = [...POLICY_STEER_ONLY.slice(0, 4), row('assistant', 'Which one?\n[OPTIONS: Fix it | Skip it]'), POLICY_STEER_ONLY[4]]
    expect(deriveFollowUpOptions(messages, false).followUpOptions).toEqual(['Fix it', 'Skip it'])
  })
})
