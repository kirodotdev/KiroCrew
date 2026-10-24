import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { render, fireEvent, act, within } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'
import { PREVIEW_CLOSE_MS, PREVIEW_OPEN_MS, REVEAL_MS } from '../components/RedactionCards'

/**
 * The hover/focus preview on a redaction marker: the rule in plain words, read
 * only. Every action stays on the card. A press never opens it, so a tap keeps
 * opening the card.
 */

const PH = (d: string) => `[REDACTED: suspicious URL to ${d}]`
const CRED = '[REDACTED: credential]'

const link = (over: Record<string, unknown> = {}) => ({
  domain: 'reviews.corp.example',
  rule: 'exfil_query_length',
  path: '/reviews',
  query_chars: 290,
  url: 'https://reviews.corp.example/reviews?filter=abc',
  url_withheld: null,
  ...over,
})

const cred = (over: Record<string, unknown> = {}) => ({
  ordinal: 0,
  rule: 'aws_secret_access_key',
  label: 'aws_secret_access_key = ',
  source: { type: 'file', path: '~/.aws/credentials', section: 'default' },
  view_command: 'aws configure get aws_secret_access_key --profile default',
  profile_command: null,
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
    const { getByTestId } = render(<MarkdownRenderer content={`See ${PH('reviews.corp.example')}`} blockedLinks={[link()]} slotKey="s1" />)
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
    const { getByTestId, queryByTestId } = render(<MarkdownRenderer content={PH('reviews.corp.example')} blockedLinks={[link()]} slotKey="s1" />)
    hover(getByTestId('blocked-link-chip'))
    expect(queryByTestId('blocked-link-preview-details')).toBeNull()
    fireEvent.click(getByTestId('blocked-link-inspect'))
    expect(getByTestId('blocked-link-card')).toBeTruthy()
    expect(queryByTestId('blocked-link-open-confirm')).toBeNull()
  })

  it('a withheld address says why and offers no way to open it', () => {
    const rec = link({ url: null, url_withheld: 'credential', rule: 'exfil_credential' })
    const { getByTestId } = render(<MarkdownRenderer content={PH('reviews.corp.example')} blockedLinks={[rec]} slotKey="s1" />)
    hover(getByTestId('blocked-link-chip'))
    const panel = preview('blocked-link-preview')!
    expect(getByTestId('blocked-link-preview-withheld').textContent).toContain('opening it would send that secret')
    // The withheld sentence is the reason; the rule's sentence is not repeated.
    expect(panel.textContent!.split('secret').length - 1).toBe(2)
    expect(within(preview('blocked-link-preview')!).queryAllByRole('button')).toEqual([])
  })

  it('several links to one host point to the card, which lists each', () => {
    const recs = [link(), link({ path: '/other', url: 'https://reviews.corp.example/other?x=1' })]
    const { getByTestId } = render(<MarkdownRenderer content={PH('reviews.corp.example')} blockedLinks={recs} slotKey="s1" />)
    hover(getByTestId('blocked-link-chip'))
    expect(preview('blocked-link-preview')!.textContent).toBe('Link to reviews.corp.example blockedSeveral links to this site were blocked. Select Inspect to see each one and its reason.')
    expect(within(preview('blocked-link-preview')!).queryAllByRole('button')).toEqual([])
  })

  it('stays while the mouse crosses into the panel and closes after it leaves both', () => {
    const { getByTestId } = render(<MarkdownRenderer content={PH('reviews.corp.example')} blockedLinks={[link()]} slotKey="s1" />)
    const chip = getByTestId('blocked-link-chip')
    hover(chip)
    const panel = preview('blocked-link-preview')!
    fireEvent.pointerOut(chip, { ...mouse, relatedTarget: panel })
    fireEvent.pointerOver(panel, { ...mouse, relatedTarget: chip })
    act(() => { vi.advanceTimersByTime(PREVIEW_CLOSE_MS * 2) })
    expect(preview('blocked-link-preview')).not.toBeNull()
    fireEvent.pointerOut(panel, { ...mouse, relatedTarget: document.body })
    act(() => { vi.advanceTimersByTime(PREVIEW_CLOSE_MS) })
    expect(preview('blocked-link-preview')).toBeNull()
  })

  it('a touch tap never shows the preview and keeps opening the card', () => {
    const { getByTestId } = render(<MarkdownRenderer content={PH('reviews.corp.example')} blockedLinks={[link()]} slotKey="s1" />)
    const inspect = getByTestId('blocked-link-inspect')
    fireEvent.pointerOver(inspect, { pointerType: 'touch' })
    fireEvent.pointerDown(inspect, { pointerType: 'touch' })
    act(() => { vi.advanceTimersByTime(PREVIEW_OPEN_MS) })
    // The tap's focus lands after the touch ends.
    act(() => { inspect.focus() })
    fireEvent.click(inspect)
    expect(preview('blocked-link-preview')).toBeNull()
    expect(getByTestId('blocked-link-card')).toBeTruthy()
  })

  it('keyboard focus shows it at once and leaves arrow keys to the page', () => {
    const { getByTestId } = render(<MarkdownRenderer content={PH('reviews.corp.example')} blockedLinks={[link()]} slotKey="s1" />)
    const inspect = getByTestId('blocked-link-inspect')
    act(() => { inspect.focus() })
    expect(preview('blocked-link-preview')).not.toBeNull()
    // Alt+ArrowDown is the turn stepper's next-turn shortcut; plain arrows scroll.
    expect(fireEvent.keyDown(inspect, { key: 'ArrowDown', altKey: true })).toBe(true)
    expect(fireEvent.keyDown(inspect, { key: 'ArrowDown' })).toBe(true)
    expect(document.activeElement).toBe(inspect)
    fireEvent.keyDown(inspect, { key: 'Escape' })
    expect(preview('blocked-link-preview')).toBeNull()
    expect(document.activeElement).toBe(inspect)
  })

  it('a preview keyboard focus opened stays when the mouse passes over and leaves', () => {
    const { getByTestId } = render(<MarkdownRenderer content={PH('reviews.corp.example')} blockedLinks={[link()]} slotKey="s1" />)
    const chip = getByTestId('blocked-link-chip')
    const inspect = getByTestId('blocked-link-inspect')
    act(() => { inspect.focus() })
    fireEvent.pointerOver(chip, mouse)
    fireEvent.pointerOut(chip, { ...mouse, relatedTarget: document.body })
    act(() => { vi.advanceTimersByTime(PREVIEW_CLOSE_MS * 2) })
    expect(preview('blocked-link-preview')).not.toBeNull()
    act(() => { inspect.blur() })
    expect(preview('blocked-link-preview')).toBeNull()
  })

  it('closes when its marker scrolls out of the transcript scroller', () => {
    const { getByTestId, container } = render(
      <div data-testid="scroller" style={{ overflow: 'auto', height: 200 }}>
        <MarkdownRenderer content={PH('reviews.corp.example')} blockedLinks={[link()]} slotKey="s1" />
      </div>,
    )
    const scroller = container.querySelector('[data-testid="scroller"]') as HTMLElement
    const chip = getByTestId('blocked-link-chip')
    const box = (top: number, bottom: number) => ({ top, bottom, left: 0, right: 100, width: 100, height: bottom - top, x: 0, y: top, toJSON: () => ({}) }) as DOMRect
    vi.spyOn(scroller, 'getBoundingClientRect').mockReturnValue(box(100, 300))
    const rect = vi.spyOn(chip, 'getBoundingClientRect').mockReturnValue(box(150, 170))
    hover(chip)
    fireEvent.scroll(scroller)
    expect(preview('blocked-link-preview')).not.toBeNull()
    rect.mockReturnValue(box(60, 80))
    fireEvent.scroll(scroller)
    expect(preview('blocked-link-preview')).toBeNull()
  })

  it('Escape closes only the preview, not a drawer or dialog listening on the document', () => {
    const outer = vi.fn()
    document.addEventListener('keydown', outer)
    try {
      const { getByTestId } = render(<MarkdownRenderer content={PH('reviews.corp.example')} blockedLinks={[link()]} slotKey="s1" />)
      const inspect = getByTestId('blocked-link-inspect')
      act(() => { inspect.focus() })
      fireEvent.keyDown(inspect, { key: 'Escape' })
      expect(preview('blocked-link-preview')).toBeNull()
      expect(outer).not.toHaveBeenCalled()
      // With the preview closed, the next Escape reaches the outer listener.
      fireEvent.keyDown(inspect, { key: 'Escape' })
      expect(outer).toHaveBeenCalledTimes(1)
    } finally {
      document.removeEventListener('keydown', outer)
    }
  })

  it('a preview opened by hover alone closes on Escape and lets it reach the focused field', () => {
    const outer = vi.fn()
    document.addEventListener('keydown', outer)
    const field = document.createElement('textarea')
    document.body.appendChild(field)
    try {
      const { getByTestId } = render(<MarkdownRenderer content={PH('reviews.corp.example')} blockedLinks={[link()]} slotKey="s1" />)
      act(() => { field.focus() })
      hover(getByTestId('blocked-link-inspect'))
      expect(preview('blocked-link-preview')).not.toBeNull()
      fireEvent.keyDown(field, { key: 'Escape' })
      expect(preview('blocked-link-preview')).toBeNull()
      expect(outer).toHaveBeenCalledTimes(1)
      expect(document.activeElement).toBe(field)
    } finally {
      document.removeEventListener('keydown', outer)
      field.remove()
    }
  })

  it('plain Tab on the marker is left to the browser, so a marker adds no stops', () => {
    const { getByTestId } = render(<MarkdownRenderer content={PH('reviews.corp.example')} blockedLinks={[link()]} slotKey="s1" />)
    const inspect = getByTestId('blocked-link-inspect')
    act(() => { inspect.focus() })
    expect(fireEvent.keyDown(inspect, { key: 'Tab' })).toBe(true)
    expect(document.activeElement).toBe(inspect)
  })

  it('stays hidden while the card is open, and the focus the card returns does not reopen it', () => {
    const { getByTestId } = render(<MarkdownRenderer content={PH('reviews.corp.example')} blockedLinks={[link()]} slotKey="s1" />)
    const inspect = getByTestId('blocked-link-inspect')
    act(() => { inspect.focus() })
    fireEvent.click(inspect)
    act(() => { vi.advanceTimersByTime(REVEAL_MS) })
    hover(getByTestId('blocked-link-chip'))
    expect(preview('blocked-link-preview')).toBeNull()
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(document.activeElement).toBe(inspect)
    expect(preview('blocked-link-preview')).toBeNull()
  })

  it('the focus a card returns does not reopen it after focus left the card for the page', () => {
    const { getByTestId } = render(<MarkdownRenderer content={PH('reviews.corp.example')} blockedLinks={[link()]} slotKey="s1" />)
    const inspect = getByTestId('blocked-link-inspect')
    fireEvent.click(inspect)
    act(() => { vi.advanceTimersByTime(REVEAL_MS + 2000) })
    // A click on the transcript text leaves focus on the body, with no relatedTarget.
    act(() => { (document.activeElement as HTMLElement | null)?.blur() })
    expect(document.activeElement).toBe(document.body)
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(document.activeElement).toBe(inspect)
    expect(preview('blocked-link-preview')).toBeNull()
  })
})

