/**
 * A shared chat draws another person's message apart from the viewer's own:
 * an outlined bubble (`user-bubble-other`) under a byline, and the byline once
 * per run of that person's messages. The author is the server's `meta.author`
 * stamp, which a teammate on the Crew's own network and a relay teammate both
 * carry, so every case here goes through the one registry `user` entry the
 * transcript surfaces share.
 *
 * happy-dom resolves no CSS, so the pin is the class contract; the fill the
 * theme tints would paint is cleared by the `user-bubble-other` rule, pinned
 * against index.css at the bottom.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { api } from '../../../api/client'
import { ALL_UNAVAILABLE } from '../../../api/sharedCrewUi'
import type { CrewModeResponse } from '../../../api/sharedCrewMode'
import { mergeRenderers, resolveRenderer, type MessageRenderContext } from '../../../app-sdk/messageRenderers'
import type { ChatMessage } from '../../../types'
import { readIndexCss } from '../../../test/indexCssSource'

vi.mock('../../../api/client', () => ({ api: { sharedCrewViewer: vi.fn(), sharedCrewCapabilities: vi.fn(), sharedCrewMode: vi.fn(), sharedCrewPeople: vi.fn() } }))
const viewer = vi.mocked(api.sharedCrewViewer)

const person = (id: string, name: string) => ({ id, kind: 'human' as const, display: { name, login: '', captured_at: null } })
const OWNER = person('p_own', 'owner@example.com')
const ANA = person('p_ana', 'ana@example.com')
const BO = person('p_bo', 'bo@example.com')

const said = (who: ReturnType<typeof person>, content: string): ChatMessage => ({ role: 'user', content, ts: content, meta: { author: who } } as unknown as ChatMessage)
const reply = (content: string): ChatMessage => ({ role: 'assistant', content, ts: content } as unknown as ChatMessage)

const ctxFor = (messages: ChatMessage[], index: number): MessageRenderContext => ({
  index, messages, running: false, key: `k${index}`, hideCardOwnedOAuth: false, autoDeniedIds: new Set<string>(),
  wrapper: children => children, row: children => children,
})

/** The transcript as the shared registry draws it; each user row is tagged
 *  with its content so a case reads byline and bubble per message. */
function mount(messages: ChatMessage[]) {
  const registry = mergeRenderers([])
  const slots = [{ key: 's1', attribution: { creator: OWNER, availability: 'recorded', created_at: 1 } }]
  const state = { dashboard: { slots } }
  const store = { getState: () => state, subscribe: () => () => {}, dispatch: (a: unknown) => a }
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    <Provider store={store as any}>
      <QueryClientProvider client={qc}>
        {messages.map((m, i) => m.role === 'user'
          ? <div key={i} data-row={m.content}>{resolveRenderer(m, registry)!.render(m, ctxFor(messages, i))}</div>
          : <div key={i} data-row={m.content}>{m.content}</div>)}
      </QueryClientProvider>
    </Provider>,
  )
}

/** Per user message: its byline (or '') and whether its bubble is outlined. */
function read(container: HTMLElement, messages: ChatMessage[]) {
  return messages.filter(m => m.role === 'user').map(m => {
    const row = container.querySelector(`[data-row="${m.content}"]`)!
    const byline = row.querySelector('[data-testid="message-byline"]')?.textContent ?? ''
    const bubble = row.querySelector('.message-bubble.msg-content')!
    return [m.content, byline, bubble.classList.contains('user-bubble-other') ? 'outlined' : 'filled']
  })
}

/** The byline reads capabilities first; only then are the mode and People
 *  queries enabled. Waits that span that chain get this ceiling. */
const CHAIN_TIMEOUT_MS = 5000

/** Rendered once any teammate's byline shows: the mode and viewer are known,
 *  so every absent byline and filled bubble after it is a decision. Both waits
 *  span the capabilities -> mode/people chain, so both carry its ceiling. */
async function settled(container: HTMLElement, messages: ChatMessage[]) {
  await screen.findAllByTestId('message-byline', {}, { timeout: CHAIN_TIMEOUT_MS })
  await waitFor(() => expect(container.querySelectorAll('.user-bubble-other').length).toBeGreaterThan(0), { timeout: CHAIN_TIMEOUT_MS })
  return read(container, messages)
}

beforeEach(() => {
  vi.clearAllMocks()
  viewer.mockResolvedValue(OWNER)
  vi.mocked(api.sharedCrewCapabilities).mockResolvedValue({ ...ALL_UNAVAILABLE, attribution: 'available', shared_crew_preview: true })
  vi.mocked(api.sharedCrewPeople).mockResolvedValue({ schema_version: 1, directory: { state: 'unavailable' }, people: [{ principal: ANA, sources: ['observed' as const], is_viewer: false }] })
  vi.mocked(api.sharedCrewMode).mockResolvedValue({
    schema_version: 1, mode: 'network', crew_id: 'crew-a', viewer: OWNER,
    team_config: { state: 'not_configured', reason: '', capability: '', cause: '' },
    console: { state: 'not_offered', url: '' }, adapters: {}, workload_identity_ref: 'unavailable',
  } satisfies CrewModeResponse)
})

describe('a shared chat draws other people apart from the viewer', () => {
  it('own messages stay filled and unlabelled; a teammate is outlined and named once per run', async () => {
    const msgs = [said(OWNER, 'mine'), said(ANA, 'a1'), said(ANA, 'a2')]
    const { container } = mount(msgs)
    expect(await settled(container, msgs)).toEqual([
      ['mine', '', 'filled'],
      ['a1', 'ana@example.com', 'outlined'],
      ['a2', '', 'outlined'],
    ])
  })

  it('two teammates alternating each start a run of their own', async () => {
    const msgs = [said(ANA, 'a1'), said(BO, 'b1'), said(ANA, 'a2'), said(BO, 'b2'), said(BO, 'b3')]
    const { container } = mount(msgs)
    expect(await settled(container, msgs)).toEqual([
      ['a1', 'ana@example.com', 'outlined'],
      ['b1', 'bo@example.com', 'outlined'],
      ['a2', 'ana@example.com', 'outlined'],
      ['b2', 'bo@example.com', 'outlined'],
      ['b3', '', 'outlined'],
    ])
  })

  it('an agent reply between two of the same teammate\'s messages starts a new run', async () => {
    const msgs = [said(ANA, 'a1'), reply('agent'), said(ANA, 'a2')]
    const { container } = mount(msgs)
    expect(await settled(container, msgs)).toEqual([
      ['a1', 'ana@example.com', 'outlined'],
      ['a2', 'ana@example.com', 'outlined'],
    ])
  })

  it('a teammate viewing the owner\'s session sees the owner outlined and named, their own filled', async () => {
    viewer.mockResolvedValue(ANA)
    const msgs = [said(OWNER, 'o1'), said(OWNER, 'o2'), said(ANA, 'mine')]
    const { container } = mount(msgs)
    expect(await settled(container, msgs)).toEqual([
      ['o1', 'owner@example.com', 'outlined'],
      ['o2', '', 'outlined'],
      ['mine', '', 'filled'],
    ])
  })
})

describe('the index.css hook', () => {
  it('clears the theme tint on an outlined bubble', () => {
    // kiro-light and the OLED / colour-blind light themes tint `.user-bubble` at (0,2,0).
    expect(readIndexCss()).toMatch(/\.message-bubble\.user-bubble\.user-bubble-other\{background-color:transparent\}/)
  })
})
