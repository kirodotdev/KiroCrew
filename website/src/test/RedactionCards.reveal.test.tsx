import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, fireEvent, waitFor, cleanup } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'
import { normalizeCredentialRecords } from '../components/RedactionCards'
import { api, ApiError } from '../api/client'
import { renderAssistantBubble, type MessageRenderContext } from '../app-sdk/messageRenderers'
import { createTranscriptRenderers } from '../pages/chat/transcriptRenderers'
import type { ChatMessage } from '../types'
import { readMessageText } from '../messageActions/messageText'
import { selectionTouchesHostOnly } from '../utils/visibleText'

const copied: string[] = []
let copyResult = true
vi.mock('../utils/clipboard', async importOriginal => {
  const actual = await importOriginal<typeof import('../utils/clipboard')>()
  return {
    ...actual,
    copyToClipboard: (text: string) => {
      copied.push(text)
      return Promise.resolve(copyResult)
    },
  }
})

/**
 * The owner's reveal of a removed credential (RFC "Owner reveal"): the card
 * offers Show; the value comes from gateway memory and stays on this page
 * only until Hide. Built at runtime so no literal token shape is in source.
 */

const CRED = '[REDACTED: credential]'
const VALUE = 'ghp_' + 'k7qmz2x9wv4tb8ncr6yd3fj5hs1gpl0aeuo2'
let handleSeq = 0
const handle = () => `rv_${String(++handleSeq).padStart(22, 'a')}`

const cred = (over: Record<string, unknown> = {}) => ({
  ordinal: 0,
  rule: 'github_token',
  label: '',
  source: null,
  view_command: null,
  profile_command: null,
  reveal_id: handle(),
  ...over,
})

function openCard(record: Record<string, unknown>) {
  const view = render(<MarkdownRenderer content={`token: ${CRED}`} redactions={[record]} slotKey="s1" />)
  fireEvent.click(view.getByTestId('credential-tag'))
  return view
}

afterEach(() => {
  cleanup()
  copied.length = 0
  copyResult = true
  vi.restoreAllMocks()
})

describe('owner reveal', () => {
  it('Show asks the gateway for this slot and handle, then shows the value', async () => {
    const record = cred()
    const spy = vi.spyOn(api, 'revealRedactedValue').mockResolvedValue({ value: VALUE })
    const { getByTestId } = openCard(record)
    fireEvent.click(getByTestId('reveal-show'))
    await waitFor(() => expect(getByTestId('reveal-value').textContent).toBe(VALUE))
    expect(spy).toHaveBeenCalledWith('s1', record.reveal_id)
  })

  it('Copy copies the value; a failed copy says to select it by hand', async () => {
    vi.spyOn(api, 'revealRedactedValue').mockResolvedValue({ value: VALUE })
    const { getByTestId } = openCard(cred())
    fireEvent.click(getByTestId('reveal-show'))
    await waitFor(() => getByTestId('reveal-value'))
    copyResult = false
    fireEvent.click(getByTestId('reveal-copy'))
    await waitFor(() => expect(getByTestId('reveal-copy-error').textContent).toContain('Select the text and copy it manually'))
    expect(copied).toEqual([VALUE])
    // The value stays on screen to select.
    expect(getByTestId('reveal-value').textContent).toBe(VALUE)
  })

  it('stays shown across closing and reopening the card, until Hide', async () => {
    const spy = vi.spyOn(api, 'revealRedactedValue').mockResolvedValue({ value: VALUE })
    const view = openCard(cred())
    fireEvent.click(view.getByTestId('reveal-show'))
    await waitFor(() => view.getByTestId('reveal-value'))
    fireEvent.click(view.getByTestId('credential-tag'))
    await waitFor(() => expect(view.queryByTestId('credential-card')).toBeNull())
    fireEvent.click(view.getByTestId('credential-tag'))
    expect(view.getByTestId('reveal-value').textContent).toBe(VALUE)
    expect(spy).toHaveBeenCalledTimes(1)
    fireEvent.click(view.getByTestId('reveal-hide'))
    expect(view.queryByTestId('reveal-value')).toBeNull()
    expect(view.getByTestId('reveal-show')).toBeTruthy()
  })

  it('leaving the page drops the shown value: back again, it needs Show', async () => {
    vi.spyOn(api, 'revealRedactedValue').mockResolvedValue({ value: VALUE })
    const record = cred()
    const first = openCard(record)
    fireEvent.click(first.getByTestId('reveal-show'))
    await waitFor(() => first.getByTestId('reveal-value'))
    first.unmount()
    const again = openCard(record)
    expect(again.queryByTestId('reveal-value')).toBeNull()
    expect(again.getByTestId('reveal-show')).toBeTruthy()
  })

  it('a reveal still in flight when the page goes keeps nothing', async () => {
    let answer: (v: { value: string }) => void = () => {}
    vi.spyOn(api, 'revealRedactedValue').mockReturnValue(new Promise(r => { answer = r }))
    const record = cred()
    const first = openCard(record)
    fireEvent.click(first.getByTestId('reveal-show'))
    first.unmount()
    answer({ value: VALUE })
    await Promise.resolve()
    const again = openCard(record)
    expect(again.queryByTestId('reveal-value')).toBeNull()
  })

  it('a 410 says the value is gone and points to the source', async () => {
    vi.spyOn(api, 'revealRedactedValue').mockRejectedValue(new ApiError(410, 'gone'))
    const { getByTestId, queryByTestId } = openCard(cred())
    fireEvent.click(getByTestId('reveal-show'))
    await waitFor(() => expect(getByTestId('reveal-gone').textContent).toContain('No longer available here'))
    expect(getByTestId('reveal-gone').textContent).toContain('where it came from')
    expect(queryByTestId('reveal-value')).toBeNull()
    // Pressing Show again cannot help, so it is held.
    expect((getByTestId('reveal-show') as HTMLButtonElement).disabled).toBe(true)
  })

  it('a 403 says only the owner can show it', async () => {
    vi.spyOn(api, 'revealRedactedValue').mockRejectedValue(new ApiError(403, 'no'))
    const { getByTestId } = openCard(cred())
    fireEvent.click(getByTestId('reveal-show'))
    await waitFor(() => expect(getByTestId('reveal-denied').textContent).toContain('Only the dashboard owner'))
  })

  it('a record with no handle offers no Show', () => {
    const spy = vi.spyOn(api, 'revealRedactedValue')
    const { getByTestId, queryByTestId } = openCard(cred({ reveal_id: null }))
    expect(queryByTestId('reveal-show')).toBeNull()
    expect(getByTestId('reveal-unavailable').textContent).toBe('This value was not kept for showing here.')
    expect(spy).not.toHaveBeenCalled()
  })

  it('the value is never written to browser storage', async () => {
    vi.spyOn(api, 'revealRedactedValue').mockResolvedValue({ value: VALUE })
    const set = vi.spyOn(Storage.prototype, 'setItem')
    const { getByTestId } = openCard(cred())
    fireEvent.click(getByTestId('reveal-show'))
    await waitFor(() => getByTestId('reveal-value'))
    expect(set.mock.calls.flat().join(' ')).not.toContain(VALUE)
  })
})

