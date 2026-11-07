/**
 * A pending deploy confirmation expires an hour after its preview (#17022). The
 * row says how long is left, read from the server's `expires_at_epoch`, and an
 * expiry that empties the list does not take the card's own error or open
 * acknowledgment with it.
 *
 * Expiry is simulated the way the gateway does it: the next `/pending` read no
 * longer carries the entry. The test triggers that read itself by invalidating
 * the query, so nothing waits on the 10 s poll.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import ArtifactDeployPage from '../pages/ArtifactDeployPage'

const NOW_MS = 1_700_000_000_000
const ACK = 'I understand, publish publicly'

function entry(expiresAtEpoch: number) {
  return {
    id: 'p1',
    site_id: 'my-app',
    artifact_slug: 'my-app',
    local_dir: '',
    profile: 'default',
    region: 'us-west-2',
    ttl_hours: 72,
    scan_summary: 'clean',
    created_at_epoch: expiresAtEpoch - 3600,
    expires_at_epoch: expiresAtEpoch,
    override_scan_required: false,
  }
}
type Entry = ReturnType<typeof entry>
/** What a confirm or dismiss call answers, and the list the next `/pending` read returns. */
type Reply = { status: number; body: Record<string, unknown>; listAfter: Entry[] }

const json = (data: unknown, status = 200) =>
  new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } })

function serve(pending: Entry[], replies: { confirm?: Reply; dismiss?: Reply } = {}) {
  let listed = pending
  vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
    const url = String(input)
    if (url.endsWith('/pending')) return json({ pending: listed })
    for (const action of ['confirm', 'dismiss'] as const) {
      const reply = replies[action]
      if (reply && url.endsWith(`/pending/p1/${action}`)) {
        listed = reply.listAfter
        return json(reply.body, reply.status)
      }
    }
    return json({})
  })
  return { expire: () => { listed = [] } }
}

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter><ArtifactDeployPage /></MemoryRouter>
    </QueryClientProvider>,
  )
  return { refetchPending: () => qc.invalidateQueries({ queryKey: ['deploy-web', 'pending'] }) }
}

async function confirmRow() {
  fireEvent.click(await screen.findByText('Confirm Deploy'))
  fireEvent.click(await screen.findByRole('button', { name: ACK }))
}

describe('pending row: time left', () => {
  beforeEach(() => {
    // shouldAdvanceTime: the panel's query carries a refetchInterval and the
    // render is awaited, so a frozen timer would stall `findBy`.
    vi.useFakeTimers({ shouldAdvanceTime: true })
    vi.setSystemTime(NOW_MS)
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('reads the deadline from expires_at_epoch', async () => {
    // Created just now, so a client-side created_at_epoch + 1 hour would read 59 minutes.
    serve([{ ...entry(NOW_MS / 1000 + 23 * 60 + 30), created_at_epoch: NOW_MS / 1000 }])
    renderPage()
    expect(await screen.findByText('Preview expires in 23 minutes unless confirmed')).toBeTruthy()
  })

  it('reads "now", never "ago", when the deadline is already behind the browser clock', async () => {
    serve([entry(NOW_MS / 1000 - 90)])
    renderPage()
    expect(await screen.findByText('Preview expires now unless confirmed')).toBeTruthy()
  })
})

describe('pending card after its last row expires', () => {
  // No assertion reads a time here, so the deadline is a fixed instant.
  const row = entry(NOW_MS / 1000)
  afterEach(() => { vi.restoreAllMocks() })

  it.each([
    ['confirm', 409, 'pending entry not found, expired, or already claimed'],
    ['dismiss', 404, 'pending entry not found or expired'],
  ] as const)('keeps the failed %s error on screen', async (action, status, error) => {
    serve([row], { [action]: { status, body: { error }, listAfter: [] } })
    const { refetchPending } = renderPage()

    if (action === 'confirm') await confirmRow()
    else fireEvent.click(await screen.findByText('Dismiss'))
    await screen.findByText(error)
    // A failed dismiss does not refetch on its own; this is the poll that drops the row.
    await refetchPending()

    await waitFor(() => expect(screen.queryByText('my-app')).toBeNull())
    expect(screen.getByText(error)).toBeTruthy()
  })

  it('keeps the publish acknowledgment open when its row expires under it', async () => {
    const { expire } = serve([row])
    const { refetchPending } = renderPage()

    fireEvent.click(await screen.findByText('Confirm Deploy'))
    await screen.findByRole('button', { name: ACK })
    expire()
    await refetchPending()

    await waitFor(() => expect(screen.queryByText('Confirm Deploy')).toBeNull())
    expect(screen.getByRole('button', { name: ACK })).toBeTruthy()
  })

  it.each([
    ['confirm', 'dismiss'],
    ['dismiss', 'confirm'],
  ] as const)('does not stay up for a failed %s once a %s empties the list', async (failed, succeeds) => {
    const error = 'The gateway could not finish the request.'
    serve([row], {
      [failed]: { status: 502, body: { error }, listAfter: [row] },
      [succeeds]: { status: 200, body: { ok: true }, listAfter: [] },
    })
    renderPage()

    const act = async (action: 'confirm' | 'dismiss') => {
      if (action === 'confirm') await confirmRow()
      else fireEvent.click(await screen.findByText('Dismiss'))
    }
    await act(failed)
    await screen.findByText(error)
    await act(succeeds)

    await waitFor(() => {
      expect(screen.queryByText('my-app')).toBeNull()
      expect(screen.queryByText(error)).toBeNull()
    })
  })

  // The running action is on p1, the other one on p2; each disables its own buttons while it runs.
  it.each([
    ['confirm', 'dismiss', 'Confirm Deploy'],
    ['dismiss', 'confirm', 'Dismiss'],
  ] as const)('leaves a running %s alone when a %s starts on another row', async (running, other, runningLabel) => {
    let finish: (r: Response) => void = () => {}
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input) => {
      const url = String(input)
      if (url.endsWith('/pending')) return json({ pending: [row, { ...row, id: 'p2', site_id: 'other-app' }] })
      if (url.endsWith(`/pending/p1/${running}`)) return new Promise<Response>((resolve) => { finish = resolve })
      return json({ ok: true })
    })
    renderPage()
    const start = async (action: 'confirm' | 'dismiss', index: number) => {
      fireEvent.click((await screen.findAllByText(action === 'confirm' ? 'Confirm Deploy' : 'Dismiss'))[index])
      if (action === 'confirm') fireEvent.click(await screen.findByRole('button', { name: ACK }))
    }
    const runningButton = () => screen.getAllByText(runningLabel)[0].closest('button')

    await start(running, 0)
    await waitFor(() => expect(runningButton()).toBeDisabled())
    await start(other, 1)

    await waitFor(() => expect(fetch).toHaveBeenCalledWith(expect.stringMatching(new RegExp(`/pending/p2/${other}$`)), expect.anything()))
    expect(runningButton()).toBeDisabled()
    finish(json({ ok: true }))
    await waitFor(() => expect(runningButton()).not.toBeDisabled())
  })
})
