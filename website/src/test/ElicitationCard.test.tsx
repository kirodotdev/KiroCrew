import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { vi } from 'vitest'
import { configureStore } from '@reduxjs/toolkit'
import { Provider } from 'react-redux'
import chatReducer, { replaceMessages, setActiveSlot } from '../store/chatSlice'
import ElicitationCard, {
  answerSummary,
  clipCodePoints,
  askerOf,
  draftToContent,
  initialDraft,
  linkHost,
  type ElicitationField,
} from '../pages/chat/ElicitationCard'
import { defaultMessageRenderers, type MessageRenderContext } from '../app-sdk/messageRenderers'
import type { ChatMessage } from '../types'

const post = vi.fn()
vi.mock(import('../api/apiTransport'), async (importOriginal) => {
  const actual = await importOriginal()
  const { ApiError } = await import('../api/apiError')
  // The real transport's contract: `post` resolves the raw Response, `j` turns
  // a non-2xx into an ApiError whose message is the gateway's `error` field.
  const j = async (r: Response) => {
    if (!r.ok) {
      const body = await r.text()
      let message = body
      try { message = (JSON.parse(body) as { error?: string }).error ?? body } catch { /* plain text */ }
      throw new ApiError(r.status, message, body)
    }
    return r.json()
  }
  return {
    ...actual,
    apiTransport: { ...actual.apiTransport, post: (...args: unknown[]) => post(...args), j },
  }
})

const FIELDS: ElicitationField[] = [
  { name: 'confirm', kind: 'boolean', required: true, title: 'Confirm', description: 'Delete it' },
  { name: 'count', kind: 'integer', required: false, minimum: 1, maximum: 5, default: 2 },
  { name: 'reason', kind: 'string', required: false },
  { name: 'tags', kind: 'multi_enum', required: false, options: [{ value: 'a', label: 'A' }, { value: 'b', label: 'B' }] },
]

function row(meta: Record<string, unknown>): ChatMessage {
  return { role: 'elicitation', content: 'Really delete probe.txt?', cls: JSON.stringify(meta), meta: { approval_id: 'rid-1', mid: 'mid-1', mode: 'form', message: 'Really delete probe.txt?', fields: FIELDS, ...meta } }
}

