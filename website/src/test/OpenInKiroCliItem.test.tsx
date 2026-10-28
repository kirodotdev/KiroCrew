/**
 * "Open in kiro-cli" in the session menu: when it shows, why it is disabled,
 * what it says will happen and about the agent, and that choosing it opens a
 * docked terminal tab that names only the chat, labelled with its title.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const mocks = vi.hoisted(() => ({
  chatSlotKiroCli: vi.fn(),
  terminalEnabled: true,
  poppedOut: false,
  bringBack: vi.fn(),
}))
vi.mock('../utils/terminalPopout', async (orig) => ({
  ...(await orig<typeof import('../utils/terminalPopout')>()),
  isPopoutOpen: () => mocks.poppedOut,
  bringBack: () => { mocks.bringBack(); mocks.poppedOut = false },
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
import { PREVIEW_OPEN_IN_KIRO_CLI, setPreviewFlag } from '../utils/previewFlags'
import { kiroCliBlockedLabel } from '../lib/kiroCli'
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
  return <output data-testid="tabs">{JSON.stringify(tabs.map(t => ({ kiroCliSlot: t.kiroCliSlot, cwd: t.cwd, chatSlot: t.chatSlot, name: t.name })))}</output>
}

function renderItem(running = false, title?: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <OpenInKiroCliItem slotKey="chat-1" title={title} running={running} Item={ItemStub} />
      <Tabs />
    </QueryClientProvider>,
  )
}

describe('OpenInKiroCliItem', () => {
  beforeEach(() => {
    mocks.chatSlotKiroCli.mockReset()
    mocks.terminalEnabled = true
    mocks.poppedOut = false
    mocks.bringBack.mockReset()
    __resetBottomTerminal()
    localStorage.clear()
    // Every test below is about the row itself, so its Feature Preview is on;
    // the preview's own tests turn it off.
    setPreviewFlag(PREVIEW_OPEN_IN_KIRO_CLI, true)
  })

  it('is left out, and asks the gateway nothing, while its Feature Preview is off (the default)', async () => {
    localStorage.clear()
    mocks.chatSlotKiroCli.mockResolvedValue({ available: true, agent: 'kirocrew', agent_without_spec: '' })
    const { container } = renderItem()
    await act(async () => { await Promise.resolve() })
    expect(screen.queryByTestId('open-in-kiro-cli')).toBeNull()
    expect(container.querySelector('[data-testid="open-in-kiro-cli"]')).toBeNull()
    expect(mocks.chatSlotKiroCli).not.toHaveBeenCalled()
  })

  it('appears once its Feature Preview is turned on, with no reload', async () => {
    setPreviewFlag(PREVIEW_OPEN_IN_KIRO_CLI, false)
    mocks.chatSlotKiroCli.mockResolvedValue({ available: true, agent: 'kirocrew', agent_without_spec: '' })
    renderItem()
    expect(screen.queryByTestId('open-in-kiro-cli')).toBeNull()
    act(() => { setPreviewFlag(PREVIEW_OPEN_IN_KIRO_CLI, true) })
    expect(await screen.findByTestId('open-in-kiro-cli')).toBeInTheDocument()
  })

  it('is left out when the gateway answers that the preview is off', async () => {
    mocks.chatSlotKiroCli.mockResolvedValue({ available: false, code: 'kiro_cli_preview_off' })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <OpenInKiroCliItem slotKey="chat-1" running={false} Item={ItemStub} />
      </QueryClientProvider>,
    )
    await waitFor(() => expect(qc.getQueryData(['kiro-cli-availability', 'chat-1', false])).toBeDefined())
    expect(screen.queryByTestId('open-in-kiro-cli')).toBeNull()
  })

  it('brings a popped-out panel back so this window opens the tab', async () => {
    mocks.chatSlotKiroCli.mockResolvedValue({ available: true, agent: 'kirocrew', agent_without_spec: '' })
    mocks.poppedOut = true
    renderItem()
    const user = userEvent.setup()
    await user.click(await screen.findByTestId('open-in-kiro-cli'))
    expect(mocks.bringBack).toHaveBeenCalledTimes(1)
    expect(screen.getByTestId('tabs').textContent).toBe(JSON.stringify([{ kiroCliSlot: 'chat-1' }]))
  })

  it('leaves a docked panel where it is', async () => {
    mocks.chatSlotKiroCli.mockResolvedValue({ available: true, agent: 'kirocrew', agent_without_spec: '' })
    renderItem()
    const user = userEvent.setup()
    await user.click(await screen.findByTestId('open-in-kiro-cli'))
    expect(mocks.bringBack).not.toHaveBeenCalled()
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

  it('says on the row where it opens and how to come back to the chat', async () => {
    mocks.chatSlotKiroCli.mockResolvedValue({ available: true, agent: 'kirocrew', agent_without_spec: '' })
    renderItem()
    expect(await screen.findByTestId('open-in-kiro-cli-note')).toHaveTextContent('Continues in the terminal panel. Close its terminal tab to return; nothing is lost.')
  })

  it('says in its hover text when kiro-cli opens with its default agent', async () => {
    mocks.chatSlotKiroCli.mockResolvedValue({ available: true, agent: '', agent_without_spec: 'kirocrew-heartbeat' })
    renderItem()
    const row = await screen.findByTestId('open-in-kiro-cli')
    expect(row).toHaveAttribute(
      'title',
      "Continue this Kiro Agent conversation in kiro-cli in the terminal. This chat pauses until kiro-cli exits. "
        + 'kiro-cli will use its own default agent, not kirocrew-heartbeat.',
    )
    expect(screen.getByTestId('open-in-kiro-cli-note')).toHaveTextContent('Continues in the terminal panel. Close its terminal tab to return; nothing is lost.')
  })

  it('labels the tab with the chat title', async () => {
    mocks.chatSlotKiroCli.mockResolvedValue({ available: true, agent: 'kirocrew', agent_without_spec: '' })
    renderItem(false, 'Fix the login bug')
    await userEvent.click(await screen.findByTestId('open-in-kiro-cli'))
    expect(JSON.parse(screen.getByTestId('tabs').textContent ?? '[]')).toEqual([
      { kiroCliSlot: 'chat-1', name: 'Fix the login bug' },
    ])
  })

  it('is disabled with the reason while the chat cannot open yet', async () => {
    mocks.chatSlotKiroCli.mockResolvedValue({ available: false, code: 'kiro_cli_no_session' })
    renderItem()
    const row = await screen.findByRole('button', { name: /Open in kiro-cli/ })
    expect(row).toBeDisabled()
    expect(row).toHaveTextContent('no conversation yet')
    expect(screen.queryByTestId('open-in-kiro-cli-note')).toBeNull()
  })

  it('shows its note at normal contrast', async () => {
    mocks.chatSlotKiroCli.mockResolvedValue({ available: true, agent: 'kirocrew', agent_without_spec: '' })
    renderItem()
    expect(await screen.findByTestId('open-in-kiro-cli-note')).not.toHaveClass('text-muted')
  })

  it('gives the reason a project has its own config, without asking anyone to delete files', async () => {
    mocks.chatSlotKiroCli.mockResolvedValue({ available: false, code: 'kiro_cli_project_agent' })
    renderItem()
    const row = await screen.findByRole('button', { name: /Open in kiro-cli/ })
    expect(row).toHaveTextContent("this project's .kiro or .amazonq folder has its own kiro-cli agents or MCP settings; keep using this chat here instead")
  })

  it.each([
    'kiro_cli_no_session', 'kiro_cli_no_log', 'kiro_cli_no_project', 'kiro_cli_not_installed', 'kiro_cli_busy', 'kiro_cli_subagents', 'kiro_cli_already_open', 'kiro_cli_project_agent', 'kiro_cli_ended', 'kiro_cli_launch_spec', 'kiro_cli_record', 'kiro_cli_session_file', 'kiro_cli_starting',
  ])('names no file to remove or delete in reason %s', (code) => {
    expect(kiroCliBlockedLabel(code)).not.toMatch(/\b(remove|delete)\b/i)
  })

  it('is disabled while a turn runs', async () => {
    mocks.chatSlotKiroCli.mockResolvedValue({ available: true, agent: 'a', agent_without_spec: '' })
    renderItem(true)
    const row = await screen.findByRole('button', { name: /Open in kiro-cli/ })
    expect(row).toBeDisabled()
    expect(row).toHaveTextContent('a reply is still running')
  })

  it('is disabled when the docked panel has no room for another tab', async () => {
    mocks.chatSlotKiroCli.mockResolvedValue({ available: true, agent: 'a', agent_without_spec: '' })
    for (let i = 0; i < MAX_TERMINALS; i++) addTab('/w')
    renderItem()
    const row = await screen.findByRole('button', { name: /Open in kiro-cli/ })
    expect(row).toBeDisabled()
    expect(row).toHaveTextContent('close a terminal tab first')
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

  it('is left out for a Kiro CLI harness chat: the gateway opens Kiro Agent chats only', async () => {
    // A Kiro CLI, Claude Code or Codex chat: the server answers kiro_cli_not_kiro.
    mocks.chatSlotKiroCli.mockResolvedValue({
      available: false,
      code: 'kiro_cli_not_kiro',
      reason: 'Open in kiro-cli works only for chats on the Kiro Agent harness',
    })
    renderItem()
    await waitFor(() => expect(mocks.chatSlotKiroCli).toHaveBeenCalledWith('chat-1'))
    // Let the query settle, so the row's absence is the answer's, not a load.
    await act(async () => {
      await mocks.chatSlotKiroCli.mock.results[0]?.value
      await new Promise(resolve => setTimeout(resolve, 0))
    })
    expect(screen.queryByTestId('open-in-kiro-cli')).toBeNull()
    expect(screen.queryByText(/Kiro Agent harness/)).toBeNull()
  })

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