describe('a card that switches to another record', () => {
  it('a gone value does not hold Show for the next handle under the same tag', async () => {
    const spy = vi.spyOn(api, 'revealRedactedValue')
      .mockRejectedValueOnce(new ApiError(410, 'gone'))
      .mockResolvedValueOnce({ value: VALUE })
    const old = cred()
    const live = cred()
    const view = render(<MarkdownRenderer content={`token: ${CRED}`} redactions={[old]} slotKey="s1" />)
    fireEvent.click(view.getByTestId('credential-tag'))
    fireEvent.click(view.getByTestId('reveal-show'))
    await waitFor(() => view.getByTestId('reveal-gone'))
    // The reply shows another variant with the same text: same tag, new handle.
    view.rerender(<MarkdownRenderer content={`token: ${CRED}`} redactions={[live]} slotKey="s1" />)
    const show = view.getByTestId('reveal-show') as HTMLButtonElement
    expect(show.disabled).toBe(false)
    expect(view.queryByTestId('reveal-gone')).toBeNull()
    fireEvent.click(show)
    await waitFor(() => expect(view.getByTestId('reveal-value').textContent).toBe(VALUE))
    expect(spy).toHaveBeenLastCalledWith('s1', live.reveal_id)
  })
})

describe('the value never leaves through the host', () => {
  it('message actions read the reply without the revealed value', async () => {
    vi.spyOn(api, 'revealRedactedValue').mockResolvedValue({ value: VALUE })
    const view = render(<div data-message-body=""><MarkdownRenderer content={`token: ${CRED}`} redactions={[cred()]} slotKey="s1" /></div>)
    fireEvent.click(view.getByTestId('credential-tag'))
    fireEvent.click(view.getByTestId('reveal-show'))
    await waitFor(() => view.getByTestId('reveal-value'))
    const text = readMessageText(view.container)
    expect(text).toContain('token:')
    expect(text).not.toContain(VALUE)
  })

  it('a selection reaching the shown value gets no Quote, Ask or app action', async () => {
    vi.spyOn(api, 'revealRedactedValue').mockResolvedValue({ value: VALUE })
    const view = render(<div data-testid="box"><MarkdownRenderer content={`intro text\n\ntoken: ${CRED}`} redactions={[cred()]} slotKey="s1" /></div>)
    fireEvent.click(view.getByTestId('credential-tag'))
    fireEvent.click(view.getByTestId('reveal-show'))
    await waitFor(() => view.getByTestId('reveal-value'))
    const box = view.getByTestId('box')
    const sel = window.getSelection()!
    sel.removeAllRanges()
    const range = document.createRange()
    range.selectNodeContents(view.getByTestId('reveal-value'))
    sel.addRange(range)
    expect(selectionTouchesHostOnly(sel, box)).toBe(true)
    sel.removeAllRanges()
    const prose = document.createRange()
    prose.selectNodeContents(view.getByText('intro text'))
    sel.addRange(prose)
    expect(selectionTouchesHostOnly(sel, box)).toBe(false)
    sel.removeAllRanges()
  })

  it('a selection reaching a value shown in another reply is caught too', async () => {
    vi.spyOn(api, 'revealRedactedValue').mockResolvedValue({ value: VALUE })
    const view = render(<>
      <div data-testid="first"><p>earlier reply</p></div>
      <div data-testid="second"><MarkdownRenderer content={`token: ${CRED}`} redactions={[cred()]} slotKey="s1" /></div>
    </>)
    fireEvent.click(view.getByTestId('credential-tag'))
    fireEvent.click(view.getByTestId('reveal-show'))
    await waitFor(() => view.getByTestId('reveal-value'))
    const sel = window.getSelection()!
    sel.removeAllRanges()
    const range = document.createRange()
    range.selectNodeContents(view.getByTestId('reveal-value'))
    sel.addRange(range)
    // The toolbar belongs to the first reply; the range is in the second.
    expect(selectionTouchesHostOnly(sel, view.getByTestId('first'))).toBe(true)
    sel.removeAllRanges()
  })

  it('a late answer after Hide does not bring the value back', async () => {
    const answers: ((v: { value: string }) => void)[] = []
    vi.spyOn(api, 'revealRedactedValue').mockImplementation(() => new Promise(r => { answers.push(r) }))
    const view = openCard(cred())
    fireEvent.click(view.getByTestId('reveal-show'))
    // Close and reopen the card while the first request is in flight, ask again.
    fireEvent.click(view.getByTestId('credential-tag'))
    await waitFor(() => expect(view.queryByTestId('credential-card')).toBeNull())
    fireEvent.click(view.getByTestId('credential-tag'))
    fireEvent.click(view.getByTestId('reveal-show'))
    answers[1]({ value: VALUE })
    await waitFor(() => view.getByTestId('reveal-value'))
    fireEvent.click(view.getByTestId('reveal-hide'))
    answers[0]({ value: VALUE })
    await Promise.resolve()
    await Promise.resolve()
    fireEvent.click(view.getByTestId('credential-tag'))
    await waitFor(() => expect(view.queryByTestId('credential-card')).toBeNull())
    fireEvent.click(view.getByTestId('credential-tag'))
    expect(view.queryByTestId('reveal-value')).toBeNull()
    expect(view.getByTestId('reveal-show')).toBeTruthy()
  })
})

