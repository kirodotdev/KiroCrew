/**
 * The pinned card stands in for a user row, so it takes that row's side: a
 * prompt someone else sent pins on the start side with the outlined bubble
 * (`user-bubble-other`), the viewer's own on the end side, filled. The sender
 * is the row's `meta.author` stamp, read by the rule the transcript bubble uses
 * (`OtherSenderBubble`; MessageByline.runs.test.tsx pins that rule).
 *
 * happy-dom resolves no CSS, so the pin is the class contract on the band, the
 * clip wrapper and the box.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { api } from '../api/client'
import { ALL_UNAVAILABLE } from '../api/sharedCrewUi'
import type { CrewModeResponse } from '../api/sharedCrewMode'
import PinnedPrompt from '../pages/chat/PinnedPrompt'
import { nextPinnedPromptState } from '../utils/pinnedPrompt'

vi.mock('../api/client', () => ({ api: { sharedCrewViewer: vi.fn(), sharedCrewCapabilities: vi.fn(), sharedCrewMode: vi.fn(), sharedCrewPeople: vi.fn() } }))

const person = (id: string, name: string) => ({ id, kind: 'human' as const, display: { name, login: '', captured_at: null } })
const OWNER = person('p_own', 'owner@example.com')
const ANA = person('p_ana', 'ana@example.com')

/** The byline reads capabilities first; only then are the mode and People
 *  queries enabled. A wait that spans that chain gets this ceiling. */
const CHAIN_TIMEOUT_MS = 5000

/** One card per meta, all under one store and one query client. */
function mount(...metas: Array<Record<string, unknown> | undefined>) {
  const slots = [{ key: 's1', attribution: { creator: OWNER, availability: 'recorded', created_at: 1 } }]
  const state = { dashboard: { slots } }
  const store = { getState: () => state, subscribe: () => () => {}, dispatch: (a: unknown) => a }
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    <Provider store={store as any}>
      <QueryClientProvider client={qc}>
        {metas.map((meta, i) => (
          <PinnedPrompt
            key={i} text="a prompt" fullText="a prompt" images={[]} meta={meta} slotKey="s1"
            pushUp={0} bannerH={40} expanded={false} onToggleExpanded={() => {}} onJump={() => {}} cardRef={() => {}}
          />
        ))}
      </QueryClientProvider>
    </Provider>,
  )
}

/** Which side the band and the clip wrapper put the card on, and its bubble. */
function drawn(index = 0) {
  const card = screen.getAllByTestId('pinned-prompt')[index]
  const clip = card.parentElement as HTMLElement
  const band = clip.parentElement as HTMLElement
  const box = card.firstElementChild as HTMLElement
  const side = (el: HTMLElement) => (el.classList.contains('justify-start') ? 'start' : el.classList.contains('justify-end') ? 'end' : 'none')
  return {
    band: side(band),
    clip: side(clip),
    bubble: box.classList.contains('user-bubble-other') ? 'outlined' : box.classList.contains('bg-card') ? 'filled' : 'none',
    ring: box.classList.contains('ring-border-strong') ? 'strong' : box.classList.contains('ring-border') ? 'plain' : 'none',
  }
}

const OWN = { band: 'end', clip: 'end', bubble: 'filled', ring: 'plain' }
const OTHER = { band: 'start', clip: 'start', bubble: 'outlined', ring: 'strong' }

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(api.sharedCrewViewer).mockResolvedValue(OWNER)
  vi.mocked(api.sharedCrewCapabilities).mockResolvedValue({ ...ALL_UNAVAILABLE, attribution: 'available', shared_crew_preview: true })
  vi.mocked(api.sharedCrewPeople).mockResolvedValue({ schema_version: 1, directory: { state: 'unavailable' }, people: [{ principal: ANA, sources: ['observed' as const], is_viewer: false }] })
  vi.mocked(api.sharedCrewMode).mockResolvedValue({
    schema_version: 1, mode: 'network', crew_id: 'crew-a', viewer: OWNER,
    team_config: { state: 'not_configured', reason: '', capability: '', cause: '' },
    console: { state: 'not_offered', url: '' }, adapters: {}, workload_identity_ref: 'unavailable',
  } satisfies CrewModeResponse)
})

describe('the pinned card takes its sender\'s side', () => {
  it('a prompt someone else sent pins on the start side, outlined', async () => {
    mount({ author: ANA })
    await waitFor(() => expect(drawn()).toEqual(OTHER), { timeout: CHAIN_TIMEOUT_MS })
  })

  it('the viewer\'s own prompt stays on the end side, filled, once the viewer is known', async () => {
    // A teammate's card beside it reads the same queries, so the moment it is
    // drawn apart the viewer is known and the own card's side is a decision.
    mount({ author: OWNER }, { author: ANA })
    await waitFor(() => expect(drawn(1)).toEqual(OTHER), { timeout: CHAIN_TIMEOUT_MS })
    expect(drawn(0)).toEqual(OWN)
  })

  it('a prompt with no author stamp stays on the end side and reads no Shared Crew state', () => {
    mount({ mid: 'm1' })
    expect(drawn()).toEqual(OWN)
    expect(api.sharedCrewCapabilities).not.toHaveBeenCalled()
  })

  it('the pinned state carries the row\'s meta, and keeps it while the same prompt is pinned', () => {
    const meta = { author: ANA }
    const input = { idx: 3, ts: 't1', raw: 'a prompt', pastes: [], meta, push: 0, bannerH: 40 }
    const first = nextPinnedPromptState(null, input)
    expect(first.meta).toBe(meta)
    expect(nextPinnedPromptState(first, { ...input, push: 12 }).meta).toBe(meta)
  })
})
