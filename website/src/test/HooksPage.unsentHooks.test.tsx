/**
 * A hook the user wrote in `agent.kiro_hooks` that the gateway accepted but did
 * not hand to kiro-cli. It is shown in the provider-hooks table beside
 * the hooks kiro-cli does run, under the Hooks table's own `never fires` mark,
 * with the reason the gateway gave as visible text and in the mark's name.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, within, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import type { NormalizedProviderHook, NormalizedUnsentHook } from '../providers/types'

vi.mock('../api/client', () => ({
  api: new Proxy({} as Record<string, unknown>, {
    get: (_t, prop: string) => {
      if (prop === 'hooks') return vi.fn(async () => ({ hooks: [] }))
      return vi.fn().mockResolvedValue({})
    },
  }),
}))

let sent: Record<string, NormalizedProviderHook[]> = {}
let unsent: NormalizedUnsentHook[] = []
let withUnsentReader = true
let unsentFails = 0
let unsentCalls = 0

vi.mock('../providers', () => ({
  useProvider: () => ({
    id: 'acp',
    capabilities: { hooks: true },
    labels: { hooksSection: 'Provider hooks', configFile: '~/.kiro/agents/kirocrew.json' },
    fetchProviderHooks: () => Promise.resolve(sent),
    ...(withUnsentReader ? {
      fetchUnsentProviderHooks: () => {
        unsentCalls++
        if (unsentFails > 0) { unsentFails--; return Promise.reject(new Error('gateway said no')) }
        return Promise.resolve(unsent)
      },
    } : {}),
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

const row = (over: Partial<NormalizedUnsentHook>): NormalizedUnsentHook => ({
  name: 'h', trigger: 'PostFileSave', matcher: '', actionType: 'command',
  command: '/home/me/.kiro/hooks/lint.sh', reason: 'trigger', ...over,
})

beforeEach(() => {
  sent = { preToolUse: [{ event: 'preToolUse', command: '/home/me/guard.sh', matcher: 'execute_bash', source: 'user' }] }
  unsent = []
  withUnsentReader = true
  unsentFails = 0
  unsentCalls = 0
})

describe('Provider hooks — hooks the provider was never handed', () => {
  it('shows each of the documented reasons as visible text', async () => {
    unsent = [
      row({ trigger: 'PostFileSave', reason: 'trigger' }),
      row({ trigger: 'Stop', actionType: 'agent', command: 'Summarize what changed', reason: 'agent_action' }),
      row({ trigger: 'PreToolUse', matcher: 'fs_write', reason: 'disabled' }),
      row({ trigger: 'UserPromptSubmit', reason: 'confirm' }),
    ]
    renderPage()
    const rows = await screen.findAllByTestId('unsent-hook-row')
    expect(rows).toHaveLength(4)
    expect(rows[0]).toHaveTextContent('kiro-cli has no event for this trigger')
    expect(rows[1]).toHaveTextContent('kiro-cli runs commands only')
    expect(rows[1]).toHaveTextContent('Summarize what changed')
    expect(rows[2]).toHaveTextContent('"enabled": false')
    expect(rows[2]).toHaveTextContent('Its script in ~/.kiro/hooks is skipped too')
    expect(rows[2]).toHaveTextContent('fs_write')
    expect(rows[3]).toHaveTextContent('"confirm": true')
    expect(rows[3]).toHaveTextContent('Remove it to run the hook')
    // The trigger reason names where the hook CAN run, so the reader has a fix.
    expect(rows[0]).toHaveTextContent('PreToolUse, PostToolUse, UserPromptSubmit, SessionStart, Stop')
  })

  it('carries the reason in the mark itself, not only on hover', async () => {
    unsent = [row({ reason: 'trigger' })]
    renderPage()
    const [r] = await screen.findAllByTestId('unsent-hook-row')
    const mark = within(r).getByText('never fires')
    expect(mark.getAttribute('aria-label')).toBe(
      'never fires — Not sent to kiro-cli: kiro-cli has no event for this trigger. '
        + 'It can run on PreToolUse, PostToolUse, UserPromptSubmit, SessionStart, Stop.',
    )
    expect(mark.getAttribute('title')).toContain('kiro-cli has no event for this trigger')
  })

  it('lists them after the hooks kiro-cli runs, with no order number', async () => {
    unsent = [row({})]
    renderPage()
    const region = await screen.findByRole('region', { name: 'Provider hooks' })
    await within(region).findByTestId('unsent-hook-row')
    const bodyRows = region.querySelectorAll('tbody tr')
    expect(bodyRows).toHaveLength(2)
    expect(bodyRows[0].querySelector('td')?.textContent).toBe('1')
    expect(bodyRows[1].getAttribute('data-testid')).toBe('unsent-hook-row')
    expect(bodyRows[1].querySelector('td')?.textContent).toBe('—')
  })

  it('shows the table even when kiro-cli was handed no user hook at all', async () => {
    sent = {}
    unsent = [row({ reason: 'disabled' })]
    renderPage()
    expect(await screen.findAllByTestId('unsent-hook-row')).toHaveLength(1)
  })

  it('still says the hook was left out for a reason this page does not know', async () => {
    unsent = [row({ reason: 'something_new' })]
    renderPage()
    const [r] = await screen.findAllByTestId('unsent-hook-row')
    expect(r).toHaveTextContent('Not sent to kiro-cli.')
  })

  it('adds nothing for a provider with no reader', async () => {
    withUnsentReader = false
    renderPage()
    await screen.findByRole('region', { name: 'Provider hooks' })
    expect(screen.queryByTestId('unsent-hook-row')).toBeNull()
  })

  it('reports a failed unsent read and keeps the sent hooks on screen', async () => {
    unsentFails = 1
    unsent = [row({})]
    renderPage()
    const notice = await screen.findByTestId('unsent-hooks-error')
    expect(notice).toHaveTextContent("Couldn't check which hooks were not sent to kiro-cli")
    expect(notice).toHaveTextContent('gateway said no')
    // The sent hook is still listed: only the second read failed.
    const region = await screen.findByRole('region', { name: 'Provider hooks' })
    expect(region).toHaveTextContent('/home/me/guard.sh')
    // Retry fetches the unsent list again and the notice clears.
    fireEvent.click(within(notice).getByRole('button', { name: 'Retry' }))
    expect(await screen.findAllByTestId('unsent-hook-row')).toHaveLength(1)
    await waitFor(() => expect(screen.queryByTestId('unsent-hooks-error')).toBeNull())
    expect(unsentCalls).toBe(2)
  })
})
