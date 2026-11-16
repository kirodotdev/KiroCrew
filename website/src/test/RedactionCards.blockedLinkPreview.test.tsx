import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { render, fireEvent, act, within } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'
import { PREVIEW_OPEN_MS } from '../components/RedactionCards'

/**
 * The hover/focus preview on a Blocked link chip: the rule in plain words, read
 * only, and never the address. Every action stays on the card. The behaviour
 * every marker's preview shares is pinned on the lock tag in
 * `RedactionCards.preview.test.tsx`.
 */

const URL = 'https://reviews.corp.example/reviews?filter=abc'

const link = (over: Record<string, unknown> = {}) => ({
  url: URL,
  domain: 'reviews.corp.example',
  rule: 'exfil_query_length',
  query_chars: 290,
  withheld: null,
  ...over,
})

const mouse = { pointerType: 'mouse' }
const preview = (testId: string) => document.querySelector(`[data-testid="${testId}"]`) as HTMLElement | null

function hover(el: Element) {
  fireEvent.pointerOver(el, mouse)
  act(() => { vi.advanceTimersByTime(PREVIEW_OPEN_MS) })
}

beforeEach(() => { vi.useFakeTimers() })
afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('Blocked link preview', () => {
  it('opens after the mouse rests on the chip and states the reason without the address', () => {
    const { getByTestId } = render(<MarkdownRenderer content={`See ${URL}`} blockedLinks={[link()]} slotKey="s1" />)
    const chip = getByTestId('blocked-link-chip')
    fireEvent.pointerOver(chip, mouse)
    act(() => { vi.advanceTimersByTime(PREVIEW_OPEN_MS - 1) })
    expect(preview('blocked-link-preview')).toBeNull()
    act(() => { vi.advanceTimersByTime(1) })
    const panel = preview('blocked-link-preview')!
    expect(panel.getAttribute('role')).toBe('tooltip')
    expect(panel.textContent).toContain('Link to reviews.corp.example blocked')
    expect(panel.textContent).toContain('Reason: the query has 290 characters.')
    // The full address appears only inside the card, after the reader asks.
    expect(panel.textContent).not.toContain('filter=abc')
    // Read-only: no control, so nothing in it can open the link or take focus.
    expect(within(panel).queryAllByRole('button')).toEqual([])
    expect(panel.querySelector('a[href], [tabindex]')).toBeNull()
    expect(getByTestId('blocked-link-inspect').getAttribute('aria-describedby')).toBe(panel.id)
  })

  it('the panel repeats no action the marker already has', () => {
    const { getByTestId, queryByTestId } = render(<MarkdownRenderer content={URL} blockedLinks={[link()]} slotKey="s1" />)
    hover(getByTestId('blocked-link-chip'))
    expect(queryByTestId('blocked-link-preview-details')).toBeNull()
    fireEvent.click(getByTestId('blocked-link-inspect'))
    expect(getByTestId('blocked-link-card')).toBeTruthy()
    expect(queryByTestId('blocked-link-open-confirm')).toBeNull()
  })

  it('a withheld address says why and offers no way to open it', () => {
    const rec = link({ withheld: 'credential', rule: 'exfil_hard_credential' })
    const { getByTestId } = render(<MarkdownRenderer content={URL} blockedLinks={[rec]} slotKey="s1" />)
    hover(getByTestId('blocked-link-chip'))
    const panel = preview('blocked-link-preview')!
    expect(getByTestId('blocked-link-preview-withheld').textContent).toContain('opening it would send that secret')
    // The withheld sentence is the reason; the rule's sentence is not repeated.
    expect(panel.textContent!.split('secret').length - 1).toBe(2)
    expect(within(preview('blocked-link-preview')!).queryAllByRole('button')).toEqual([])
  })

  it('several links to one host point to the card, which lists each', () => {
    const recs = [link(), link({ url: 'https://reviews.corp.example/other?x=1' })]
    const { getByTestId } = render(<MarkdownRenderer content={URL} blockedLinks={recs} slotKey="s1" />)
    hover(getByTestId('blocked-link-chip'))
    expect(preview('blocked-link-preview')!.textContent).toBe('Link to reviews.corp.example blockedSeveral links to this site were blocked. Select Inspect to see each one and its reason.')
    expect(within(preview('blocked-link-preview')!).queryAllByRole('button')).toEqual([])
  })

})