describe('draft helpers', () => {
  it('seeds the draft from server defaults, false for a boolean and [] for a multi-select', () => {
    expect(initialDraft(FIELDS)).toEqual({ confirm: false, count: 2, tags: [] })
  })

  it('reports the first required field left empty, by its title', () => {
    expect(draftToContent([{ name: 'who', kind: 'string', required: true, title: 'Who' }], {})).toEqual({ missing: 'Who' })
  })

  it('holds a multi-select to its minItems/maxItems before any request', () => {
    const f: ElicitationField[] = [{ name: 'tags', kind: 'multi_enum', required: false, title: 'Tags', min_items: 2, max_items: 2, options: [{ value: 'a', label: 'A' }, { value: 'b', label: 'B' }, { value: 'c', label: 'C' }] }]
    expect(draftToContent(f, { tags: ['a'] })).toEqual({ missing: 'Tags' })
    expect(draftToContent(f, { tags: ['a', 'b', 'c'] })).toEqual({ missing: 'Tags' })
    expect(draftToContent(f, { tags: ['a', 'b'] })).toEqual({ content: { tags: ['a', 'b'] } })
  })

  it('leaves an untouched optional multi-select out whatever its minimum, and sends a required empty one', () => {
    // The bounds govern a list that is SENT; an optional field nobody touched
    // is omitted, as the gateway allows. A required field is always sent, so a
    // required `[]` reaches the bounds check: fine under maxItems 0, a miss
    // under minItems 1.
    const optional: ElicitationField[] = [{ name: 'tags', kind: 'multi_enum', required: false, title: 'Tags', min_items: 1, options: [{ value: 'a', label: 'A' }] }]
    expect(draftToContent(optional, { tags: [] })).toEqual({ content: {} })
    const requiredNone: ElicitationField[] = [{ name: 'tags', kind: 'multi_enum', required: true, title: 'Tags', max_items: 0, options: [{ value: 'a', label: 'A' }] }]
    expect(draftToContent(requiredNone, { tags: [] })).toEqual({ content: { tags: [] } })
    const requiredOne: ElicitationField[] = [{ name: 'tags', kind: 'multi_enum', required: true, title: 'Tags', min_items: 1, options: [{ value: 'a', label: 'A' }] }]
    expect(draftToContent(requiredOne, { tags: [] })).toEqual({ missing: 'Tags' })
  })

  it('sends an enum choice whose value is the empty string, and still catches an unset one', () => {
    const f: ElicitationField[] = [{ name: 'tier', kind: 'enum', required: true, title: 'Tier', options: [{ value: '', label: 'None' }, { value: 'a', label: 'A' }] }]
    expect(draftToContent(f, { tier: '' })).toEqual({ content: { tier: '' } })
    expect(draftToContent(f, {})).toEqual({ missing: 'Tier' })
  })

  it('refuses an integer past the safe range instead of sending a rounded one', () => {
    const f: ElicitationField[] = [{ name: 'id', kind: 'integer', required: true, title: 'ID' }]
    expect(draftToContent(f, { id: '9007199254740993' })).toEqual({ unsafe: 'ID' })
    expect(draftToContent(f, { id: '1.5' })).toEqual({ notWhole: 'ID' })
    expect(draftToContent(f, { id: '9007199254740991' })).toEqual({ content: { id: 9007199254740991 } })
    expect(draftToContent(f, { id: 7 })).toEqual({ content: { id: 7 } })
  })

  it('bounds a text answer by code points, so one emoji fits a maxLength of 1', () => {
    // The gateway counts code points (Python len); the native maxlength counts
    // UTF-16 units and would refuse the emoji the schema allows.
    expect(clipCodePoints('😀', 1)).toBe('😀')
    expect(clipCodePoints('😀😀', 1)).toBe('😀')
    expect(clipCodePoints('abc', 2)).toBe('ab')
    expect(clipCodePoints('abc', undefined)).toBe('abc')
  })

  it('keeps a field named __proto__ as an ordinary answer, unset until typed', () => {
    // On a plain object that name reads as the inherited prototype (so an
    // untouched field would look filled) and an assignment to it sets the
    // prototype instead of the answer, which JSON then leaves out.
    const f: ElicitationField[] = [{ name: '__proto__', kind: 'string', required: true, title: 'Proto' }]
    expect(initialDraft(f)).toEqual({})
    expect(draftToContent(f, initialDraft(f))).toEqual({ missing: 'Proto' })
    const shaped = draftToContent(f, Object.assign(initialDraft(f), { ['__proto__']: 'x' }))
    expect(JSON.parse(JSON.stringify(shaped.content))).toEqual({ ['__proto__']: 'x' })
    expect(answerSummary(f, JSON.parse('{"__proto__": "x"}'))).toBe('Proto: x')
    expect(answerSummary(f, {})).toBe('')
  })

  it('shapes numbers from their text and leaves optional empties out', () => {
    expect(draftToContent(FIELDS, { confirm: true, count: '3', reason: '', tags: [] })).toEqual({
      content: { confirm: true, count: 3 },
    })
    expect(draftToContent(FIELDS, { confirm: true, count: 'x' })).toEqual({ missing: 'count' })
  })
})

