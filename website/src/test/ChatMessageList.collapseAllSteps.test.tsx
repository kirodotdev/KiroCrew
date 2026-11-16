/**
 * Settings -> Chat -> Show Thinking Inline reaches every dashboard chat through
 * the list's `collapseAllSteps`: a split-view pane and a pop-out crew window
 * fold a finished turn's working steps exactly as the Sessions chat does. The
 * list forwards it to TurnBlock's `collapseAll` on BOTH mounts (the plain one
 * and the virtualized one a pane uses); an embed that passes nothing keeps
 * every step in view.
 */
import { render, screen } from '@testing-library/react'
import type { ChatMessage } from '../types'
import type { TurnItem } from '../pages/chat/types'

// Draws none of the turn's rows, only the setting it received and the roles it
// holds, so a row the list puts inside a turn is absent from the DOM.
vi.mock('../pages/chat/TurnBlock', () => ({
  default: (props: { collapseAll?: boolean; turn: { items: TurnItem[] } }) => (
    <div
      data-testid="turn"
      data-collapse-all={String(!!props.collapseAll)}
      data-roles={props.turn.items.map(t => (t.kind === 'single' ? t.msg.role : 'group')).join(' ')}
    />
  ),
}))
vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <span>{content}</span>,
}))

import ChatMessageList from '../app-sdk/ChatMessageList'

const msg = (role: string, content: string, ts: string): ChatMessage => ({ role, content, cls: '', ts })
// A finished turn with a working step, so the list groups it into a turn row.
const rows = [
  msg('user', 'check the logs', '2026-01-01T00:00:01Z'),
  msg('tool', '🔧 Read logs', '2026-01-01T00:00:02Z'),
  msg('assistant', 'The logs show one failed request at noon, retried once and then served.', '2026-01-01T00:00:03Z'),
]

// A finished two-cycle auto-nudge session: each cycle's reply is long enough to
// be a turn's conclusion (TurnBlock's 50-character rule), and the nudge row is
// the prompt of the second cycle.
const firstReply = 'Cycle one: the build is green on every platform and nothing needs a retry.'
const secondReply = 'Cycle two: the build is still green and the queue has drained completely.'
const nudgeSession = [
  msg('user', 'watch the build', '2026-01-01T00:01:01Z'),
  msg('assistant', firstReply, '2026-01-01T00:01:02Z'),
  msg('nudge', '[auto-nudge cycle 2]\ncheck the build again', '2026-01-01T00:01:03Z'),
  msg('assistant', secondReply, '2026-01-01T00:01:04Z'),
]

describe.each([
  ['plain mount', undefined],
  ['virtualized mount', { sessionId: 'test:collapse-all-steps' }],
])('ChatMessageList collapseAllSteps (%s)', (_name, transcript) => {
  it('keeps every step in view when the host passes nothing', () => {
    render(<ChatMessageList messages={rows} running={false} transcript={transcript} />)
    expect(screen.getByTestId('turn')).toHaveAttribute('data-collapse-all', 'false')
  })

  it('folds the turn when the host passes the setting', () => {
    render(<ChatMessageList messages={rows} running={false} transcript={transcript} collapseAllSteps />)
    expect(screen.getByTestId('turn')).toHaveAttribute('data-collapse-all', 'true')
  })

  it('opens a turn at each auto-nudge cycle, so the fold never takes the previous cycle\'s answer', () => {
    render(<ChatMessageList messages={nudgeSession} running={false} transcript={transcript} collapseAllSteps />)
    for (const turn of screen.queryAllByTestId('turn')) {
      expect(turn.getAttribute('data-roles')?.split(' ')).not.toContain('nudge')
    }
    const nudge = screen.getByTestId('nudge-card')
    expect(nudge).toHaveAttribute('data-cycle', '2')
    expect(nudge.closest('[data-testid="turn"]')).toBeNull()
    expect(screen.getByText(firstReply)).toBeInTheDocument()
    expect(screen.getByText(secondReply)).toBeInTheDocument()
  })
})
