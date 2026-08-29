/**
 * Action-chip row limit and double-click guard.
 *
 * Both properties are review findings on the zero-turn-option-actions change, and
 * both are the kind that pass a smoke test and fail in use: a third peer button
 * only appears once a row offers two content options AS WELL AS an action, and the
 * duplicate dispatch only appears on a real double-click, where both handlers land
 * in one tick before React re-renders.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, cleanup, fireEvent } from '@testing-library/react'
import FollowUpBar from '../components/FollowUpBar'
import type { OptionAction } from '../app-sdk/protocol/options'
import { parseOptions } from '../app-sdk/protocol/options'

const CLOSE: OptionAction = { action: 'close', label: 'Nothing else, close this tab' }
// The chip's accessible NAME states the effect and carries the label inside it
// (`Closes this session — <label>`) in every state, because a bare model-authored
// label named no consequence. Match the label as a substring: an exact-name match
// would pass only for the old name this PR removed.
const CLOSE_NAME = new RegExp(CLOSE.label, 'i')

afterEach(cleanup)

function renderBar(over: Partial<React.ComponentProps<typeof FollowUpBar>> = {}) {
  const onSelect = vi.fn()
  const onAction = vi.fn()
  render(
    <FollowUpBar
      options={[]}
      picked={new Set()}
      onSelect={onSelect}
      action={CLOSE}
      onAction={onAction}
      {...over}
    />,
  )
  return { onSelect, onAction }
}

describe('action chips and the two-button row limit', () => {
  for (const layout of ['multiline', 'scroll'] as const) {
    it(`keeps the action chip out of the content button row (${layout})`, () => {
      // The finding's exact shape: TWO content options plus an action chip. A third
      // PEER button breaches max-two-buttons-per-row, so the action must leave the
      // row rather than append to it.
      renderBar({ options: ['Alpha', 'Beta'], layout })
      const action = screen.getByRole('button', { name: CLOSE_NAME })
      const alpha = screen.getByRole('button', { name: 'Alpha' })
      expect(action.parentElement).not.toBe(alpha.parentElement)
      // ...and the action's own group holds only actions, never a content chip.
      const group = action.parentElement as HTMLElement
      expect(group.querySelectorAll('button')).toHaveLength(1)
      expect(group.textContent).not.toContain('Alpha')
    })
  }

  it('renders one chip per action KIND, not per label', () => {
    // `close=Yes | close=Done` is one button's worth of behaviour wearing two
    // labels. Rendering both invites a choice between chips that do the same thing,
    // and it is what could push the action group itself past two.
    //
    // Enforced by the PARSER now, not by a dedupe in the component: over a
    // one-member enum a dedupe could never fire, so `parseActionEntries` returns at
    // most one entry. Driving this from `parseOptions` tests the path both hosts
    // actually use to fill this prop.
    const parsed = parseOptions('[OPTION-ACTIONS: close=close this tab | close=Done here]')
    expect(parsed.action).toBeTruthy()
    renderBar({ action: parsed.action })
    expect(screen.getAllByRole('button', { name: /close this tab|Done here/ })).toHaveLength(1)
  })

  it('never renders more than two action chips', () => {
    // The two-button row cap is the constraint; with `close` the whole enum the cap
    // is satisfied by the parser's one-entry bound rather than by a count in the
    // view. Same assertion, driven through the real parse.
    const parsed = parseOptions(
      '[OPTION-ACTIONS: close=close this tab | close=Second | close=Third]',
    )
    renderBar({ action: parsed.action })
    const buttons = screen.getAllByRole('button')
    const actionButtons = buttons.filter(b => b.hasAttribute('data-option-action'))
    expect(actionButtons.length).toBeLessThanOrEqual(2)
  })
})

describe('action chip double-click guard', () => {
  it('dispatches ONCE for a double-click', async () => {
    // The bug: two clicks land in the SAME tick, before React re-renders, so a
    // state flag still reads false on the second and `disabled` has not applied
    // yet either. Two dispatches meant two note POSTs — two durable breadcrumbs
    // for one user action — and two close requests.
    let release: (() => void) | undefined
    const onAction = vi.fn(() => new Promise<void>(res => { release = res }))
    render(
      <FollowUpBar options={[]} picked={new Set()} onSelect={vi.fn()} action={CLOSE} onAction={onAction} />,
    )
    const chip = screen.getByRole('button', { name: CLOSE_NAME })

    fireEvent.click(chip)
    fireEvent.click(chip)
    expect(onAction).toHaveBeenCalledTimes(1)

    release?.()
  })

  it('re-arms after the dispatch settles, so a refused close is retryable', async () => {
    // A close can be refused (a teardown hook answers 500). The chip must not latch
    // permanently, or the user is left with a tab they cannot dismiss.
    let release: (() => void) | undefined
    const onAction = vi.fn(() => new Promise<void>(res => { release = res }))
    render(
      <FollowUpBar options={[]} picked={new Set()} onSelect={vi.fn()} action={CLOSE} onAction={onAction} />,
    )
    const chip = screen.getByRole('button', { name: CLOSE_NAME })

    fireEvent.click(chip)
    expect(onAction).toHaveBeenCalledTimes(1)
    release?.()
    await vi.waitFor(() => expect(chip).toHaveAttribute('aria-disabled', 'false'))

    fireEvent.click(chip)
    expect(onAction).toHaveBeenCalledTimes(2)
  })

  it('does not latch when the handler is synchronous', () => {
    // A non-promise host must not leave the chip stuck: single-threaded, so a sync
    // handler cannot re-enter on one tick anyway.
    const onAction = vi.fn()
    render(
      <FollowUpBar options={[]} picked={new Set()} onSelect={vi.fn()} action={CLOSE} onAction={onAction} />,
    )
    const chip = screen.getByRole('button', { name: CLOSE_NAME })
    fireEvent.click(chip)
    expect(chip).toHaveAttribute('aria-disabled', 'false')
    fireEvent.click(chip)
    expect(onAction).toHaveBeenCalledTimes(2)
  })
})
