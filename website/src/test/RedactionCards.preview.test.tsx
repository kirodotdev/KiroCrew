import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest'
import { render, fireEvent, act, within } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'
import { PREVIEW_CLOSE_MS, PREVIEW_OPEN_MS, REVEAL_MS } from '../components/RedactionCards'

/**
 * The hover/focus preview on a redaction marker: the rule in plain words, read
 * only. Every action stays on the card. A press never opens it, so a tap keeps
 * opening the card.
 */

const CRED = '[REDACTED: credential]'

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

/** One lock tag in a reply, with its record. */
const tagged = () => render(<MarkdownRenderer content={`key: ${CRED}`} redactions={[cred()]} slotKey="s1" />)

describe('Marker preview', () => {
  it('opens after the mouse rests on the tag and holds no control', () => {
    const { getByTestId } = tagged()
    const tag = getByTestId('credential-tag')
    fireEvent.pointerOver(tag, mouse)
    act(() => { vi.advanceTimersByTime(PREVIEW_OPEN_MS - 1) })
    expect(preview('credential-preview')).toBeNull()
    act(() => { vi.advanceTimersByTime(1) })
    const panel = preview('credential-preview')!
    expect(panel.getAttribute('role')).toBe('tooltip')
    // Read-only: no control, so nothing in it can act or take focus.
    expect(within(panel).queryAllByRole('button')).toEqual([])
    expect(panel.querySelector('a[href], [tabindex]')).toBeNull()
    expect(tag.getAttribute('aria-describedby')).toBe(panel.id)
  })

  it('stays while the mouse crosses into the panel and closes after it leaves both', () => {
    const { getByTestId } = tagged()
    const tag = getByTestId('credential-tag')
    hover(tag)
    const panel = preview('credential-preview')!
    fireEvent.pointerOut(tag, { ...mouse, relatedTarget: panel })
    fireEvent.pointerOver(panel, { ...mouse, relatedTarget: tag })
    act(() => { vi.advanceTimersByTime(PREVIEW_CLOSE_MS * 2) })
    expect(preview('credential-preview')).not.toBeNull()
    fireEvent.pointerOut(panel, { ...mouse, relatedTarget: document.body })
    act(() => { vi.advanceTimersByTime(PREVIEW_CLOSE_MS) })
    expect(preview('credential-preview')).toBeNull()
  })

  it('a touch tap never shows the preview and keeps opening the card', () => {
    const { getByTestId } = tagged()
    const tag = getByTestId('credential-tag')
    fireEvent.pointerOver(tag, { pointerType: 'touch' })
    fireEvent.pointerDown(tag, { pointerType: 'touch' })
    act(() => { vi.advanceTimersByTime(PREVIEW_OPEN_MS) })
    // The tap's focus lands after the touch ends.
    act(() => { tag.focus() })
    fireEvent.click(tag)
    expect(preview('credential-preview')).toBeNull()
    expect(getByTestId('credential-card')).toBeTruthy()
  })

  it('keyboard focus shows it at once and leaves arrow keys to the page', () => {
    const { getByTestId } = tagged()
    const tag = getByTestId('credential-tag')
    act(() => { tag.focus() })
    expect(preview('credential-preview')).not.toBeNull()
    // Alt+ArrowDown is the turn stepper's next-turn shortcut; plain arrows scroll.
    expect(fireEvent.keyDown(tag, { key: 'ArrowDown', altKey: true })).toBe(true)
    expect(fireEvent.keyDown(tag, { key: 'ArrowDown' })).toBe(true)
    expect(document.activeElement).toBe(tag)
    fireEvent.keyDown(tag, { key: 'Escape' })
    expect(preview('credential-preview')).toBeNull()
    expect(document.activeElement).toBe(tag)
  })

  it('a preview keyboard focus opened stays when the mouse passes over and leaves', () => {
    const { getByTestId } = tagged()
    const tag = getByTestId('credential-tag')
    act(() => { tag.focus() })
    fireEvent.pointerOver(tag, mouse)
    fireEvent.pointerOut(tag, { ...mouse, relatedTarget: document.body })
    act(() => { vi.advanceTimersByTime(PREVIEW_CLOSE_MS * 2) })
    expect(preview('credential-preview')).not.toBeNull()
    act(() => { tag.blur() })
    expect(preview('credential-preview')).toBeNull()
  })

  it('closes when its marker scrolls out of the transcript scroller', () => {
    const { getByTestId, container } = render(
      <div data-testid="scroller" style={{ overflow: 'auto', height: 200 }}>
        <MarkdownRenderer content={`key: ${CRED}`} redactions={[cred()]} slotKey="s1" />
      </div>,
    )
    const scroller = container.querySelector('[data-testid="scroller"]') as HTMLElement
    const tag = getByTestId('credential-tag')
    const box = (top: number, bottom: number) => ({ top, bottom, left: 0, right: 100, width: 100, height: bottom - top, x: 0, y: top, toJSON: () => ({}) }) as DOMRect
    vi.spyOn(scroller, 'getBoundingClientRect').mockReturnValue(box(100, 300))
    const rect = vi.spyOn(tag, 'getBoundingClientRect').mockReturnValue(box(150, 170))
    hover(tag)
    fireEvent.scroll(scroller)
    expect(preview('credential-preview')).not.toBeNull()
    rect.mockReturnValue(box(60, 80))
    fireEvent.scroll(scroller)
    expect(preview('credential-preview')).toBeNull()
  })

  it('Escape closes only the preview, not a drawer or dialog listening on the document', () => {
    const outer = vi.fn()
    document.addEventListener('keydown', outer)
    try {
      const { getByTestId } = tagged()
      const tag = getByTestId('credential-tag')
      act(() => { tag.focus() })
      fireEvent.keyDown(tag, { key: 'Escape' })
      expect(preview('credential-preview')).toBeNull()
      expect(outer).not.toHaveBeenCalled()
      // With the preview closed, the next Escape reaches the outer listener.
      fireEvent.keyDown(tag, { key: 'Escape' })
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
      const { getByTestId } = tagged()
      act(() => { field.focus() })
      hover(getByTestId('credential-tag'))
      expect(preview('credential-preview')).not.toBeNull()
      fireEvent.keyDown(field, { key: 'Escape' })
      expect(preview('credential-preview')).toBeNull()
      expect(outer).toHaveBeenCalledTimes(1)
      expect(document.activeElement).toBe(field)
    } finally {
      document.removeEventListener('keydown', outer)
      field.remove()
    }
  })

  it('plain Tab on the marker is left to the browser, so a marker adds no stops', () => {
    const { getByTestId } = tagged()
    const tag = getByTestId('credential-tag')
    act(() => { tag.focus() })
    expect(fireEvent.keyDown(tag, { key: 'Tab' })).toBe(true)
    expect(document.activeElement).toBe(tag)
  })

  it('stays hidden while the card is open, and the focus the card returns does not reopen it', () => {
    const { getByTestId } = tagged()
    const tag = getByTestId('credential-tag')
    act(() => { tag.focus() })
    fireEvent.click(tag)
    act(() => { vi.advanceTimersByTime(REVEAL_MS) })
    hover(tag)
    expect(preview('credential-preview')).toBeNull()
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(document.activeElement).toBe(tag)
    expect(preview('credential-preview')).toBeNull()
  })

  it('the focus a card returns does not reopen it after focus left the card for the page', () => {
    const { getByTestId } = tagged()
    const tag = getByTestId('credential-tag')
    fireEvent.click(tag)
    act(() => { vi.advanceTimersByTime(REVEAL_MS + 2000) })
    // A click on the transcript text leaves focus on the body, with no relatedTarget.
    act(() => { (document.activeElement as HTMLElement | null)?.blur() })
    expect(document.activeElement).toBe(document.body)
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(document.activeElement).toBe(tag)
    expect(preview('credential-preview')).toBeNull()
  })
})

describe('Credential preview', () => {
  it('names the rule and says the value cannot be shown, with no reveal', () => {
    const { getByTestId, queryByTestId } = render(<MarkdownRenderer content={`key: ${CRED}`} redactions={[cred()]} slotKey="s1" />)
    hover(getByTestId('credential-tag'))
    const panel = preview('credential-preview')!
    expect(panel.textContent).toContain('Credential removed before saving')
    expect(panel.textContent).toContain('Matched: a value after an aws_secret_access_key label.')
    expect(getByTestId('credential-preview-floor').textContent).toBe('Never saved. The owner can show it from the card while this gateway runs.')
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
