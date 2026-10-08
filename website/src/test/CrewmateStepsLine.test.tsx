import { describe, it, expect, afterEach } from 'vitest'
import { render, screen, cleanup, fireEvent } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { Provider } from 'react-redux'
import { configureStore } from '@reduxjs/toolkit'
import chatReducer from '../store/chatSlice'
import CrewmateStepsLine from '../pages/chat/CrewmateStepsLine'
import type { CrewmateSteps } from '../components/chat/crewmateBubbles'
import type { ChatMessage } from '../types'

/* The one line a crewmate's run of steps folds into (Crew page chat): collapsed
 * by default, the live one names the current step and counts the steps, the
 * finished one is a quiet "Worked through N steps", and either opens on click or Enter to
 * the rows it folded. */

afterEach(() => cleanup())

const call = (title: string, meta: Record<string, unknown> = {}): ChatMessage => ({ role: 'tool', content: `🔧 ${title}`, cls: '', meta })
const STEPS = [
  call('Verify heads and build approve/RC bodies'),
  call('Post 5 approvals'),
  call('Merge clean PRs', { purpose: 'Merge the clean PRs' }),
]

function renderLine(steps: CrewmateSteps, slotDetail?: Record<string, unknown>) {
  const initial = chatReducer(undefined, { type: '@@init' })
  const chat = slotDetail ? { ...initial, slotStatusDetail: { 'member-radar': slotDetail } } : initial
  const store = configureStore({ reducer: { chat: chatReducer }, preloadedState: { chat } as never })
  return render(
    <Provider store={store}>
      <CrewmateStepsLine steps={steps} slot="member-radar">
        {() => steps.steps.map((s, i) => <div key={i} data-testid="step-row">{s.content}</div>)}
      </CrewmateStepsLine>
    </Provider>,
  )
}

describe('CrewmateStepsLine', () => {
  it('live: one collapsed line with the working mark, the current step and the count', () => {
    renderLine({ steps: STEPS, count: 3, live: true })
    const button = screen.getByRole('button', { expanded: false })
    expect(button).toHaveTextContent('Merge the clean PRs')
    expect(button).toHaveTextContent('3 steps')
    expect(screen.getByTestId('crewmate-steps-mark')).toBeInTheDocument()
    expect(screen.queryAllByTestId('step-row')).toHaveLength(0)
  })

  it("live: the slot's live tool status names the current step when it has one", () => {
    renderLine({ steps: STEPS, count: 3, live: true }, { kind: 'tool', purpose: 'Rebase onto main', toolName: 'git rebase origin/main' })
    expect(screen.getByRole('button')).toHaveTextContent('Rebase onto main')
  })

  it('live: thinking after the last tool call reads as thinking', () => {
    renderLine({ steps: [...STEPS, { role: 'thinking', content: 'hmm', cls: '' }], count: 3, live: true })
    expect(screen.getByRole('button')).toHaveTextContent('Thinking')
  })

  it('the working mark stops moving under prefers-reduced-motion', () => {
    renderLine({ steps: STEPS, count: 3, live: true })
    const mark = screen.getByTestId('crewmate-steps-mark')
    expect(mark.getAttribute('class')).toContain('animate-spin')
    expect(mark.getAttribute('class')).toContain('motion-reduce:animate-none')
  })

  it('finished: a quiet "Worked through N steps" with no working mark', () => {
    renderLine({ steps: STEPS, count: 3, live: false })
    expect(screen.getByRole('button', { expanded: false })).toHaveTextContent('Worked through 3 steps')
    expect(screen.queryByTestId('crewmate-steps-mark')).toBeNull()
  })

  it('a single step is singular', () => {
    renderLine({ steps: STEPS.slice(0, 1), count: 1, live: false })
    expect(screen.getByRole('button')).toHaveTextContent('Worked through 1 step')
  })

  it('live: the title carries a tooltip, so a narrow screen can still read it', () => {
    renderLine({ steps: STEPS, count: 3, live: true })
    expect(screen.getByTitle('Merge the clean PRs')).toBeInTheDocument()
  })

  it('live: a listener hears the working status the footer would have announced', () => {
    renderLine({ steps: STEPS, count: 3, live: true })
    expect(screen.getByRole('status')).toHaveTextContent('Thinking')
  })

  it('live and open: the heading keeps only the count, the running row names the step', () => {
    renderLine({ steps: STEPS, count: 3, live: true })
    const button = screen.getByRole('button')
    fireEvent.click(button)
    expect(button).toHaveTextContent('3 steps')
    expect(button).not.toHaveTextContent('Merge the clean PRs')
  })

  it('before the first step: a plain live "Thinking" line with nothing to open', () => {
    renderLine({ steps: [], count: 0, live: true })
    expect(screen.queryByRole('button')).toBeNull()
    expect(screen.getByTestId('crewmate-steps')).toHaveTextContent('Thinking')
    expect(screen.getByTestId('crewmate-steps-mark')).toBeInTheDocument()
  })

  it('opens on click to the full list, and closes again', () => {
    renderLine({ steps: STEPS, count: 3, live: true })
    const button = screen.getByRole('button')
    fireEvent.click(button)
    expect(button).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getAllByTestId('step-row')).toHaveLength(3)
    expect(button.getAttribute('aria-controls')).toBe(screen.getByTestId('crewmate-steps-list').id)
    fireEvent.click(button)
    expect(button).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryAllByTestId('step-row')).toHaveLength(0)
  })

  it('opens from the keyboard with Enter', async () => {
    renderLine({ steps: STEPS, count: 3, live: false })
    const user = userEvent.setup()
    await user.tab()
    expect(screen.getByRole('button')).toHaveFocus()
    await user.keyboard('{Enter}')
    expect(screen.getByRole('button')).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getAllByTestId('step-row')).toHaveLength(3)
  })
})
