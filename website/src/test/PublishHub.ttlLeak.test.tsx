import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { PublishHub, effectiveTtlHours, ttlSelectableFor, TTL_72 } from '../components/PublishHub'
import type { Artifact } from '../types'

/**
 * `ttlHours` is ONE piece of state shared by every row. A row that cannot expire hides
 * the TTL control, which left the previous choice standing underneath it -- so picking
 * "72 hours" on a row that supports expiry and then switching to a core row that does
 * not left the acknowledgment modal promising a time-boxed exposure for a link that is
 * permanent. The promise is the harm: the user reads it in the last dialog before the
 * content goes world-readable.
 *
 * The derivation is asserted DIRECTLY rather than by driving the widget. `SimpleSelect`
 * renders a Radix trigger (not a native `<select>`) off the touch path, so a
 * `fireEvent.change` against it is a no-op -- a render-level test of this leak passes
 * whether the bug is present or not, which makes it worse than no test. The render test
 * below is therefore scoped to the one thing it can actually observe: the control's
 * absence.
 */

const coreRow = (supportsExpiration: boolean) =>
  ({
    id: 'default',
    label: 'Public web (shared drive)',
    configured: true,
    core: {
      name: 'default',
      display_name: 'Public web (shared drive)',
      sharing_model: { supports_expiration: supportsExpiration },
    },
  }) as unknown as Parameters<typeof effectiveTtlHours>[1]

const appRow = () =>
  ({
    id: 'timed',
    label: 'Timed destination',
    configured: true,
    app: { id: 'timed', label: 'Timed destination' },
  }) as unknown as Parameters<typeof effectiveTtlHours>[1]

describe('effectiveTtlHours — a TTL chosen elsewhere cannot survive onto a row that cannot expire', () => {
  it('drops a 72-hour choice on a core row with no expiration support', () => {
    expect(effectiveTtlHours(TTL_72, coreRow(false))).toBe(0)
  })

  it('keeps a 72-hour choice on a row that does support expiration', () => {
    expect(effectiveTtlHours(TTL_72, appRow())).toBe(72)
    expect(effectiveTtlHours(TTL_72, coreRow(true))).toBe(72)
  })

  it('treats persistent as persistent everywhere', () => {
    expect(effectiveTtlHours('Persistent (no expiry)', appRow())).toBe(0)
    expect(effectiveTtlHours('Persistent (no expiry)', coreRow(false))).toBe(0)
  })

  it('offers the control exactly where a TTL can apply', () => {
    // The control's visibility and the value must read the SAME predicate, which is
    // what stops a hidden control from leaving a live choice behind it.
    expect(ttlSelectableFor(coreRow(false))).toBe(false)
    expect(ttlSelectableFor(coreRow(true))).toBe(true)
    expect(ttlSelectableFor(appRow())).toBe(true)
    expect(ttlSelectableFor(undefined)).toBe(true)
  })
})

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  )
}

const fakeArtifact: Artifact = {
  slug: 'test-doc',
  name: 'Test Doc',
  kind: 'markdown',
  description: '',
  content: '',
  version: 1,
  created_at: '',
  updated_at: '',
  tags: [],
}

const coreDescriptor = (supportsExpiration: boolean) => ({
  name: 'default',
  display_name: 'Public web (shared drive)',
  capabilities: ['sharing'],
  kind_support: 'native',
  capable: true,
  available: true,
  sharing_model: {
    supports_private: true,
    supports_shared: false,
    supports_public: true,
    principal_kind: 'none',
    supports_roles: false,
    supports_expiration: supportsExpiration,
    programmable: false,
  },
  sync_model: { authority: 'local', concurrency: 'token', collab_mode: 'mirror' },
  discovery_model: {
    list_mine: false,
    list_shared_with_me: false,
    list_public: false,
    full_text_search: false,
    pull_by_id: false,
  },
})

describe('PublishHub — the TTL control is absent where no TTL can apply', () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    fetchSpy = vi.spyOn(globalThis, 'fetch')
  })

  function mockCore(supportsExpiration: boolean) {
    fetchSpy.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes('/api/artifacts/publish-providers')) {
        return new Response(
          JSON.stringify({ providers: [coreDescriptor(supportsExpiration)], kind: 'markdown' }),
          { status: 200 },
        )
      }
      if (url.includes('/api/publish-providers')) {
        return new Response(JSON.stringify({ providers: [] }), { status: 200 })
      }
      return new Response(JSON.stringify({ publication: { view_url: 'https://drive/x' } }), {
        status: 200,
      })
    })
  }

  it('hides it for a core row that cannot expire', async () => {
    mockCore(false)
    render(<PublishHub artifact={fakeArtifact} />, { wrapper })
    fireEvent.click(await screen.findByText('Public web (shared drive)'))
    await waitFor(() => {
      expect(screen.queryByLabelText(/time to live/i)).toBeNull()
    })
  })

  it('shows it for a core row that can', async () => {
    mockCore(true)
    render(<PublishHub artifact={fakeArtifact} />, { wrapper })
    fireEvent.click(await screen.findByText('Public web (shared drive)'))
    expect(await screen.findByLabelText(/time to live/i)).toBeTruthy()
  })
})