describe('ElicitationCard', () => {
  beforeEach(() => { post.mockReset(); post.mockResolvedValue(new Response('{"ok":true}')) })

  it('hands an unset enum a value no option carries, so "" stays a real choice', () => {
    const fields: ElicitationField[] = [{ name: 'tier', kind: 'enum', required: true, title: 'Tier', options: [{ value: '', label: 'None' }] }]
    render(<ElicitationCard message={row({ fields })} slot="dashboard:main" />)
    // Nothing picked yet: the trigger shows the fallback, not the "" option.
    expect(screen.getByRole('combobox', { name: 'Tier' })).toHaveTextContent('Choose')
    fireEvent.click(screen.getByRole('button', { name: /submit/i }))
    expect(screen.getByRole('status')).toHaveTextContent('Tier')
    expect(post).not.toHaveBeenCalled()
  })

  it('names the server and tool that are asking, and the host a URL ask sends the person to', () => {
    render(<ElicitationCard message={row({ mode: 'url', url: 'https://example.com/probe/authorize?state=1', fields: undefined, mcp_server: 'probe', tool_name: 'delete_file' })} slot="dashboard:main" />)
    expect(screen.getByText('probe · delete_file is asking you')).toBeInTheDocument()
    expect(screen.getByTestId('elicitation-link-host')).toHaveTextContent('example.com')
    expect(askerOf({ tool_name: 'delete_file' })).toBe('delete_file')
    expect(askerOf({})).toBe('')
    expect(linkHost('not a url')).toBe('')
  })

  it('shows a date-time default as the text that will be sent', () => {
    const fields: ElicitationField[] = [{ name: 'when', kind: 'string', required: false, title: 'When', format: 'date-time', default: '2026-10-23T12:00:00Z' }]
    render(<ElicitationCard message={row({ fields })} slot="dashboard:main" />)
    const input = screen.getByLabelText('When') as HTMLInputElement
    expect(input.type).toBe('text')
    expect(input.value).toBe('2026-10-23T12:00:00Z')
  })

  it('posts a validated form answer to the slot elicitation route', async () => {
    render(<ElicitationCard message={row({})} slot="dashboard:main" />)
    expect(screen.getByText('Really delete probe.txt?')).toBeInTheDocument()
    fireEvent.click(screen.getByLabelText('Confirm'))
    fireEvent.click(screen.getByRole('button', { name: /submit/i }))
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1))
    expect(post.mock.calls[0][0]).toBe('/api/chat/slots/dashboard%3Amain/elicitation')
    expect(post.mock.calls[0][1]).toEqual({
      request_id: 'rid-1',
      request_mid: 'mid-1',
      action: 'accept',
      content: { confirm: true, count: 2 },
    })
  })

  it('stamps its own row once the gateway accepts the answer, without waiting for the frame', async () => {
    // A socket that drops during the post misses `elicitation_resolved`; the
    // accepted answer must still settle the card in the store.
    const store = configureStore({ reducer: { chat: chatReducer }, middleware: d => d({ serializableCheck: false, immutableCheck: false }) })
    store.dispatch(setActiveSlot('dashboard:main'))
    const card = row({})
    store.dispatch(replaceMessages([card]))
    render(<Provider store={store}><ElicitationCard message={card} slot="dashboard:main" /></Provider>)
    fireEvent.click(screen.getByLabelText('Confirm'))
    fireEvent.click(screen.getByRole('button', { name: /submit/i }))
    await waitFor(() => expect(store.getState().chat.messages[0].meta?.resolved).toBe('accept'))
    expect(store.getState().chat.messages[0].meta?.answer).toEqual({ confirm: true, count: 2 })
  })

  it('refuses to submit with a required field empty and says which, without a request', () => {
    // An unchecked required boolean is a valid `false`; a required STRING left
    // blank is the miss the card must catch before any round trip.
    const fields: ElicitationField[] = [{ name: 'who', kind: 'string', required: true, title: 'Who' }]
    render(<ElicitationCard message={row({ fields })} slot="dashboard:main" />)
    fireEvent.click(screen.getByRole('button', { name: /submit/i }))
    // A validation hint is status text, not an error: no ErrorNotice, no alert.
    expect(screen.getByRole('status')).toHaveTextContent('Who')
    expect(screen.queryByRole('alert')).toBeNull()
    expect(post).not.toHaveBeenCalled()
  })

  it('declines with no content', async () => {
    render(<ElicitationCard message={row({})} slot="dashboard:main" />)
    fireEvent.click(screen.getByRole('button', { name: /decline/i }))
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1))
    expect(post.mock.calls[0][1]).toEqual({ request_id: 'rid-1', request_mid: 'mid-1', action: 'decline' })
  })

  it('shows the gateway reason through ErrorNotice when the answer is refused and keeps the form open', async () => {
    const body = JSON.stringify({ error: "'count' is above its maximum", code: 'invalid_content' })
    post.mockResolvedValueOnce(new Response(body, { status: 400, headers: { 'content-type': 'application/json' } }))
    render(<ElicitationCard message={row({})} slot="dashboard:main" />)
    fireEvent.click(screen.getByLabelText('Confirm'))
    fireEvent.click(screen.getByRole('button', { name: /submit/i }))
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('above its maximum'))
    // The shared surface, not a hand-written red div.
    expect(screen.getByTestId('elicitation-answer-error')).toHaveAttribute('role', 'alert')
    expect(screen.getByRole('button', { name: /submit/i })).toBeInTheDocument()
  })

  it('renders a URL ask as a link plus done/decline, and a resolved row read-only', () => {
    const url = row({ mode: 'url', url: 'https://example.com/confirm', fields: undefined })
    render(<ElicitationCard message={url} slot="dashboard:main" />)
    expect(screen.getByRole('link')).toHaveAttribute('href', 'https://example.com/confirm')
    expect(screen.getByRole('button', { name: /finished/i })).toBeInTheDocument()
    const resolved = row({ resolved: 'decline' })
    const { container } = render(<ElicitationCard message={resolved} slot="dashboard:main" />)
    expect(container.querySelectorAll('button')).toHaveLength(0)
    expect(container.textContent).toMatch(/Declined/)
  })

  it('is claimed by the default renderer registry so an embed draws it instead of a bubble', () => {
    const entry = defaultMessageRenderers.find(r => r.roles.includes('elicitation'))
    expect(entry?.id).toBe('elicitation')
    const ctx = { row: (c: React.ReactNode) => <div data-testid="row">{c}</div>, wrapper: (c: React.ReactNode) => c } as unknown as MessageRenderContext
    render(<>{entry!.render(row({}), ctx)}</>)
    // Slot-less: the ask is drawn but cannot be answered from here.
    expect(screen.getByRole('button', { name: /submit/i })).toBeDisabled()
  })
})
