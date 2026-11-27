// MCP Management: the working-folder question.
//
// Contract under test:
// - sharing a server that is not Kiro Crew's own and has no "does not use the
//   folder" answer asks first; "Share across chats" records the answer and then
//   shares, pinned to the launch read as usual; "Keep one per chat" records
//   "uses the folder" and shares nothing
// - a server shared before the question existed keeps its switch, and its row
//   asks once; "Keep one per chat" takes it off sharing in the same request
// - the dialog opens with focus on "Keep one per chat", the safe choice
// - Kiro Crew's own servers and servers already answered "No" are never asked
// - the bulk share never offers a server that waits for the answer
// - a server the gateway saw ask for roots has no switch, is never asked, and
//   its Details say why
// - a roots row's answer sends only the answer; the page follows the gateway's
//   reply: "can now be shared" when nothing was routed, the restart or not-live
//   notice when it was, and the launch preview when its approval is needed;
//   a saved answer is not offered again
//
// Every label is read from the catalog, so a wording change never breaks a test.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { api, ApiError, type McpManagedServer } from '../api/client'
import { i18nT } from '../i18n/t'
import { McpManagement } from '../pages/settings/McpManagement'
import {
  autoShareCandidates,
  detailsWhyKey,
  needsCwdAnswer,
  offersRootsAnswer,
  rowState,
} from '../pages/settings/mcpManagementModel'
import { answerBatchPreviewsPerName } from './McpManagementTestUtils'

const M = (key: string, vars?: Record<string, unknown>) => i18nT(`pages.mcpManagement.${key}`, vars)

function server(over: Record<string, unknown> = {}) {
  return {
    name: 'alpha-mcp',
    stub: false,
    can_stub: true,
    in_allowlist: false,
    entry_poolable: false,
    agents: ['kirocrew'],
    transport: 'stdio',
    denylisted: false,
    cwd_use: null,
    cwd_proven: false,
    ...over,
  }
}

const launch = {
  name: 'alpha-mcp',
  commands: [['/opt/alpha']],
  envs: [[]],
  complete: true,
  expected_launch: 'alpha-identity',
}

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  return render(
    <MemoryRouter>
      <QueryClientProvider client={qc}>
        <McpManagement />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

function serve(row: Record<string, unknown>) {
  vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue({
    enabled: true, stub: [], stub_count: 0, running: false, ping_ok: false, supported: true,
  } as never)
  vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server(row)] } as never)
  vi.spyOn(api, 'mcpGatewayLaunchPreview').mockResolvedValue(launch as never)
}

const findSwitch = () =>
  screen.findByRole('switch', { name: `${M('share_toggle')} alpha-mcp` }, { timeout: 5000 })

beforeEach(() => {
  vi.restoreAllMocks()
  vi.spyOn(api, 'mcpGatewayFootprint').mockResolvedValue({ using_now_bytes: null, with_sharing_bytes: null, open_chats: 0, stale_chats: [] } as never)
  answerBatchPreviewsPerName()
})
afterEach(cleanup)

