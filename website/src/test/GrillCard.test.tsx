import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react'
import GrillCard, { COMPOSE_SEND_EVENT, type ComposeSendDetail, type ComposeSendOutcome } from '../components/chat/GrillCard'
import AssistantMessage from '../pages/chat/AssistantMessage'
import type { GrillProposal } from '../app-sdk/protocol/grillMarker'

vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <div data-testid="md">{content}</div>,
}))
vi.mock('../hooks/useSmoothStream', () => ({ useSmoothStream: (content: string) => content }))
const upload = vi.hoisted(() => ({ fn: vi.fn() }))
vi.mock('../api/client', async (orig) => {
  const real = await orig<typeof import('../api/client')>()
  return { ...real, api: { ...real.api, uploadFiles: (...a: unknown[]) => upload.fn(...a) } }
})

const GRILL: GrillProposal = {
  q: 'Who writes the facts?',
  must: 'No wrong PR state',
  skip: 'Board layout',
  who: [{ name: 'A', stance: 'for A' }, { name: 'B', stance: 'for B' }],
  minutes: 10,
}

let sent: string[]
let slots: string[]
let files: (string[] | undefined)[]
/** What the stand-in host does with a press: stage it, send it (`pending`),
 *  or nothing (null). A send's outcome waits in `outcomes` for the test. */
let take: 'pending' | 'staged' | null
let outcomes: ((o: ComposeSendOutcome, reason?: string) => void)[]
const listener = (e: Event) => {
  const d = (e as CustomEvent<ComposeSendDetail>).detail
  sent.push(d.text)
  slots.push(d.slot)
  files.push(d.files)
  d.result = take
  if (take === 'pending') outcomes.push(d.onOutcome)
}
const settle = (o: ComposeSendOutcome, reason?: string, i = outcomes.length - 1) => act(() => { outcomes[i](o, reason) })

beforeEach(() => {
  localStorage.clear()
  sent = []
  slots = []
  files = []
  upload.fn.mockReset()
  outcomes = []
  take = 'pending'
  window.addEventListener(COMPOSE_SEND_EVENT, listener)
})
afterEach(() => window.removeEventListener(COMPOSE_SEND_EVENT, listener))