describe('every host that draws this gateway\'s replies', () => {
  const row = (record: Record<string, unknown>) => ({ role: 'assistant', content: `token: ${CRED}`, ts: '2026-11-03T00:00:00Z', meta: { redactions: [record] } }) as unknown as ChatMessage
  const ctx = { messages: [], index: 0, running: false } as unknown as MessageRenderContext

  it('the shared bubble (Crew page DM, session grid) draws the lock tag and reveals in its slot', async () => {
    const record = cred()
    const spy = vi.spyOn(api, 'revealRedactedValue').mockResolvedValue({ value: VALUE })
    const { getByTestId } = render(<>{renderAssistantBubble(row(record), ctx, undefined, { redactionSlot: 'dm-slot' })}</>)
    fireEvent.click(getByTestId('credential-tag'))
    fireEvent.click(getByTestId('reveal-show'))
    await waitFor(() => expect(getByTestId('reveal-value').textContent).toBe(VALUE))
    expect(spy).toHaveBeenCalledWith('dm-slot', record.reveal_id)
  })

  it('a host with no local slot (the crew window) keeps the plain placeholder', () => {
    const { queryByTestId, container } = render(<>{renderAssistantBubble(row(cred()), ctx)}</>)
    expect(queryByTestId('credential-tag')).toBeNull()
    expect(container.textContent).toContain(CRED)
  })

  it('the transcript registry draws the reveal only when the host names its slot', () => {
    const withSlot = createTranscriptRenderers({ slot: 's2', revealSlot: 's2' })
    expect(withSlot.some(r => r.id === 'assistant')).toBe(true)
    expect(createTranscriptRenderers({ slot: 'crew:peer:s2' }).some(r => r.id === 'assistant')).toBe(false)
  })
})

describe('record shape', () => {
  it('accepts a record with or without a handle, and drops a malformed one', () => {
    const { reveal_id: _drop, ...old } = cred()
    expect(normalizeCredentialRecords([old])[0].reveal_id).toBeNull()
    expect(normalizeCredentialRecords([cred({ reveal_id: 'rv_' + 'x'.repeat(22) })])[0].reveal_id).toBe('rv_' + 'x'.repeat(22))
    expect(normalizeCredentialRecords([cred({ reveal_id: 'not-a-handle' })])).toEqual([])
    expect(normalizeCredentialRecords([cred({ reveal_id: 7 })])).toEqual([])
  })
})
