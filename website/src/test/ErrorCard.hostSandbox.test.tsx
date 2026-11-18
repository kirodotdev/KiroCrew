// The error row a chat leaves when the Crew's host cannot build an OS sandbox
// (`meta.code: host_sandbox_unavailable`). The machine owner keeps the full
// diagnostic and Resume; a teammate gets one line naming whom to ask, Copy
// details (code and time only), and no Resume unless the refusal was momentary.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const clipboard = vi.hoisted(() => ({ copyToClipboard: vi.fn<(text: string) => Promise<boolean>>() }))
vi.mock('../utils/clipboard', async (importOriginal) => ({ ...(await importOriginal<object>()), ...clipboard }))

import { ErrorCard } from '../pages/chat/ErrorCard'

// Chained reads: /api/auth/me answers first, then enables /api/shared-crew/mode,
// so the line lands after two round trips, past the 1s default under load.
const CHAIN_TIMEOUT_MS = 5000

const DIAGNOSTIC = 'Sandbox backend unavailable and allow_unsandboxed_exec is not set. Probe detail: unshare(CLONE_NEWNS) failed with errno 1 (EPERM).'
const CONTENT = `host_sandbox_unavailable: ${DIAGNOSTIC}`
const AT = '2026-11-17T05:54:00+00:00'
const META = { code: 'host_sandbox_unavailable', at: AT }

/** Answers `/api/auth/me` with `ownerOk`, and the mode route with `ownerLabel`
 *  (a 404 when it is `null`, as with the Shared Crew preview off). */
function stubFetch(ownerOk: boolean, ownerLabel: string | null) {
  const json = (body: unknown, status = 200) => ({
    ok: status < 400, status,
    json: async () => body,
    text: async () => JSON.stringify(body),
    headers: new Headers({ 'content-type': 'application/json' }),
  })
  const fetch = vi.fn(async (input: unknown) => {
    const url = String(input)
    if (url.includes('/api/auth/me')) return json({ owner_ok: ownerOk })
    if (url.includes('/api/shared-crew/mode')) return ownerLabel === null ? json({ error: 'not found' }, 404) : json({ owner_label: ownerLabel })
    return json({})
  })
  vi.stubGlobal('fetch', fetch)
  return fetch
}

function mount(props: Partial<Parameters<typeof ErrorCard>[0]> = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <ErrorCard content={CONTENT} meta={META} {...props} />
    </QueryClientProvider>,
  )
}

describe('ErrorCard: the host cannot build a sandbox', () => {
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
    clipboard.copyToClipboard.mockReset()
  })

  it('gives a teammate one line naming the owner, Copy details and no Resume', async () => {
    stubFetch(false, 'build-box')
    clipboard.copyToClipboard.mockResolvedValue(true)
    const onContinue = vi.fn()
    mount({ onContinue })
    expect(await screen.findByText("This Crew's computer can't start chats right now. Ask build-box (Crew owner) to check it.", {}, { timeout: CHAIN_TIMEOUT_MS })).toBeInTheDocument()
    expect(screen.getByTestId('error-card')).not.toHaveTextContent('unshare')
    expect(screen.queryByTestId('error-card-continue')).toBeNull()
    fireEvent.click(screen.getByTestId('error-card-copy-details'))
    await waitFor(() => expect(screen.getByTestId('error-card-copy-details')).toHaveTextContent('Copied'))
    expect(clipboard.copyToClipboard).toHaveBeenCalledWith(`host_sandbox_unavailable ${AT}`)
  })

  it('names the Crew\'s owner when the mode route is unavailable', async () => {
    stubFetch(false, null)
    mount()
    expect(await screen.findByText("This Crew's computer can't start chats right now. Ask the Crew's owner to check it.")).toBeInTheDocument()
  })

  it('keeps Resume for a teammate when the refusal was momentary, and says to press it', async () => {
    stubFetch(false, 'build-box')
    mount({ meta: { ...META, retryable: true }, onContinue: () => {} })
    expect(await screen.findByText("This Crew couldn't start this chat just now. Press Resume to try again.")).toBeInTheDocument()
    expect(screen.getByTestId('error-card')).not.toHaveTextContent('Ask build-box')
    expect(screen.getByTestId('error-card-copy-details')).toBeInTheDocument()
    expect(screen.getByTestId('error-card-continue')).toBeInTheDocument()
  })

  it('keeps the full diagnostic and Resume for the machine owner', async () => {
    const fetch = stubFetch(true, 'build-box')
    mount({ onContinue: () => {} })
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(expect.stringContaining('/api/auth/me'), expect.anything()))
    await act(async () => {})
    expect(screen.getByTestId('error-card')).toHaveTextContent(DIAGNOSTIC)
    expect(screen.getByTestId('error-card')).not.toHaveTextContent('host_sandbox_unavailable:')
    expect(screen.getByTestId('error-card-continue')).toBeInTheDocument()
    expect(screen.queryByTestId('error-card-copy-details')).toBeNull()
  })
})
