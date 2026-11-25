/**
 * The pinned card stands in for a user row, so it takes that row's side: a
 * prompt someone else sent pins on the start side with the outlined bubble
 * (`user-bubble-other`), the viewer's own on the end side, filled. The sender
 * is the row's `meta.author` stamp, read by the rule the transcript bubble uses
 * (`OtherSenderBubble`; MessageByline.runs.test.tsx pins that rule). That
 * person's card also carries the row's byline above its box, and counts it in
 * every height it hands out.
 *
 * happy-dom resolves no CSS, so the pin is the class contract on the band, the
 * clip wrapper and the box.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
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
  return mountWith({}, ...metas)
}

function mountWith(props: { maxH?: number; liveH?: number; onCollapsedHeight?: (h: number) => void; scrollTranscriptBy?: (dy: number) => void }, ...metas: Array<Record<string, unknown> | undefined>) {
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
            {...props}
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
  const box = card.querySelector('.message-bubble') as HTMLElement
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

describe('the pinned card names a sender who is not the viewer', () => {
  /** The byline's name inside card `index`, or null when the card has none. */
  const named = (index = 0) => {
    const card = screen.getAllByTestId('pinned-prompt')[index]
    return within(card).queryByTestId('message-byline')?.textContent ?? null
  }

  it('shows the transcript byline above the box of a prompt someone else sent', async () => {
    mount({ author: ANA })
    await waitFor(() => expect(named()).toBe('ana@example.com'), { timeout: CHAIN_TIMEOUT_MS })
    const card = screen.getByTestId('pinned-prompt')
    // Above the box, inside the element the push translates, so the name
    // leaves with the card.
    expect(Array.from(card.children).map(c => (c as HTMLElement).dataset.testid ?? 'box')).toEqual(['pinned-prompt-byline', 'box'])
    // The box under a wider name keeps hugging its own text.
    expect(card.querySelector('.message-bubble')!.classList.contains('w-fit')).toBe(true)
  })

  it('scrolls the transcript on a wheel over the name, as over the box', async () => {
    const scrollTranscriptBy = vi.fn()
    mountWith({ scrollTranscriptBy }, { author: ANA })
    await waitFor(() => expect(named()).toBe('ana@example.com'), { timeout: CHAIN_TIMEOUT_MS })
    const byline = screen.getByTestId('pinned-prompt-byline')
    const wheel = new WheelEvent('wheel', { deltaY: 40, cancelable: true })
    byline.dispatchEvent(wheel)
    expect(scrollTranscriptBy).toHaveBeenCalledTimes(1)
    expect(scrollTranscriptBy).toHaveBeenCalledWith(40)
    expect(wheel.defaultPrevented).toBe(true)
  })

  it('names nobody on the viewer\'s own prompt, or on one with no author stamp', async () => {
    mount({ author: OWNER }, { mid: 'm1' }, { author: ANA })
    await waitFor(() => expect(named(2)).toBe('ana@example.com'), { timeout: CHAIN_TIMEOUT_MS })
    for (const index of [0, 1]) {
      expect(named(index)).toBeNull()
      const card = screen.getAllByTestId('pinned-prompt')[index]
      expect(card.querySelector('[data-testid="pinned-prompt-byline"]')).toBeNull()
      expect(card.querySelector('.message-bubble')!.classList.contains('w-fit')).toBe(false)
    }
  })

  it('names the app on a prompt an app sent into someone else\'s chat', async () => {
    const app = { id: 'sys_app', kind: 'system' as const, display: { name: 'app:notes', login: '', captured_at: null } }
    mount({ author: app, turnActor: 'app' })
    await waitFor(() => expect(within(screen.getByTestId('pinned-prompt')).queryByTestId('message-app-sender')).not.toBeNull(), { timeout: CHAIN_TIMEOUT_MS })
  })

  describe('with measured heights', () => {
    // happy-dom lays nothing out, so the two heights the card adds up are
    // given: a 22px byline over a 37px box.
    const BYLINE_H = 22
    const BOX_H = 37
    let original: typeof HTMLElement.prototype.getBoundingClientRect
    beforeEach(() => {
      original = HTMLElement.prototype.getBoundingClientRect
      HTMLElement.prototype.getBoundingClientRect = function (this: HTMLElement) {
        const height = this.dataset.testid === 'pinned-prompt-byline' ? BYLINE_H : this.classList.contains('message-bubble') ? BOX_H : 0
        return { top: 0, bottom: height, left: 0, right: 0, width: 0, height, x: 0, y: 0, toJSON: () => ({}) } as DOMRect
      }
    })
    afterEach(() => { HTMLElement.prototype.getBoundingClientRect = original })

    it('reports the byline inside the resting height, so the host\'s push and gate cover it', async () => {
      const onCollapsedHeight = vi.fn()
      mountWith({ onCollapsedHeight }, { author: ANA })
      await waitFor(() => expect(named()).toBe('ana@example.com'), { timeout: CHAIN_TIMEOUT_MS })
      await waitFor(() => expect(onCollapsedHeight).toHaveBeenLastCalledWith(BYLINE_H + BOX_H))
    })

    it('reports the box alone for the viewer\'s own prompt', () => {
      const onCollapsedHeight = vi.fn()
      mountWith({ onCollapsedHeight }, { mid: 'm1' })
      expect(onCollapsedHeight).toHaveBeenLastCalledWith(BOX_H)
    })

    it('takes the byline off a fold height too, so the card ends on the bubble it tracks', async () => {
      mountWith({ liveH: 120 }, { author: ANA }, { mid: 'm1' })
      await waitFor(() => expect(named(0)).toBe('ana@example.com'), { timeout: CHAIN_TIMEOUT_MS })
      const box = (index: number) => screen.getAllByTestId('pinned-prompt')[index].querySelector('.message-bubble') as HTMLElement
      await waitFor(() => expect(box(0).style.height).toBe(`${120 - BYLINE_H}px`))
      expect(box(1).style.height).toBe('120px')
    })

    it('takes the byline off the room the ceiling leaves the box', async () => {
      mountWith({ maxH: 200 }, { author: ANA }, { mid: 'm1' })
      await waitFor(() => expect(named(0)).toBe('ana@example.com'), { timeout: CHAIN_TIMEOUT_MS })
      const box = (index: number) => screen.getAllByTestId('pinned-prompt')[index].querySelector('.message-bubble') as HTMLElement
      await waitFor(() => expect(box(0).style.maxHeight).toBe(`${200 - BYLINE_H}px`))
      expect(box(1).style.maxHeight).toBe('200px')
    })
  })
})
