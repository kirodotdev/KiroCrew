import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import FollowUpBar from '../components/FollowUpBar'
import { deriveFollowUpOptions, parseOptions } from '../app-sdk/protocol'
import { stripPartialGoalMarker } from '../app-sdk/protocol/goalMarker'
import { goalSuggestionReplyFallback } from '../app-sdk/protocol/options'
import type { ChatMessage } from '../types'


const user = (content: string): ChatMessage => ({ role: 'user', content, cls: 'msg msg-u' })
const assistant = (content: string): ChatMessage => ({ role: 'assistant', content, cls: 'msg msg-a' })

describe('autonomous goal suggestion protocol', () => {
  it('parses a standalone goal marker and removes it from visible prose', () => {
    const parsed = parseOptions('Recommended next action.\n\n[GOAL: Deliver the recommended next actions]')
    expect(parsed.goalSuggestion).toBe('Deliver the recommended next actions')
    expect(parsed.options).toEqual([])
    expect(parsed.text).toBe('Recommended next action.')
  })

  it('supports one autonomous goal alongside ordinary reply options', () => {
    const parsed = parseOptions([
      'Choose what happens next.',
      '[GOAL: Deliver the recommended next actions]',
      '[OPTIONS: Show me the staged diff | Keep the changes local]',
    ].join('\n'))
    expect(parsed.goalSuggestion).toBe('Deliver the recommended next actions')
    expect(parsed.options).toEqual(['Show me the staged diff', 'Keep the changes local'])
    expect(parsed.text).toBe('Choose what happens next.')
  })

  it('leaves an inline goal-shaped phrase as prose', () => {
    const content = 'The literal [GOAL: example] syntax is documented here.'
    expect(parseOptions(content)).toMatchObject({ text: content, goalSuggestion: null })
  })

  it('treats a CRLF terminator as the end of a standalone marker line', () => {
    const parsed = parseOptions('Ready.\r\n[GOAL: Deliver it]\r\n')
    expect(parsed.goalSuggestion).toBe('Deliver it')
    expect(parsed.text).toBe('Ready.')
  })

  it('does not consume the next line as a goal objective', () => {
    const content = '[GOAL:\nordinary prose]'
    expect(parseOptions(content)).toMatchObject({ text: content, goalSuggestion: null })
  })

  it('rejects a large hostile unterminated marker without changing the reply', () => {
    const content = `[GOAL:${' '.repeat(128_000)}x`
    expect(parseOptions(content)).toMatchObject({ text: content, goalSuggestion: null })
  })

  it('hides only a standalone goal marker while it is still streaming', () => {
    expect(stripPartialGoalMarker('Done.\n[GOAL: Deliver the recomm')).toBe('Done.')
    expect(stripPartialGoalMarker('The literal [GOAL: example')).toBe('The literal [GOAL: example')
  })

  it('derives a goal-only action with the source row identity', () => {
    const derived = deriveFollowUpOptions([
      user('What next?'),
      { ...assistant('Do the work.\n[GOAL: Deliver the result]'), ts: 'row-2' },
    ], false)
    expect(derived.followUpGoal).toBe('Deliver the result')
    expect(derived.followUpOptions).toEqual([])
    expect(derived.followUpSourceKey).toBe('row-2')
  })

  it('clears goal suggestions once the user replies or while a turn streams', () => {
    const messages = [user('What next?'), assistant('[GOAL: Deliver the result]')]
    expect(deriveFollowUpOptions(messages, true).followUpGoal).toBeNull()
    expect(deriveFollowUpOptions([...messages, user('Go')], false).followUpGoal).toBeNull()
  })

  it('degrades a goal to one ordinary reply without duplicating an existing label', () => {
    expect(goalSuggestionReplyFallback(['Show details'], 'Deliver it')).toEqual(['Deliver it', 'Show details'])
    expect(goalSuggestionReplyFallback(['Deliver it'], 'Deliver it')).toEqual(['Deliver it'])
    expect(goalSuggestionReplyFallback(['Show details'], null)).toEqual(['Show details'])
  })
})

describe('autonomous goal card', () => {
  it('renders separately from reply pills and opens goal review with the exact objective', () => {
    const onGoal = vi.fn()
    render(
      <FollowUpBar
        options={['Show details']}
        goal="Deliver the recommended next actions"
        picked={new Set()}
        onSelect={() => {}}
        onGoal={onGoal}
      />,
    )

    const card = screen.getByTestId('autonomous-goal-card')
    expect(card).toHaveTextContent('Autonomous goal')
    expect(card).toHaveTextContent('Keeps working on its own until done or stuck.')
    expect(screen.getByRole('button', { name: 'Show details' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Review & start' }))
    expect(onGoal).toHaveBeenCalledWith('Deliver the recommended next actions')
  })

  it('stacks the action on narrow screens and disables it when another automation is active', () => {
    render(
      <FollowUpBar
        options={[]}
        goal="Deliver the result"
        goalDisabledReason="A goal is running in this session. To start this one instead, stop it in Open goal."
        picked={new Set()}
        onSelect={() => {}}
        onGoal={() => {}}
      />,
    )

    const card = screen.getByTestId('autonomous-goal-card')
    expect(card.querySelector('.flex-col')).toHaveClass('min-[390px]:flex-row')
    expect(screen.getByRole('button', { name: 'Review & start' })).toBeDisabled()
    expect(card).toHaveTextContent('A goal is running in this session. To start this one instead, stop it in Open goal.')
  })

  it('demotes Review & start after a storage failure and makes Open goal the one action', () => {
    const onOpenGoal = vi.fn()
    render(
      <FollowUpBar
        options={[]}
        goal="Deliver the result"
        goalError="Storage is full."
        onOpenGoal={onOpenGoal}
        picked={new Set()}
        onSelect={() => {}}
        onGoal={() => {}}
      />,
    )
    const card = screen.getByTestId('autonomous-goal-card')
    expect(card).toHaveAttribute('data-state', 'failed')
    expect(screen.getByTestId('goal-save-error')).toHaveTextContent('Storage is full.')
    expect(card).toHaveTextContent('Deliver the result')
    expect(screen.getByRole('button', { name: 'Review & start' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Review & start' })).not.toHaveClass('bg-accent')
    expect(screen.getByRole('button', { name: 'Open goal' })).toHaveClass('bg-accent')
    expect(screen.queryByRole('button', { name: 'Copy goal' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Open goal' }))
    expect(onOpenGoal).toHaveBeenCalledTimes(1)
  })

  it('offers Open goal only when the card is blocked or failed', () => {
    render(
      <FollowUpBar
        options={[]}
        goal="Deliver the result"
        onOpenGoal={() => {}}
        picked={new Set()}
        onSelect={() => {}}
        onGoal={() => {}}
      />,
    )
    expect(screen.getByRole('button', { name: 'Review & start' })).toBeEnabled()
    expect(screen.queryByRole('button', { name: 'Open goal' })).not.toBeInTheDocument()
  })
})
