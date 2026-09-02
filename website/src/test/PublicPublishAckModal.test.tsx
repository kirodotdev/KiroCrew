// The blocking public-exposure acknowledgment (issue #3599).
//
// #3493 put the exposure warning next to each confirm button; this suite pins the
// stronger property the issue asked for — no click path reaches a world-readable
// URL without a human pressing a button whose label IS the acknowledgment, and
// that button is neither pre-focused nor the default action.
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { PublishHub } from '../components/PublishHub'
import PublicPublishAckModal from '../components/PublicPublishAckModal'
import ArtifactDeployPage from '../pages/ArtifactDeployPage'
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

const PROVIDERS = {
  providers: [{
    id: 'deploy-web-aws',
    label: 'Public Web',
    icon: 'Globe',
    kinds: [],
    configured: true,
    setupRoute: '/deploy',
    endpoint: '/api/deploy/deploy',
  }],
}

const jsonResp = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })

const ackButton = () =>
  screen.getAllByRole('button').find(b => b.textContent === 'I understand, publish publicly')

const startPublishBtn = () =>
  screen.getAllByRole('button')
    .find(b => b.textContent?.includes('Publish') && !b.textContent?.includes('Close'))!

/** Drive PublishHub to its commit step and return the fetch spy. */
async function openCommitStep(secondResponse: unknown, status = 200) {
  const fetchSpy = vi.spyOn(globalThis, 'fetch')
  fetchSpy.mockImplementationOnce(async () => jsonResp(PROVIDERS))
  render(<PublishHub artifact={fakeArtifact} />, { wrapper })
  await waitFor(() => expect(screen.getByText('Public Web')).toBeDefined())
  fireEvent.click(screen.getByText('Public Web'))

  fetchSpy.mockImplementationOnce(async () => jsonResp(secondResponse, status))
  fireEvent.click(startPublishBtn())
  return fetchSpy
}

describe('PublishHub public-exposure acknowledgment (#3599)', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('Confirm & Publish opens the acknowledgment instead of publishing', async () => {
    const fetchSpy = await openCommitStep({
      requires_confirm: true, message: 'Ready to publish', bytes: 1024, content_digest: 'abc123',
    })
    await waitFor(() => expect(screen.getByText(/Confirm & Publish/)).toBeDefined())
    const callsBefore = fetchSpy.mock.calls.length

    fireEvent.click(screen.getByText(/Confirm & Publish/))

    // A modal dialog is up, naming what becomes public …
    const dialog = await waitFor(() => screen.getByRole('dialog'))
    expect(dialog.textContent).toContain('This will create a publicly accessible website')
    expect(dialog.textContent).toMatch(/Anyone in the world with the link can view test-app/)
    expect(dialog.textContent).toMatch(/Do not publish anything you would not put on the open internet/)
    // … and NOTHING was published by the click that opened it.
    expect(fetchSpy.mock.calls.length).toBe(callsBefore)
  })

  it('the acknowledge button is not pre-focused and does not submit a form', async () => {
    await openCommitStep({ requires_confirm: true, content_digest: 'abc123' })
    await waitFor(() => expect(screen.getByText(/Confirm & Publish/)).toBeDefined())
    fireEvent.click(screen.getByText(/Confirm & Publish/))
    await waitFor(() => screen.getByRole('dialog'))

    const ack = ackButton()
    expect(ack).toBeDefined()
    // Focus entered the dialog, but not onto the destructive control.
    expect(document.activeElement).not.toBe(ack)
    // Not the default action: no form to submit, so Enter cannot fire it.
    expect(ack!.getAttribute('type')).not.toBe('submit')
    expect(ack!.closest('form')).toBeNull()
  })

  it('acknowledging publishes; cancelling does not', async () => {
    const fetchSpy = await openCommitStep({ requires_confirm: true, content_digest: 'abc123' })
    await waitFor(() => expect(screen.getByText(/Confirm & Publish/)).toBeDefined())

    // Cancel path — the publish POST never happens.
    fireEvent.click(screen.getByText(/Confirm & Publish/))
    await waitFor(() => screen.getByRole('dialog'))
    const callsBefore = fetchSpy.mock.calls.length
    fireEvent.click(screen.getAllByRole('button').find(b => b.textContent === 'Cancel')!)
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(fetchSpy.mock.calls.length).toBe(callsBefore)

    // Acknowledge path — now it publishes, with confirm=true.
    fetchSpy.mockImplementationOnce(async () => jsonResp({ url: 'https://d1.cloudfront.net/x' }))
    fireEvent.click(screen.getByText(/Confirm & Publish/))
    await waitFor(() => screen.getByRole('dialog'))
    fireEvent.click(ackButton()!)

    await waitFor(() => expect(screen.getByText(/Published/)).toBeDefined())
    const post = fetchSpy.mock.calls.at(-1)!
    expect(post[0]).toBe('/api/deploy/deploy')
    const sent = JSON.parse((post[1] as RequestInit).body as string)
    expect(sent.confirm).toBe(true)
    expect(sent.override_scan).toBeUndefined()
  })

  it('states the exposure window — persistent is the dashboard default', async () => {
    // The Publish panel defaults to "Persistent (no expiry)", i.e. the LONGEST
    // exposure, so the acknowledgment has to say the link never expires on its own.
    await openCommitStep({ requires_confirm: true, content_digest: 'd' })
    await waitFor(() => expect(screen.getByText(/Confirm & Publish/)).toBeDefined())
    fireEvent.click(screen.getByText(/Confirm & Publish/))
    const dialog = await waitFor(() => screen.getByRole('dialog'))
    expect(dialog.textContent).toMatch(/stays public until you recall or destroy the deployment/)
  })

  it('the scan-override branch goes through the same gate', async () => {
    const fetchSpy = await openCommitStep({
      blocked: true, reason: 'scan', findings: 'aws_key at index.html:12',
      count: 1, credential: false, content_digest: 'abc123',
    }, 409)
    await waitFor(() => expect(screen.getByText(/Override & Publish Anyway/)).toBeDefined())
    const callsBefore = fetchSpy.mock.calls.length

    fireEvent.click(screen.getByText(/Override & Publish Anyway/))
    await waitFor(() => screen.getByRole('dialog'))
    expect(fetchSpy.mock.calls.length).toBe(callsBefore)

    fetchSpy.mockImplementationOnce(async () => jsonResp({ url: 'https://d1.cloudfront.net/x' }))
    fireEvent.click(ackButton()!)
    await waitFor(() => expect(screen.getByText(/Published/)).toBeDefined())
    const sent = JSON.parse((fetchSpy.mock.calls.at(-1)![1] as RequestInit).body as string)
    // The override survives the acknowledgment round-trip.
    expect(sent.override_scan).toBe(true)
    expect(sent.confirm).toBe(true)
  })
})