describe('Credential preview', () => {
  it('names the rule and says the value cannot be shown, with no reveal', () => {
    const { getByTestId, queryByTestId } = render(<MarkdownRenderer content={`key: ${CRED}`} redactions={[cred()]} slotKey="s1" />)
    hover(getByTestId('credential-tag'))
    const panel = preview('credential-preview')!
    expect(panel.textContent).toContain('Credential removed before saving')
    expect(panel.textContent).toContain('Matched: a value after an aws_secret_access_key label.')
    expect(getByTestId('credential-preview-floor').textContent).toBe('The value was never saved, so nothing here can show it.')
    expect(queryByTestId('redaction-preview-actions')).toBeNull()
  })

  it('names a session token as one', () => {
    const { getByTestId } = render(<MarkdownRenderer content={`token: ${CRED}`} redactions={[cred({ rule: 'aws_session_token', label: '' })]} slotKey="s1" />)
    hover(getByTestId('credential-tag'))
    expect(preview('credential-preview')!.textContent).toContain('Session token removed before saving')
  })

  it('a click on the tag opens the credential card and toggles it shut', () => {
    const { getByTestId, queryByTestId } = render(<MarkdownRenderer content={`key: ${CRED}`} redactions={[cred()]} slotKey="s1" />)
    const tag = getByTestId('credential-tag')
    hover(tag)
    fireEvent.pointerDown(tag, mouse)
    fireEvent.click(tag)
    expect(getByTestId('credential-card')).toBeTruthy()
    fireEvent.pointerDown(tag, mouse)
    fireEvent.click(tag)
    expect(tag.getAttribute('aria-expanded')).toBe('false')
    expect(queryByTestId('credential-preview')).toBeNull()
  })
})
