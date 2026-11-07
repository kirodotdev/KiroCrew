/**
 * The provider-hooks card's rescan control. A script added to the hooks folder
 * after the gateway started reaches the provider's hook config only when it is
 * rewritten, so the control asks the provider to rewrite it, then shows the
 * refreshed list -- or says the config was not rewritten.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import type { NormalizedProviderHook } from '../providers/types'

vi.mock('../api/client', () => ({
  api: new Proxy({} as Record<string, unknown>, {
    get: (_t, prop: string) => {
      if (prop === 'hooks') return vi.fn(async () => ({ hooks: [] }))
      return vi.fn().mockResolvedValue({})
    },
  }),
}))

const SCRIPT = '/home/me/.kiro/hooks/guard.sh'
let sent: Record<string, NormalizedProviderHook[]> = {}
let rewritten = true
const rescan = vi.fn(async () => {
  if (rewritten) sent = { preToolUse: [{ event: 'preToolUse', command: SCRIPT, matcher: '', source: 'user' }] }
  return rewritten
})

vi.mock('../providers', () => ({
  useProvider: () => ({
    id: 'acp',
    capabilities: { hooks: true },
    labels: { hooksSection: 'Provider hooks', configFile: '~/.kiro/agents/kirocrew.json' },
    fetchProviderHooks: () => Promise.resolve(sent),
    rescanProviderHooks: rescan,
  }),
}))

import HooksPage from '../pages/HooksPage'

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <HooksPage />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  sent = {}
  rewritten = true
  rescan.mockClear()
})

describe('Provider hooks — rescan the hooks folder', () => {
  it('lists the script a rescan picked up', async () => {
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'Rescan hooks folder' }))

    // The line appears only once the list it describes has been refetched.
    expect(await screen.findByRole('status')).toHaveTextContent('the Logs page says why')
    expect(screen.getByText(SCRIPT)).toBeInTheDocument()
    expect(rescan).toHaveBeenCalledTimes(1)
  })

  it('keeps focus on Rescan while it runs, and ignores a second press', async () => {
    let finish: (v: boolean) => void = () => {}
    rescan.mockImplementationOnce(() => new Promise<boolean>(r => { finish = r }))
    renderPage()
    const btn = await screen.findByRole('button', { name: 'Rescan hooks folder' })
    btn.focus()
    // Two presses in the same tick, before any re-render, still start one rescan.
    fireEvent.click(btn)
    fireEvent.click(btn)

    await waitFor(() => expect(btn).toHaveAttribute('aria-busy', 'true'))
    expect(btn).toHaveAttribute('aria-disabled', 'true')
    expect(btn).not.toBeDisabled()
    expect(btn).toHaveFocus()
    fireEvent.click(btn)
    expect(rescan).toHaveBeenCalledTimes(1)

    await act(async () => { finish(true) })
    await waitFor(() => expect(btn).toHaveAttribute('aria-disabled', 'false'))
  })

  it('says so when the config was not rewritten', async () => {
    rewritten = false
    renderPage()
    fireEvent.click(await screen.findByRole('button', { name: 'Rescan hooks folder' }))

    expect(await screen.findByTestId('rescan-not-applied')).toHaveTextContent('The agent config was not rewritten')
    expect(screen.queryByText(SCRIPT)).not.toBeInTheDocument()
  })
})