describe('PublicPublishAckModal exposure window (#3599)', () => {
  // The exposure WINDOW is half the acknowledgment: "public for a day" and
  // "public forever" are different decisions. Driven at the component so both
  // branches are covered without fighting the Radix TTL dropdown in jsdom.
  const renderModal = (ttlHours: number) =>
    render(
      <PublicPublishAckModal
        open
        target="my-app"
        ttlHours={ttlHours}
        onCancel={() => {}}
        onConfirm={() => {}}
      />,
      { wrapper },
    )

  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('names the finite TTL when one was chosen', () => {
    renderModal(72)
    expect(screen.getByRole('dialog').textContent).toMatch(/stays public for 72 hours/)
  })

  it('says the link never expires on its own when persistent', () => {
    renderModal(0)
    expect(screen.getByRole('dialog').textContent)
      .toMatch(/stays public until you recall or destroy the deployment/)
  })

  it('holds both buttons while a publish is in flight', () => {
    render(
      <PublicPublishAckModal
        open
        busy
        target="my-app"
        ttlHours={0}
        onCancel={() => {}}
        onConfirm={() => {}}
      />,
      { wrapper },
    )
    // A second click must not fire a second publish.
    const buttons = screen.getAllByRole('button')
      .filter(b => b.textContent === 'Cancel' || b.textContent === 'Publishing…')
    expect(buttons.length).toBe(2)
    for (const b of buttons) expect((b as HTMLButtonElement).disabled).toBe(true)
  })

  it('Escape cannot abandon a publish that is already in flight', () => {
    const onCancel = vi.fn()
    render(
      <PublicPublishAckModal
        open
        busy
        target="my-app"
        ttlHours={0}
        onCancel={onCancel}
        onConfirm={() => {}}
      />,
      { wrapper },
    )
    // `onClose` is guarded by `busy`: the request is already on its way to AWS,
    // so dismissing the dialog would hide an in-flight public deploy behind a
    // closed modal rather than stopping it.
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onCancel).not.toHaveBeenCalled()
  })

  it('Escape cancels when no publish is in flight', () => {
    const onCancel = vi.fn()
    render(
      <PublicPublishAckModal
        open
        target="my-app"
        ttlHours={0}
        onCancel={onCancel}
        onConfirm={() => {}}
      />,
      { wrapper },
    )
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onCancel).toHaveBeenCalled()
  })
})

describe('Artifact Deploy pending confirm acknowledgment (#3599)', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })

  it('Confirm Deploy opens the acknowledgment, and only the ack confirms', async () => {
    const confirmPath = '/pending/p1/confirm'
    const fetchSpy = vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.endsWith('/pending')) {
        return jsonResp({
          pending: [{
            id: 'p1', site_id: 'my-app', artifact_slug: 'my-app', local_dir: '',
            profile: 'default', region: 'us-west-2', ttl_hours: 72,
            scan_summary: 'clean', created_at_epoch: Math.floor(Date.now() / 1000),
          }],
        })
      }
      return jsonResp({})
    })
    const confirmed = () =>
      fetchSpy.mock.calls.some(c => String(c[0]).includes(confirmPath))

    render(<ArtifactDeployPage />, { wrapper })
    await waitFor(() => expect(screen.getByText(/Confirm Deploy/)).toBeDefined())
    expect(confirmed()).toBe(false)

    fireEvent.click(screen.getByText(/Confirm Deploy/))
    const dialog = await waitFor(() => screen.getByRole('dialog'))
    expect(dialog.textContent).toMatch(/Anyone in the world with the link can view my-app/)
    expect(dialog.textContent).toMatch(/stays public for 72 hours/)
    // Opening the dialog must not have confirmed the pending deploy.
    expect(confirmed()).toBe(false)

    fireEvent.click(ackButton()!)
    await waitFor(() => expect(confirmed()).toBe(true))
  })
})
