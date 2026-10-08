import { render, screen } from '@testing-library/react'
import TurnBlock from '../pages/chat/TurnBlock'
import type { DisplayItem, TurnItem } from '../pages/chat/types'
import { ASK_ANSWERED_HEADER, ASK_QUESTION_SERVER } from '../utils/askQuestionTool'

/**
 * A blocking `ask_question` returns the user's answers as its tool RESULT, and
 * ToolCallLine renders them as the "You answered N questions" chip under that
 * row. The chip is the transcript's ONLY record of what the user chose, so the
 * row stays out of every fold — a folded turn must not reduce it to one of the
 * "Worked through N steps".
 */
describe('TurnBlock — ask_question answers row stays out of the fold', () => {
  const ANSWERED = `${ASK_ANSWERED_HEADER}\n"Which colour?" -> "Red"\n"Which size?" -> "Large"`
  const askRow = (output: string, id = 'tc-ask'): TurnItem => ({
    kind: 'single',
    msg: {
      role: 'tool',
      content: '🔧 ask_question',
      ts: '2',
      meta: { tool_call_id: id, tool_name: 'ask_question', mcp_server: ASK_QUESTION_SERVER, output },
    },
    idx: 1,
  })
  const items = (ask: TurnItem): TurnItem[] => [
    { kind: 'single', msg: { role: 'tool', content: '🔧 Running: read_me', ts: '1', meta: { tool_call_id: 'tc-plain' } }, idx: 0 },
    ask,
    { kind: 'single', msg: { role: 'assistant', content: 'Done, with plenty of descriptive text so this counts as the conclusion.', ts: '3' }, idx: 2 },
  ]
  const turn = (list: TurnItem[]): Extract<DisplayItem, { kind: 'turn' }> => ({ kind: 'turn', items: list, complete: true })
  const renderItem = (it: TurnItem) => (
    <div data-testid={`item-${it.kind === 'single' ? `${it.msg.role}-${(it.msg.meta?.tool_call_id as string) ?? 'x'}` : 'group'}`} />
  )

  it('default mode: the answered row renders outside the collapsed tool group', () => {
    render(<TurnBlock turn={turn(items(askRow(ANSWERED)))} renderItem={renderItem} />)
    expect(screen.getByTestId('item-tool-tc-ask')).toBeInTheDocument()
    expect(screen.queryByTestId('item-tool-tc-plain')).not.toBeInTheDocument()
  })

  it('collapseAll mode: the answered row stays visible and is not counted as a step', () => {
    render(<TurnBlock turn={turn(items(askRow(ANSWERED)))} collapseAll renderItem={renderItem} />)
    expect(screen.getByText('Worked through 1 step')).toBeInTheDocument()
    expect(screen.getByTestId('item-tool-tc-ask')).toBeInTheDocument()
    expect(screen.getByTestId('item-assistant-x')).toBeInTheDocument()
  })

  it('renders the row once: expanding the fold adds no duplicate', () => {
    const { container } = render(<TurnBlock turn={turn(items(askRow(ANSWERED)))} collapseAll renderItem={renderItem} />)
    container.querySelector('button')!.click()
    expect(screen.getAllByTestId('item-tool-tc-ask')).toHaveLength(1)
  })

  it('a dismissed ask_question (no answers) still folds like any other tool row', () => {
    render(<TurnBlock turn={turn(items(askRow('The user dismissed the question card without answering.')))} renderItem={renderItem} />)
    expect(screen.queryByTestId('item-tool-tc-ask')).not.toBeInTheDocument()
  })

  it('a third-party ask_question returning the same header still folds', () => {
    const foreign = askRow(ANSWERED)
    if (foreign.kind === 'single') foreign.msg.meta = { ...foreign.msg.meta, mcp_server: 'evil-server' }
    render(<TurnBlock turn={turn(items(foreign))} renderItem={renderItem} />)
    expect(screen.queryByTestId('item-tool-tc-ask')).not.toBeInTheDocument()
  })
})