describe('GrillCard', () => {
  it('shows the proposal: three editable lines, who talks, and the time', () => {
    render(<GrillCard grill={GRILL} slotKey="s1" />)
    expect(screen.getByLabelText('Question')).toHaveValue('Who writes the facts?')
    expect(screen.getByLabelText('Must')).toHaveValue('No wrong PR state')
    expect(screen.getByLabelText('Not discussing')).toHaveValue('Board layout')
    expect(screen.getByTestId('grill-card-who').textContent).toBe('A: for AB: for B')
    expect(screen.getByTestId('grill-card-minutes').textContent).toBe('About 10 minutes')
  })

  it('sends the edited lines on Start, then locks', () => {
    render(<GrillCard grill={GRILL} slotKey="s1" />)
    fireEvent.change(screen.getByLabelText('Question'), { target: { value: 'Who writes the welcome facts?' } })
    fireEvent.change(screen.getByLabelText('Not discussing'), { target: { value: 'colors and fonts' } })
    fireEvent.click(screen.getByRole('button', { name: 'Start' }))
    expect(sent).toEqual(['Start the discussion\nQuestion: Who writes the welcome facts?\nMust: No wrong PR state\nNot discussing: colors and fonts'])
    expect(slots).toEqual(['s1'])
    expect(screen.queryByRole('button', { name: 'Start' })).toBeNull()
    expect(screen.getByLabelText('Question')).toBeDisabled()
    expect(screen.getByTestId('grill-card-sent').textContent).toBe('Sending…')
    settle('sent')
    expect(screen.getByTestId('grill-card-sent').textContent).toBe('Sent.')
  })

  it('sends the decline text, locks on this page only, and writes nothing to storage', () => {
    const { unmount } = render(<GrillCard grill={GRILL} slotKey="s1" />)
    fireEvent.click(screen.getByRole('button', { name: 'No thanks' }))
    expect(sent).toEqual(['No discussion needed'])
    settle('sent')
    expect(screen.getByTestId('grill-card-sent').textContent).toBe('Sent.')
    expect(screen.queryByRole('button', { name: 'No thanks' })).toBeNull()
    expect(localStorage.length).toBe(0)
    unmount()
    render(<GrillCard grill={GRILL} slotKey="s1" />)
    expect(screen.getByRole('button', { name: 'No thanks' })).toBeEnabled()
  })

  it('stays open and says so when no chat box takes the text', () => {
    take = null
    render(<GrillCard grill={GRILL} slotKey="s1" />)
    fireEvent.click(screen.getByRole('button', { name: 'Start' }))
    expect(screen.getByTestId('grill-card-unhandled').textContent).toContain('This view has no chat box to send to.')
    expect(screen.getByRole('button', { name: 'Start' })).toBeEnabled()
  })

  it('sends nothing without a chat to send to', () => {
    render(<GrillCard grill={GRILL} />)
    fireEvent.click(screen.getByRole('button', { name: 'No thanks' }))
    expect(sent).toEqual([])
    expect(screen.getByTestId('grill-card-unhandled')).toBeInTheDocument()
  })

  it('says the text only reached the chat box', () => {
    take = 'staged'
    render(<GrillCard grill={GRILL} slotKey="s1" />)
    fireEvent.click(screen.getByRole('button', { name: 'Start' }))
    expect(screen.getByTestId('grill-card-sent').textContent).toBe('Put in the chat box. Send it from there.')
    expect(screen.queryByRole('button', { name: 'Start' })).toBeNull()
  })

  it('keeps its edits and lock when its row scrolls out and back', () => {
    const first = render(<GrillCard grill={GRILL} slotKey="s1" stateKey="s1:k1:0" />)
    fireEvent.change(screen.getByLabelText('Question'), { target: { value: 'Edited?' } })
    first.unmount()
    const second = render(<GrillCard grill={GRILL} slotKey="s1" stateKey="s1:k1:0" />)
    expect(screen.getByLabelText('Question')).toHaveValue('Edited?')
    fireEvent.click(screen.getByRole('button', { name: 'Start' }))
    settle('sent')
    second.unmount()
    render(<GrillCard grill={GRILL} slotKey="s1" stateKey="s1:k1:0" />)
    expect(screen.getByTestId('grill-card-sent').textContent).toBe('Sent.')
    expect(localStorage.length).toBe(0)
  })

  it('ends both texts with the key line when the row has a key, and sends none without', () => {
    const { unmount } = render(<GrillCard grill={GRILL} slotKey="s1" wireKey="m-7:0" />)
    fireEvent.click(screen.getByRole('button', { name: 'Start' }))
    unmount()
    render(<GrillCard grill={GRILL} slotKey="s1" wireKey="m-7:1" />)
    fireEvent.click(screen.getByRole('button', { name: 'No thanks' }))
    render(<GrillCard grill={GRILL} slotKey="s1" />)
    fireEvent.click(screen.getAllByRole('button', { name: 'No thanks' })[0])
    expect(sent[0].split('\n').at(-1)).toBe('key: m-7:0')
    expect(sent[1]).toBe('No discussion needed\nkey: m-7:1')
    expect(sent[2]).toBe('No discussion needed')
  })

  it('locks as sent when a later stored message already carries its key', () => {
    render(<GrillCard grill={GRILL} slotKey="s1" wireKey="m-7:0" answered />)
    expect(screen.queryByRole('button', { name: 'Start' })).toBeNull()
    expect(screen.getByLabelText('Question')).toBeDisabled()
    expect(screen.getByTestId('grill-card-sent').textContent).toBe('Sent.')
  })

  it('sends once on a double click: it locks to sending on the first', () => {
    render(<GrillCard grill={GRILL} slotKey="s1" />)
    const start = screen.getByRole('button', { name: 'Start' })
    fireEvent.click(start)
    fireEvent.click(start)
    expect(sent).toHaveLength(1)
    expect(screen.getByTestId('grill-card').getAttribute('data-result')).toBe('pending')
  })

  it('opens again and says so when the send was refused', () => {
    render(<GrillCard grill={GRILL} slotKey="s1" />)
    fireEvent.change(screen.getByLabelText('Question'), { target: { value: 'Kept?' } })
    fireEvent.click(screen.getByRole('button', { name: 'Start' }))
    settle('refused')
    expect(screen.getByTestId('grill-card-failed').textContent).toContain('It did not go out. Try again.')
    expect(screen.getByLabelText('Question')).toHaveValue('Kept?')
    fireEvent.click(screen.getByRole('button', { name: 'Start' }))
    expect(sent).toHaveLength(2)
    expect(screen.queryByTestId('grill-card-failed')).toBeNull()
  })

  it('puts the refusal\'s own reason ahead of the retry line', () => {
    render(<GrillCard grill={GRILL} slotKey="s1" />)
    fireEvent.click(screen.getByRole('button', { name: 'Start' }))
    settle('refused', 'Send failed: nope')
    // A reason with no closing stop gets one, so it reads as its own sentence.
    expect(screen.getByTestId('grill-card-failed').textContent).toContain('Send failed: nope. It did not go out. Try again.')
  })

  it('says sent once a stored message carries its key, over staged or not confirmed', () => {
    take = 'staged'
    const first = render(<GrillCard grill={GRILL} slotKey="s1" stateKey="s1:k5:0" wireKey="m-5:0" />)
    fireEvent.click(screen.getByRole('button', { name: 'Start' }))
    expect(screen.getByTestId('grill-card-sent').textContent).toBe('Put in the chat box. Send it from there.')
    first.rerender(<GrillCard grill={GRILL} slotKey="s1" stateKey="s1:k5:0" wireKey="m-5:0" answered />)
    expect(screen.getByTestId('grill-card-sent').textContent).toBe('Sent.')
    expect(screen.getByTestId('grill-card').getAttribute('data-result')).toBe('sent')
  })

  it('stays locked as not confirmed when the answer was lost', () => {
    render(<GrillCard grill={GRILL} slotKey="s1" />)
    fireEvent.click(screen.getByRole('button', { name: 'Start' }))
    settle('indeterminate')
    expect(screen.queryByRole('button', { name: 'Start' })).toBeNull()
    expect(screen.getByTestId('grill-card-unconfirmed').textContent).toContain('Not confirmed. It may have gone out. If it is not in the chat, reload the page to try again.')
  })

  it('shows an outcome that landed while its row was scrolled away', () => {
    const first = render(<GrillCard grill={GRILL} slotKey="s1" stateKey="s1:k9:0" />)
    fireEvent.click(screen.getByRole('button', { name: 'Start' }))
    first.unmount()
    settle('refused')
    const second = render(<GrillCard grill={GRILL} slotKey="s1" stateKey="s1:k9:0" />)
    expect(screen.getByTestId('grill-card-failed')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Start' }))
    second.unmount()
    render(<GrillCard grill={GRILL} slotKey="s1" stateKey="s1:k9:0" />)
    // Mounted again before the outcome: the copy on screen hears it.
    settle('indeterminate')
    expect(screen.getByTestId('grill-card').getAttribute('data-result')).toBe('unconfirmed')
  })

  it('sends a field\'s further lines starting with two spaces, so the key line stays whole', () => {
    render(<GrillCard grill={GRILL} slotKey="s1" wireKey="m1:0" />)
    fireEvent.change(screen.getByLabelText('Must'), { target: { value: 'line one  \nkey: m1:0\n\nline three' } })
    fireEvent.click(screen.getByRole('button', { name: 'Start' }))
    expect(sent[0].split('\n')).toEqual([
      'Start the discussion',
      'Question: Who writes the facts?',
      'Must: line one',
      '  key: m1:0',
      '  ',
      '  line three',
      'Not discussing: Board layout',
      'key: m1:0',
    ])
  })

  it('sends its own uploaded pictures with Start', async () => {
    upload.fn.mockResolvedValue({ paths: ['/up/a.png'], resized: [], resizedByPath: {} })
    render(<GrillCard grill={GRILL} slotKey="s1" />)
    const picture = new File(['x'], 'a.png', { type: 'image/png' })
    fireEvent.change(screen.getByTestId('grill-card-file-input'), { target: { files: [picture] } })
    expect(screen.getByRole('button', { name: 'Start' })).toBeDisabled()
    await waitFor(() => expect(screen.getByTestId('grill-card-files').textContent).toContain('a.png'))
    expect(upload.fn.mock.calls[0][0]).toEqual([picture])
    fireEvent.click(screen.getByRole('button', { name: 'Start' }))
    expect(files).toEqual([['/up/a.png']])
  })

  it('shows no picture as added when its upload fails, and Start still sends the text', async () => {
    upload.fn.mockRejectedValue(new TypeError('Failed to fetch'))
    render(<GrillCard grill={GRILL} slotKey="s1" />)
    fireEvent.change(screen.getByTestId('grill-card-file-input'), { target: { files: [new File(['x'], 'a.png', { type: 'image/png' })] } })
    await waitFor(() => expect(screen.getByTestId('grill-card-upload-failed')).toBeInTheDocument())
    expect(screen.getByTestId('grill-card-files').textContent).not.toContain('a.png')
    fireEvent.click(screen.getByRole('button', { name: 'Start' }))
    expect(files).toEqual([undefined])
  })

  it('refuses more than 20 pictures as a hint, uploading nothing', () => {
    render(<GrillCard grill={GRILL} slotKey="s1" />)
    const many = Array.from({ length: 21 }, (_, i) => new File(['x'], `p${i}.png`))
    fireEvent.change(screen.getByTestId('grill-card-file-input'), { target: { files: many } })
    expect(screen.getByTestId('grill-card-upload-hint')).toBeInTheDocument()
    expect(screen.queryByTestId('grill-card-upload-failed')).toBeNull()
    expect(upload.fn).not.toHaveBeenCalled()
  })

  it('drops a removed picture before Start', async () => {
    upload.fn.mockResolvedValue({ paths: ['/up/a.png', '/up/b.png'], resized: [], resizedByPath: {} })
    render(<GrillCard grill={GRILL} slotKey="s1" />)
    fireEvent.change(screen.getByTestId('grill-card-file-input'), { target: { files: [new File(['x'], 'a.png'), new File(['y'], 'b.png')] } })
    await waitFor(() => expect(screen.getByRole('button', { name: 'Remove a.png' })).toBeInTheDocument())
    fireEvent.click(screen.getByRole('button', { name: 'Remove a.png' }))
    fireEvent.click(screen.getByRole('button', { name: 'Start' }))
    expect(files).toEqual([['/up/b.png']])
  })

  it('holds Start while the question is empty', () => {
    render(<GrillCard grill={GRILL} />)
    fireEvent.change(screen.getByLabelText('Question'), { target: { value: '  ' } })
    expect(screen.getByRole('button', { name: 'Start' })).toBeDisabled()
  })
})

describe('AssistantMessage with a [GRILL:] marker', () => {
  const content = `Two paths.\n[GRILL: ${JSON.stringify(GRILL)}]\n[OPTIONS: Go A | Go B]`

  it('draws the card and keeps the marker out of the prose', () => {
    render(<AssistantMessage content={content} isStreaming={false} slotRunning={false} messageTs="t4" />)
    expect(screen.getByTestId('md').textContent).toBe('Two paths.')
    expect(screen.getByTestId('grill-card')).toBeInTheDocument()
  })

  it('starts each reply version with its own proposal', () => {
    const other = { ...GRILL, q: 'Who reads the facts?' }
    const v = (g: GrillProposal) => ({ content: `Two paths.\n[GRILL: ${JSON.stringify(g)}]` })
    const { rerender } = render(<AssistantMessage content={v(GRILL).content} variants={[v(GRILL), v(other)]} variantIdx={0} onSwitchVariant={() => {}} isStreaming={false} slotRunning={false} messageTs="t5" />)
    expect(screen.getByLabelText('Question')).toHaveValue('Who writes the facts?')
    rerender(<AssistantMessage content={v(other).content} variants={[v(GRILL), v(other)]} variantIdx={1} onSwitchVariant={() => {}} isStreaming={false} slotRunning={false} messageTs="t5" />)
    expect(screen.getByLabelText('Question')).toHaveValue('Who reads the facts?')
  })

  it('keeps edits when a refresh gives the row its timestamp', () => {
    const { rerender } = render(<AssistantMessage content={content} isStreaming={false} slotRunning={false} slotKey="chat-b" grillRowId="client-1" />)
    fireEvent.change(screen.getByLabelText('Question'), { target: { value: 'Kept?' } })
    rerender(<AssistantMessage content={content} isStreaming={false} slotRunning={false} slotKey="chat-b" messageTs="2026-11-15T00:00:00Z" grillRowId="client-1" />)
    expect(screen.getByLabelText('Question')).toHaveValue('Kept?')
  })

  const version = (g: GrillProposal, mid?: string) => ({ content: `Two paths.\n[GRILL: ${JSON.stringify(g)}]`, mid })
  const regenerated = [version(GRILL, 'm-old'), version({ ...GRILL, q: 'Other?' }, 'm-new')]

  it('keys each version of a regenerated reply on the mid of the row it was written as', () => {
    const { unmount } = render(<AssistantMessage content={regenerated[1].content} variants={regenerated} variantIdx={1} onSwitchVariant={() => {}} isStreaming={false} slotRunning={false} slotKey="chat-b" grillRowId="r1" grillMid="m-new" />)
    fireEvent.click(screen.getByRole('button', { name: 'No thanks' }))
    unmount()
    render(<AssistantMessage content={regenerated[0].content} variants={regenerated} variantIdx={0} onSwitchVariant={() => {}} isStreaming={false} slotRunning={false} slotKey="chat-b" grillRowId="r1" grillMid="m-new" />)
    fireEvent.click(screen.getByRole('button', { name: 'No thanks' }))
    expect(sent).toEqual(['No discussion needed\nkey: m-new:0', 'No discussion needed\nkey: m-old:0'])
  })

  it('locks only the regenerated version whose key a later stored message sent', () => {
    const { rerender } = render(<AssistantMessage content={regenerated[0].content} variants={regenerated} variantIdx={0} onSwitchVariant={() => {}} isStreaming={false} slotRunning={false} slotKey="chat-b" grillRowId="r3" grillMid="m-new" grillSent="m-old:0" />)
    expect(screen.getByTestId('grill-card-sent').textContent).toBe('Sent.')
    rerender(<AssistantMessage content={regenerated[1].content} variants={regenerated} variantIdx={1} onSwitchVariant={() => {}} isStreaming={false} slotRunning={false} slotKey="chat-b" grillRowId="r3" grillMid="m-new" grillSent="m-old:0" />)
    expect(screen.queryByTestId('grill-card-sent')).toBeNull()
  })

  it('sends no key from a version stored without a mid, and a key does not lock it', () => {
    const plain = [version(GRILL), version({ ...GRILL, q: 'Other?' })]
    render(<AssistantMessage content={plain[1].content} variants={plain} variantIdx={1} onSwitchVariant={() => {}} isStreaming={false} slotRunning={false} slotKey="chat-b" grillRowId="r5" grillMid="m-1" grillSent={'m-1:0\nm-1:1'} />)
    fireEvent.click(screen.getByRole('button', { name: 'No thanks' }))
    expect(sent).toEqual(['No discussion needed'])
  })

  it('keys a reply with one version on its row mid', () => {
    render(<AssistantMessage content={content} isStreaming={false} slotRunning={false} slotKey="chat-b" grillRowId="r4" grillMid="m-4" />)
    fireEvent.click(screen.getByRole('button', { name: 'No thanks' }))
    expect(sent).toEqual(['No discussion needed\nkey: m-4:0'])
  })

  it('locks the card whose key a later stored message sent', () => {
    render(<AssistantMessage content={content} isStreaming={false} slotRunning={false} slotKey="chat-b" grillRowId="r2" grillMid="m-2" grillSent={'m-2:3\nm-2:0'} />)
    expect(screen.getByTestId('grill-card-sent').textContent).toBe('Sent.')
  })

  it('draws no card while the reply streams', () => {
    render(<AssistantMessage content={'Two paths.\n[GRILL: {"q":"Wh'} isStreaming slotRunning />)
    expect(screen.getByTestId('md').textContent).toBe('Two paths.')
    expect(screen.queryByTestId('grill-card')).toBeNull()
  })

  it('shows a bad marker as text, with no card', () => {
    const bad = 'Two paths.\n[GRILL: {"q": oops}]'
    render(<AssistantMessage content={bad} isStreaming={false} slotRunning={false} />)
    expect(screen.getByTestId('md').textContent).toBe(bad)
    expect(screen.queryByTestId('grill-card')).toBeNull()
  })
})