describe('the working-folder question', () => {
  it('asks before sharing, and "Share across chats" records the answer and then shares', async () => {
    serve({})
    const answer = vi.spyOn(api, 'mcpGatewaySetCwdUse').mockResolvedValue({ ok: true, name: 'alpha-mcp' } as never)
    const setStub = vi.spyOn(api, 'mcpGatewaySetStub').mockResolvedValue({ ok: true, name: 'alpha-mcp', stub: true } as never)

    mount()
    fireEvent.click(await findSwitch())

    const dialog = await screen.findByRole('dialog', {}, { timeout: 5000 })
    expect(dialog.textContent).toContain(M('cwd_question_title', { name: 'alpha-mcp' }))
    expect(dialog.textContent).toContain(M('cwd_question_help'))
    expect(dialog.textContent).toContain(M('cwd_question_not_sure'))
    // Opening focus is on the safe choice, so Enter never shares by accident.
    await waitFor(
      () => expect(document.activeElement).toBe(within(dialog).getByRole('button', { name: M('cwd_question_keep') })),
      { timeout: 5000 },
    )
    expect(api.mcpGatewayLaunchPreview).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: M('cwd_question_share') }))

    await waitFor(() => expect(setStub).toHaveBeenCalledWith('alpha-mcp', true, 'alpha-identity'), { timeout: 5000 })
    expect(answer).toHaveBeenCalledWith('alpha-mcp', 'not_used')
  })

  it('Cancel closes the question and shares nothing', async () => {
    serve({})
    const answer = vi.spyOn(api, 'mcpGatewaySetCwdUse')
    const setStub = vi.spyOn(api, 'mcpGatewaySetStub')

    mount()
    fireEvent.click(await findSwitch())
    const dialog = await screen.findByRole('dialog', {}, { timeout: 5000 })
    fireEvent.click(within(dialog).getAllByRole('button', { name: M('cancel') })[0])
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull(), { timeout: 5000 })

    fireEvent.click(await findSwitch())
    await screen.findByRole('dialog', {}, { timeout: 5000 })
    fireEvent.keyDown(document.activeElement ?? document.body, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull(), { timeout: 5000 })

    expect(answer).not.toHaveBeenCalled()
    expect(setStub).not.toHaveBeenCalled()
  })

  it('"Keep one per chat" keeps the server in each chat and shares nothing', async () => {
    serve({})
    const answer = vi.spyOn(api, 'mcpGatewaySetCwdUse').mockResolvedValue({ ok: true, name: 'alpha-mcp' } as never)
    const setStub = vi.spyOn(api, 'mcpGatewaySetStub')

    mount()
    fireEvent.click(await findSwitch())
    fireEvent.click(await screen.findByRole('button', { name: M('cwd_question_keep') }))

    await waitFor(() => expect(answer).toHaveBeenCalledWith('alpha-mcp', 'uses_cwd'), { timeout: 5000 })
    await screen.findByText(M('cwd_kept_per_chat', { name: 'alpha-mcp' }), {}, { timeout: 5000 })
    expect(setStub).not.toHaveBeenCalled()
    expect(api.mcpGatewayLaunchPreview).not.toHaveBeenCalled()
  })

  it('asks once on a server shared before the question, and "Keep one per chat" stops sharing it', async () => {
    serve({ stub: true, in_allowlist: true })
    const setStub = vi.spyOn(api, 'mcpGatewaySetStub').mockResolvedValue({ ok: true, name: 'alpha-mcp', stub: false } as never)

    mount()
    expect((await findSwitch()).getAttribute('aria-checked')).toBe('true')
    screen.getByText(M('cwd_unanswered_hint'))
    fireEvent.click(screen.getByRole('button', { name: `${M('cwd_unanswered_answer')} alpha-mcp` }))
    fireEvent.click(await screen.findByRole('button', { name: M('cwd_question_keep') }))

    await waitFor(
      () => expect(setStub).toHaveBeenCalledWith('alpha-mcp', false, undefined, false, 'uses_cwd'),
      { timeout: 5000 },
    )
  })

  it('asks before approving an updated launch of an unanswered server, and "Share across chats" approves with the answer', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue({
      enabled: true, stub: ['alpha-mcp'], stub_count: 1, running: true, ping_ok: true, supported: true,
      launch_refused: {
        'alpha-mcp': {
          reason: 'changed_needs_reapproval',
          commands: [['/opt/alpha', '--two']],
          envs: [[]],
          approved_commands: [['/opt/alpha', '--one']],
          approved_envs: [[]],
          complete: true,
          expected_launch: 'a:b',
        },
      },
    } as never)
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server({ stub: true, in_allowlist: true })] } as never)
    const setStub = vi.spyOn(api, 'mcpGatewaySetStub').mockResolvedValue({ ok: true, name: 'alpha-mcp', stub: true } as never)

    mount()
    fireEvent.click(await screen.findByRole('button', { name: `${M('new_command_button')} alpha-mcp` }, { timeout: 5000 }))
    expect(setStub).not.toHaveBeenCalled()
    fireEvent.click(await screen.findByRole('button', { name: M('cwd_question_share') }))

    await waitFor(
      () => expect(setStub).toHaveBeenCalledWith('alpha-mcp', true, 'a:b', false, 'not_used'),
      { timeout: 5000 },
    )
  })

  it.each([
    ['Kiro Crew\'s own server', { cwd_proven: true }],
    ['a server answered "No"', { cwd_use: 'not_used' }],
  ])('never asks about %s', async (_label, row) => {
    serve(row)
    const setStub = vi.spyOn(api, 'mcpGatewaySetStub').mockResolvedValue({ ok: true, name: 'alpha-mcp', stub: true } as never)

    mount()
    fireEvent.click(await findSwitch())

    await waitFor(() => expect(setStub).toHaveBeenCalledWith('alpha-mcp', true, 'alpha-identity'), { timeout: 5000 })
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('leaves a server waiting for the answer out of the bulk share', () => {
    const rows = [
      server({ name: 'a' }),
      server({ name: 'b', cwd_use: 'not_used' }),
      server({ name: 'c', cwd_proven: true }),
      server({ name: 'd', cwd_use: 'uses_cwd' }),
    ] as McpManagedServer[]
    expect(autoShareCandidates(rows).map(s => s.name)).toEqual(['b', 'c'])
    // A gateway that sends no answer field does not ask, so neither does the page.
    const { cwd_use: _omit, ...older } = server()
    void _omit
    expect(needsCwdAnswer(older as McpManagedServer)).toBe(false)
  })

  it('draws no switch and asks nothing for a server seen asking for roots', async () => {
    serve({ can_stub: false, cwd_roots: true })

    mount()
    await screen.findByText('alpha-mcp', {}, { timeout: 5000 })

    expect(screen.queryByRole('switch', { name: `${M('share_toggle')} alpha-mcp` })).toBeNull()
    expect(screen.queryByRole('dialog')).toBeNull()
    const row = server({ can_stub: false, cwd_roots: true }) as McpManagedServer
    expect(detailsWhyKey(row, rowState(row, true))).toBe('pages.mcpManagement.why_roots')
  })

  it('lets a roots row answer that the server does not use the folder, then shows its switch', async () => {
    serve({ can_stub: false, cwd_roots: true })
    // The refresh after the answer reads the row the gateway now reports.
    vi.mocked(api.mcpGatewayServers)
      .mockResolvedValueOnce({ servers: [server({ can_stub: false, cwd_roots: true })] } as never)
      .mockResolvedValue({ servers: [server({ cwd_use: 'not_used' })] } as never)
    const answer = vi.spyOn(api, 'mcpGatewaySetCwdUse').mockResolvedValue({ ok: true, name: 'alpha-mcp', routing_changed: false } as never)
    const setStub = vi.spyOn(api, 'mcpGatewaySetStub')

    mount()
    await screen.findByText(M('why_roots'), {}, { timeout: 5000 })
    // The row says why once: its Details do not repeat the line.
    fireEvent.click(screen.getByRole('button', { name: M('details_aria', { name: 'alpha-mcp' }) }))
    expect(screen.getAllByText(M('why_roots'))).toHaveLength(1)
    fireEvent.click(screen.getByRole('button', { name: `${M('cwd_roots_not_used')} alpha-mcp` }))

    await waitFor(() => expect(answer).toHaveBeenCalledWith('alpha-mcp', 'not_used'), { timeout: 5000 })
    expect((await findSwitch()).getAttribute('aria-checked')).toBe('false')
    const notice = await screen.findByText('alpha-mcp can now be shared. Turn on Share.')
    expect(notice.getAttribute('role')).toBe('status')
    expect(notice.closest('tr')?.previousElementSibling?.textContent).toContain('alpha-mcp')
    expect(screen.queryByRole('button', { name: `${M('cwd_roots_not_used')} alpha-mcp` })).toBeNull()
    // Only the open Details read the launch; the answer shares nothing.
    expect(api.mcpGatewayLaunchPreview).toHaveBeenCalledTimes(1)
    expect(setStub).not.toHaveBeenCalled()
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it.each([
    ['applied', { applied: true, restart_required: false }, null],
    ['restart required', { applied: false, restart_required: true }, 'stub_restart_required'],
    ['not applied', { applied: false, restart_required: false }, 'stub_not_live'],
  ])('reacts to a roots answer the gateway routed: %s', async (_label, result, message) => {
    serve({ can_stub: false, cwd_roots: true, in_allowlist: true })
    const answer = vi.spyOn(api, 'mcpGatewaySetCwdUse').mockResolvedValue(
      { ok: true, name: 'alpha-mcp', routing_changed: true, ...result } as never,
    )
    const setStub = vi.spyOn(api, 'mcpGatewaySetStub')

    mount()
    fireEvent.click(await screen.findByRole('button', { name: `${M('cwd_roots_not_used')} alpha-mcp` }))

    await waitFor(() => expect(answer).toHaveBeenCalledWith('alpha-mcp', 'not_used'))
    if (message) await screen.findByText(M(message))
    else {
      await waitFor(() => expect(answer).toHaveBeenCalledTimes(1))
      expect(screen.queryByText(M('stub_restart_required'))).toBeNull()
      expect(screen.queryByText(M('stub_not_live'))).toBeNull()
    }
    // The gateway already routed it: nothing else is sent, and no "can now be shared".
    expect(api.mcpGatewayLaunchPreview).not.toHaveBeenCalled()
    expect(setStub).not.toHaveBeenCalled()
    expect(screen.queryByText(M('cwd_roots_share_ready', { name: 'alpha-mcp' }))).toBeNull()
  })

  it('shows and approves the launch when a roots answer needs it, sending the answer with it', async () => {
    serve({ can_stub: false, cwd_roots: true, in_allowlist: true })
    const answer = vi.spyOn(api, 'mcpGatewaySetCwdUse').mockRejectedValue(
      new ApiError(409, 'display the launch before approving it', '{"code":"expected_launch_required"}'),
    )
    const setStub = vi.spyOn(api, 'mcpGatewaySetStub').mockResolvedValue(
      { ok: true, name: 'alpha-mcp', stub: true, applied: false, restart_required: true } as never,
    )

    mount()
    fireEvent.click(await screen.findByRole('button', { name: `${M('cwd_roots_not_used')} alpha-mcp` }))

    await waitFor(() => expect(answer).toHaveBeenCalledWith('alpha-mcp', 'not_used'))
    await waitFor(() => expect(api.mcpGatewayLaunchPreview).toHaveBeenCalledWith('alpha-mcp'))
    await waitFor(() => expect(setStub).toHaveBeenCalledWith('alpha-mcp', true, 'alpha-identity', false, 'not_used'))
    await screen.findByText(M('stub_restart_required'))
    expect(screen.queryByText(M('cwd_roots_share_ready', { name: 'alpha-mcp' }))).toBeNull()
  })

  it('names sharing and the folder answer, and keeps focus while the answer is pending', async () => {
    serve({ can_stub: false, cwd_roots: true })
    let finishAnswer!: () => void
    const answer = vi.spyOn(api, 'mcpGatewaySetCwdUse').mockImplementation(() => new Promise(resolve => {
      finishAnswer = () => resolve({ ok: true, name: 'alpha-mcp', routing_changed: false })
    }))
    mount()
    const button = await screen.findByRole('button', {
      name: "Allow sharing (it doesn't use the folder) alpha-mcp",
    })
    expect(button.className).toContain('rounded-lg')
    expect(button.className).toContain('border')
    button.focus()
    fireEvent.click(button)
    await waitFor(() => expect(answer).toHaveBeenCalledWith('alpha-mcp', 'not_used'))
    try {
      await waitFor(() => expect(button).toHaveAttribute('aria-busy', 'true'))
      expect(button).toHaveAttribute('aria-disabled', 'true')
      expect(button).not.toBeDisabled()
      expect(button).toHaveFocus()
      fireEvent.click(button)
      expect(answer).toHaveBeenCalledTimes(1)
    } finally {
      await act(async () => { finishAnswer() })
    }
    await waitFor(() => expect(button).not.toHaveAttribute('aria-busy'))
  })

  it('names the roots answer button in a sharing refusal', async () => {
    serve({ cwd_use: 'not_used' })
    vi.spyOn(api, 'mcpGatewaySetStub').mockRejectedValue(
      new ApiError(409, 'roots requested', '{"code":"cwd_roots"}'),
    )
    mount()
    fireEvent.click(await findSwitch())
    await screen.findByText(
      "alpha-mcp starts in each chat's folder, so each chat runs its own copy. If it doesn't use the folder, choose “Allow sharing (it doesn't use the folder)”.",
    )
  })

  it.each([
    ['a roots row with no answer', { can_stub: false, cwd_roots: true }, true],
    ['a roots row already answered "No"', { can_stub: false, cwd_roots: true, cwd_use: 'not_used' }, false],
    ['a row that did not ask for roots', {}, false],
  ])('decides whether to offer the "does not use the folder" answer on %s', (_label, over, offered) => {
    expect(offersRootsAnswer(server(over) as McpManagedServer)).toBe(offered)
  })

  it('does not offer the answer on a roots row already answered "No"', async () => {
    serve({ can_stub: false, cwd_roots: true, cwd_use: 'not_used' })

    mount()
    await screen.findByText('alpha-mcp', {}, { timeout: 5000 })

    expect(screen.queryByRole('button', { name: `${M('cwd_roots_not_used')} alpha-mcp` })).toBeNull()
  })
})
