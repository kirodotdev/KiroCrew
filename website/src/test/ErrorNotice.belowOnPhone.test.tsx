/**
 * ErrorNotice `actionPlacement="below-on-phone"`.
 *
 * Below `sm` (a phone) a beside hand-off squeezes the sentence into a narrow
 * column, so these notices wrap there: the message keeps the row's width and
 * Ask the agent takes the line under it. From `sm` up nothing changes. jsdom
 * cannot lay out a breakpoint, so the pins read the responsive classes; the PR
 * carries 375px and 1024px screenshots for the rendered shape.
 */
import { describe, it, expect } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import ErrorNotice from '../components/ErrorNotice'
import SpeechProviderNotice from '../components/SpeechProviderNotice'

const MSG = 'The microphone is blocked for this site.'

describe('ErrorNotice below-on-phone', () => {
  it('the inline variant wraps its row and lets the message take the line', () => {
    render(<ErrorNotice variant="inline" message={MSG} askAgent actionPlacement="below-on-phone" testId="n" />)
    const notice = screen.getByTestId('n')
    expect(notice.className).toContain('flex-wrap')
    expect(notice.className).toContain('sm:flex-nowrap')
    const text = within(notice).getByText(MSG)
    expect(text.className).toContain('basis-[calc(100%-2rem)]')
    expect(text.className).toContain('sm:basis-auto')
  })

  it('the block variant keeps the icon and the dismiss on the first line and moves the hand-off under the text', () => {
    render(<ErrorNotice message={MSG} askAgent actionPlacement="below-on-phone" onDismiss={() => {}} testId="n" />)
    const notice = screen.getByTestId('n')
    expect(notice.className).toContain('flex-wrap')
    expect(notice.className).toContain('sm:flex-nowrap')
    const column = within(notice).getByText(MSG)
    expect(column.className).toContain('basis-[calc(100%-3rem)]')
    expect(column.className).toContain('sm:flex-1')
    const handoff = within(notice).getByRole('button', { name: /ask the agent/i })
    // Outside the text column, ordered last below sm, back in the row from sm up.
    expect(column.contains(handoff)).toBe(false)
    expect(handoff.className).toContain('order-last')
    expect(handoff.className).toContain('sm:order-none')
    expect(within(notice).getAllByRole('button', { name: /ask the agent/i })).toHaveLength(1)
  })

  it('without the hand-off the shape is the default one', () => {
    const { rerender } = render(<ErrorNotice message={MSG} testId="n" />)
    const before = screen.getByTestId('n').outerHTML
    rerender(<ErrorNotice message={MSG} actionPlacement="below-on-phone" testId="n" />)
    expect(screen.getByTestId('n').outerHTML).toBe(before)
  })

  it('a failed speech provider check uses it', () => {
    render(<SpeechProviderNotice entry={{ name: 'acme', label: 'Acme', builtin: false, available: false, reason: MSG, action: null, error: true }} testId="p" askAgent />)
    expect(screen.getByTestId('p').className).toContain('sm:flex-nowrap')
  })
})
