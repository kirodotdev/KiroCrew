/**
 * "Open in kiro-cli" in the session menu: when it shows, why it is disabled,
 * what it says about the agent, and that choosing it opens a docked terminal tab
 * that names only the chat.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const mocks = vi.hoisted(() => ({
  chatSlotKiroCli: vi.fn(),
  terminalEnabled: true,
}))
vi.mock('../api/client', () => ({
  api: new Proxy(mocks as Record<string, unknown>, {
    get: (t, p: string) => (p in t ? t[p] : vi.fn().mockResolvedValue({})),
  }),
}))
vi.mock('../utils/terminalRegistry', async (orig) => ({
  ...(await orig<typeof import('../utils/terminalRegistry')>()),
  useTerminalEnabled: () => mocks.terminalEnabled,
}))

import OpenInKiroCliItem from '../components/OpenInKiroCliItem'
import { __resetBottomTerminal, addTab, MAX_TERMINALS, useBottomTerminal } from '../hooks/useBottomTerminal'

function ItemStub({ onSelect, disabled, children, ...rest }: {
  readonly onSelect?: (e: Event) => void
  readonly disabled?: boolean
  readonly children?: React.ReactNode
}) {
  return <button disabled={disabled} onClick={() => onSelect?.(new Event('select'))} {...rest}>{children}</button>
}

function Tabs() {
  const { tabs } = useBottomTerminal()
  return <output data-testid="tabs">{JSON.stringify(tabs.map(t => ({ kiroCliSlot: t.kiroCliSlot, cwd: t.cwd, chatSlot: t.chatSlot })))}</output>
}

function renderItem(running = false) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <OpenInKiroCliItem slotKey="chat-1" running={running} Item={ItemStub} />
      <Tabs />
    </QueryClientProvider>,
  )
}

describe('OpenInKiroCliItem', () => {
  beforeEach(() => {
    mocks.chatSlotKiroCli.mockReset()
    mocks.terminalEnabled = true
    __resetBottomTerminal()
  })

  it('opens a docked tab that names only the chat', async () => {
    mocks.chatSlotKiroCli.mockResolvedValue({ available: true, agent: 'kirocrew', agent_without_spec: '' })
    renderItem()
    await userEvent.click(await screen.findByRole('button', { name: /Open in kiro-cli/ }))
    expect(JSON.parse(screen.getByTestId('tabs').textContent ?? '[]')).toEqual([{ kiroCliSlot: 'chat-1' }])
    // A second choice focuses that tab instead of opening another.
    await userEvent.click(screen.getByRole('button', { name: /Open in kiro-cli/ }))
    expect(JSON.parse(screen.getByTestId('tabs').textContent ?? '[]')).toHaveLength(1)
  })

  it('says when kiro-cli opens with its default agent', async () => {
    mocks.chatSlotKiroCli.mockResolvedValue({ available: true, agent: '', agent_without_spec: 'kirocrew-heartbeat' })
    renderItem()
    expect(await screen.findByTestId('open-in-kiro-cli-agent-note')).toHaveTextContent(
      "Opens with kiro-cli's default agent. kirocrew-heartbeat has no agent file kiro-cli can load.",
    )
  })

  it('is disabled with the reason while the chat cannot open yet', async () => {
    mocks.chatSlotKiroCli.mockResolvedValue({ available: false, code: 'kiro_cli_no_session' })
    renderItem()
    const row = await screen.findByRole('button', { name: /Open in kiro-cli/ })
    expect(row).toBeDisabled()
    expect(row).toHaveTextContent('no conversation yet')
  })

  it('is disabled while a turn runs', async () => {
    mocks.chatSlotKiroCli.mockResolvedValue({ available: true, agent: 'a', agent_without_spec: '' })
    renderItem(true)
    const row = await screen.findByRole('button', { name: /Open in kiro-cli/ })
    expect(row).toBeDisabled()
    expect(row).toHaveTextContent('turn running')
  })

  it('is disabled when the docked panel has no room for another tab', async () => {
    mocks.chatSlotKiroCli.mockResolvedValue({ available: true, agent: 'a', agent_without_spec: '' })
    for (let i = 0; i < MAX_TERMINALS; i++) addTab('/w')
    renderItem()
    const row = await screen.findByRole('button', { name: /Open in kiro-cli/ })
    expect(row).toBeDisabled()
    expect(row).toHaveTextContent('terminal tabs full')
  })

  it.each(['kiro_cli_not_kiro', 'kiro_cli_unsupported', 'kiro_cli_remote', 'kiro_cli_terminal_disabled'])(
    'is left out when the chat can never open in kiro-cli (%s)',
    async (code) => {
      mocks.chatSlotKiroCli.mockResolvedValue({ available: false, code })
      renderItem()
      await expect(mocks.chatSlotKiroCli.mock.results[0]?.value).resolves.toBeTruthy()
      expect(screen.queryByText('Open in kiro-cli')).toBeNull()
    },
  )

  it('is left out, without asking the gateway, when the terminal panel is off', () => {
    mocks.terminalEnabled = false
    renderItem()
    expect(screen.queryByText('Open in kiro-cli')).toBeNull()
    expect(mocks.chatSlotKiroCli).not.toHaveBeenCalled()
  })

  it('shows a failed availability check instead of hiding the row', async () => {
    mocks.chatSlotKiroCli.mockRejectedValue(new Error('gateway unreachable'))
    renderItem()
    expect(await screen.findByText(/gateway unreachable/)).toBeTruthy()
    expect(screen.getAllByText('Open in kiro-cli').length).toBeGreaterThan(0)
  })
})
