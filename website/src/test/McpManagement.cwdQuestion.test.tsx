// MCP Management: the working-folder question.
//
// Contract under test:
// - sharing a server that is not Kiro Crew's own and has no "does not use the
//   folder" answer asks first; "No" records the answer and then shares, pinned
//   to the launch read as usual; "Not sure" records "uses the folder" and
//   shares nothing
// - a server shared before the question existed keeps its switch, and its row
//   asks once; "Yes" takes it off sharing in the same request
// - Kiro Crew's own servers and servers already answered "No" are never asked
// - the bulk share never offers a server that waits for the answer
//
// Every label is read from the catalog, so a wording change never breaks a test.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { api, type McpManagedServer } from '../api/client'
import { i18nT } from '../i18n/t'
import { McpManagement } from '../pages/settings/McpManagement'
import { autoShareCandidates, needsCwdAnswer } from '../pages/settings/mcpManagementModel'
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
  it('asks before sharing, and "No" records the answer and then shares', async () => {
    serve({})
    const answer = vi.spyOn(api, 'mcpGatewaySetCwdUse').mockResolvedValue({ ok: true, name: 'alpha-mcp' } as never)
    const setStub = vi.spyOn(api, 'mcpGatewaySetStub').mockResolvedValue({ ok: true, name: 'alpha-mcp', stub: true } as never)

    mount()
    fireEvent.click(await findSwitch())

    const dialog = await screen.findByRole('dialog', {}, { timeout: 5000 })
    expect(dialog.textContent).toContain(M('cwd_question_title', { name: 'alpha-mcp' }))
    expect(dialog.textContent).toContain(M('cwd_question_why'))
    expect(api.mcpGatewayLaunchPreview).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: new RegExp(M('cwd_question_no')) }))

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

  it('"Not sure" keeps the server in each chat and shares nothing', async () => {
    serve({})
    const answer = vi.spyOn(api, 'mcpGatewaySetCwdUse').mockResolvedValue({ ok: true, name: 'alpha-mcp' } as never)
    const setStub = vi.spyOn(api, 'mcpGatewaySetStub')

    mount()
    fireEvent.click(await findSwitch())
    fireEvent.click(await screen.findByRole('button', { name: new RegExp(M('cwd_question_unsure')) }))

    await waitFor(() => expect(answer).toHaveBeenCalledWith('alpha-mcp', 'uses_cwd'), { timeout: 5000 })
    await screen.findByText(M('cwd_kept_per_chat', { name: 'alpha-mcp' }), {}, { timeout: 5000 })
    expect(setStub).not.toHaveBeenCalled()
    expect(api.mcpGatewayLaunchPreview).not.toHaveBeenCalled()
  })

  it('asks once on a server shared before the question, and "Yes" stops sharing it', async () => {
    serve({ stub: true, in_allowlist: true })
    const setStub = vi.spyOn(api, 'mcpGatewaySetStub').mockResolvedValue({ ok: true, name: 'alpha-mcp', stub: false } as never)

    mount()
    expect((await findSwitch()).getAttribute('aria-checked')).toBe('true')
    screen.getByText(M('cwd_unanswered_hint'))
    fireEvent.click(screen.getByRole('button', { name: `${M('cwd_unanswered_answer')} alpha-mcp` }))
    fireEvent.click(await screen.findByRole('button', { name: new RegExp(M('cwd_question_yes')) }))

    await waitFor(
      () => expect(setStub).toHaveBeenCalledWith('alpha-mcp', false, undefined, false, 'uses_cwd'),
      { timeout: 5000 },
    )
  })

  it('asks before approving an updated launch of an unanswered server, and "No" approves with the answer', async () => {
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
    fireEvent.click(await screen.findByRole('button', { name: new RegExp(M('cwd_question_no')) }))

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
})
