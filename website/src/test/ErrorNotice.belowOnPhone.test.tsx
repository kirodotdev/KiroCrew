/**
 * ErrorNotice `actionPlacement="below-on-phone"`.
 *
 * Below `sm` (a phone) a beside hand-off squeezes the sentence into a narrow
 * column, so these notices wrap there: the message keeps the row's width and
 * Ask the agent takes the line under it. From `sm` up nothing changes. The
 * block variant always puts the hand-off in its action row. jsdom
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

  it('the block variant reads no placement: the hand-off is in the action row under the text at every width', () => {
    render(<ErrorNotice message={MSG} askAgent actionPlacement="below-on-phone" onDismiss={() => {}} testId="n" />)
    const notice = screen.getByTestId('n')
    const handoff = within(notice).getByRole('button', { name: /ask the agent/i })
    const row = notice.querySelector('[data-notice-actions]')!
    expect(row.contains(handoff)).toBe(true)
    // The message sits above the row; the dismiss stays on the first line, outside the text column.
    expect(within(notice).getByText(MSG).compareDocumentPosition(row) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(row.contains(within(notice).getByRole('button', { name: 'Dismiss' }))).toBe(false)
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
