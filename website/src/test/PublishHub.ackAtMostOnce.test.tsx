// A confirmed public publish must happen AT MOST ONCE per acknowledgment (#3599).
//
// The acknowledgment renders inside `Modal`'s <AnimatePresence>. When it closes,
// framer-motion keeps rendering the exiting subtree from the element it captured
// BEFORE `busy` flipped, so the `danger` confirm button stays enabled and
// hit-testable for the exit duration. A second click there used to issue a second
// confirmed deploy of the same slug — two public deployments from one human
// acknowledgment, which is precisely what this gate exists to prevent.
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { PublishHub } from '../components/PublishHub'
import type { Artifact } from '../types'

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return (
    <QueryClientProvider client={qc}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  )
}

const fakeArtifact: Artifact = {
  slug: 'test-app',
  name: 'Test App',
  kind: 'widget',
  description: '',
  content: '',
  version: 1,
  created_at: '',
  updated_at: '',
  tags: [],
}

const ACK = 'I understand, publish publicly'

/** The panel header is also the word "Publish", so match the BUTTON. */
function clickPublish() {
  const btn = screen.getAllByRole('button')
    .find(b => b.textContent?.includes('Publish') && !b.textContent?.includes('Close'))
  fireEvent.click(btn!)
}

describe('PublishHub acknowledgment is at-most-once', () => {
  let fetchSpy: ReturnType<typeof vi.spyOn>

  beforeEach(() => {
    fetchSpy = vi.spyOn(globalThis, 'fetch')
  })

  // These tests install a PERSISTENT mockImplementation (the held response), so
  // it must be torn down or it answers the next test's provider fetch.
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('issues ONE confirmed publish when the acknowledgment is double-clicked', async () => {
    fetchSpy.mockImplementationOnce(async () =>
      new Response(JSON.stringify({
        providers: [{
          id: 'deploy-web',
          label: 'Public Web',
          icon: 'Globe',
          kinds: [],
          configured: true,
          setupRoute: '/deploy',
          endpoint: '/api/deploy/deploy',
        }],
      }), { status: 200, headers: { 'Content-Type': 'application/json' } })
    )

    render(<PublishHub artifact={fakeArtifact} />, { wrapper })
    await waitFor(() => { expect(screen.getByText('Public Web')).toBeDefined() })
    fireEvent.click(screen.getByText('Public Web'))

    // Preview (requires_confirm) — the step before the acknowledgment.
    fetchSpy.mockImplementationOnce(async () =>
      new Response(JSON.stringify({ requires_confirm: true, message: 'Ready to publish', bytes: 1024 }),
        { status: 200, headers: { 'Content-Type': 'application/json' } })
    )
    clickPublish()
    await waitFor(() => { expect(screen.getByText(/Ready to publish/)).toBeDefined() })

    // Count only CONFIRMED publishes, and hold the response open so both clicks
    // land while the request is in flight — the real double-click window.
    let confirmCalls = 0
    let release: (r: Response) => void = () => {}
    const held = new Promise<Response>(resolve => { release = resolve })
    fetchSpy.mockImplementation(async (_url, init) => {
      const body = JSON.parse((init as RequestInit).body as string)
      if (body.confirm === true) {
        confirmCalls += 1
        return held
      }
      return new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } })
    })

    fireEvent.click(screen.getByText('Confirm & Publish'))
    const ackBtn = await screen.findByRole('button', { name: ACK })

    // Two clicks, back to back, before the publish settles.
    fireEvent.click(ackBtn)
    fireEvent.click(ackBtn)

    await waitFor(() => { expect(confirmCalls).toBeGreaterThan(0) })
    expect(confirmCalls).toBe(1)

    release(new Response(JSON.stringify({ url: 'https://example.com/app' }),
      { status: 200, headers: { 'Content-Type': 'application/json' } }))

    await waitFor(() => { expect(screen.getByText('Published!')).toBeDefined() })
    // Still exactly one after settling — the latch released without replaying.
    expect(confirmCalls).toBe(1)
  })

  it('holds the acknowledgment open and disabled while the publish is in flight', async () => {
    fetchSpy.mockImplementationOnce(async () =>
      new Response(JSON.stringify({
        providers: [{
          id: 'deploy-web',
          label: 'Public Web',
          icon: 'Globe',
          kinds: [],
          configured: true,
          setupRoute: '/deploy',
          endpoint: '/api/deploy/deploy',
        }],
      }), { status: 200, headers: { 'Content-Type': 'application/json' } })
    )

    render(<PublishHub artifact={fakeArtifact} />, { wrapper })
    await waitFor(() => { expect(screen.getByText('Public Web')).toBeDefined() })
    fireEvent.click(screen.getByText('Public Web'))

    fetchSpy.mockImplementationOnce(async () =>
      new Response(JSON.stringify({ requires_confirm: true, message: 'Ready to publish', bytes: 1024 }),
        { status: 200, headers: { 'Content-Type': 'application/json' } })
    )
    clickPublish()
    await waitFor(() => { expect(screen.getByText(/Ready to publish/)).toBeDefined() })

    let release: (r: Response) => void = () => {}
    const held = new Promise<Response>(resolve => { release = resolve })
    fetchSpy.mockImplementation(async (_url, init) => {
      const body = JSON.parse((init as RequestInit).body as string)
      if (body.confirm === true) return held
      return new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } })
    })

    fireEvent.click(screen.getByText('Confirm & Publish'))
    fireEvent.click(await screen.findByRole('button', { name: ACK }))

    // Mid-flight the acknowledgment is still mounted, relabelled, and disabled —
    // the state that makes the exiting-subtree race unreachable in the first
    // place. Both the panel's confirm and the modal's confirm show it, and BOTH
    // must be disabled: either one left live is a second publish path.
    const busyBtns = await screen.findAllByRole('button', { name: 'Publishing…' })
    expect(busyBtns.length).toBeGreaterThan(0)
    for (const b of busyBtns) expect((b as HTMLButtonElement).disabled).toBe(true)
    // The acknowledgment has NOT closed — its body text is still on screen.
    expect(screen.getByText(/Anyone in the world with the link/i)).toBeDefined()

    release(new Response(JSON.stringify({ url: 'https://example.com/app' }),
      { status: 200, headers: { 'Content-Type': 'application/json' } }))
    await waitFor(() => { expect(screen.getByText('Published!')).toBeDefined() })
  })
})
