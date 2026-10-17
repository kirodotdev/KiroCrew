/**
 * What a pack-wearing roster asks the gateway for.
 *
 * An avatar must know a slot's FORMAT before it can pick a player, and the
 * per-slot route cannot say it before the request. The meta read
 * (`GET /api/appearances/{id}?meta=1`) answers that without any file's bytes, and
 * the svg and sprite tiers draw from the per-slot route, so a roster wearing a
 * sprite pack never downloads the whole pack. A Lottie document is parsed in the
 * page, so a pack with a Lottie slot reads the whole pack once as well.
 *
 * The real client runs here over a stubbed `fetch`, because the request URLs are
 * what is under test. The players are stubbed: they report what they were handed.
 */
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import PackAvatar from '../components/appearancePacks/PackAvatar'
import { namedCeiling } from './namedCeiling'

vi.mock('../components/appearancePacks/SpriteRenderer', () => ({
  SpriteRenderer: ({ src, row }: { src: string; row?: number }) => (
    <i data-testid="sprite-stub" data-src={src} data-row={String(row)} />
  ),
}))

vi.mock('../components/appearancePacks/LottieRenderer', () => ({
  LottieRenderer: ({ animationData }: { animationData: string }) => (
    <i data-testid="lottie-stub" data-bytes={String(animationData.length)} />
  ),
}))

/** The gateway: one answer per URL, and a 404 for any URL it was not given. */
function gateway(answers: Record<string, unknown>) {
  return vi.fn(async (url: string) =>
    url in answers
      ? new Response(JSON.stringify(answers[url]), { status: 200, headers: { 'Content-Type': 'application/json' } })
      : new Response(JSON.stringify({ code: 'pack_not_found' }), { status: 404 }),
  )
}

let qc: QueryClient
const urls = (fetchMock: ReturnType<typeof gateway>) => fetchMock.mock.calls.map(([url]) => url)

beforeEach(() => {
  qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

describe('what a roster wearing a pack requests', () => {
  it('reads a sprite pack once, as meta, and draws every face from the slot route', async () => {
    const sprite = { frameWidth: 16, frameHeight: 16, fps: 4, rowAssignments: { idle: 0, working: 1 } }
    const fetchMock = gateway({
      '/api/appearances/aurora?meta=1': {
        animations: { idle: { format: 'sprite' }, working: { format: 'sprite' } },
        sprite,
        sounds: {},
      },
      // What the route inlines without `?meta=1`: the sheet itself, as base64.
      '/api/appearances/aurora': {
        animations: { idle: { content: 'iVBORw0KGgo=', format: 'sprite' }, working: { content: 'iVBORw0KGgo=', format: 'sprite' } },
        sprite,
        sounds: {},
      },
    })
    vi.stubGlobal('fetch', fetchMock)

    render(
      <QueryClientProvider client={qc}>
        <PackAvatar id="aurora" state="idle" size={38} />
        <PackAvatar id="aurora" state="working" size={38} />
        <PackAvatar id="aurora" state="done" size={38} />
      </QueryClientProvider>,
    )

    await waitFor(() => expect(screen.getAllByTestId('sprite-stub')).toHaveLength(3))
    expect(urls(fetchMock)).toEqual(['/api/appearances/aurora?meta=1'])
    // `done` falls back to idle: the pack draws no done.
    expect(screen.getAllByTestId('sprite-stub').map((s) => [s.getAttribute('data-src'), s.getAttribute('data-row')]))
      .toEqual([
        ['/api/appearances/aurora/slot/idle', '0'],
        ['/api/appearances/aurora/slot/working', '1'],
        ['/api/appearances/aurora/slot/idle', '0'],
      ])
  })

  it('reads the whole pack once more for a Lottie slot, whose document the page parses', async () => {
    const fetchMock = gateway({
      '/api/appearances/aurora?meta=1': { animations: { idle: { format: 'lottie' } }, sounds: {} },
      '/api/appearances/aurora': { animations: { idle: { content: '{"v":"5.7.4"}', format: 'lottie' } }, sounds: {} },
    })
    vi.stubGlobal('fetch', fetchMock)

    render(
      <QueryClientProvider client={qc}>
        <PackAvatar id="aurora" state="idle" size={38} />
        <PackAvatar id="aurora" state="working" size={38} />
      </QueryClientProvider>,
    )

    // A chained query: the whole-pack read starts only once the meta read says the
    // pack has a Lottie slot, and the clip draws from that second answer.
    await waitFor(
      () => expect(screen.getAllByTestId('lottie-stub')).toHaveLength(2),
      namedCeiling('META_THEN_WHOLE_READ', 5000),
    )
    for (const stub of screen.getAllByTestId('lottie-stub')) {
      expect(stub.getAttribute('data-bytes')).toBe('13')
    }
    expect(urls(fetchMock)).toEqual(['/api/appearances/aurora?meta=1', '/api/appearances/aurora'])
  })
})
