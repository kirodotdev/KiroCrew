import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

/* ── Mock api/client BEFORE the component imports ── */
const mockApi = vi.hoisted(() => ({
  skills: vi.fn(),
  agentCreate: vi.fn(),
}))
const MockApiError = vi.hoisted(() => {
  return class MockApiError extends Error {
    readonly status: number
    readonly body: string
    constructor(status: number, message: string, body = '') {
      super(message)
      this.name = 'ApiError'
      this.status = status
      this.body = body
    }
  }
})
vi.mock('../api/client', () => ({ api: mockApi, ApiError: MockApiError }))

import AgentTemplateCreator from '../components/AgentTemplateCreator'

const CATALOG = [
  { key: 'babysit', name: 'babysit', description: 'Monitor a PR', source: 'kirocrew' },
  { key: 'kiro-user/prepare-pr', name: 'prepare-pr', description: 'Ship a PR', source: 'kiro-user' },
]

function renderCreator(props: Partial<React.ComponentProps<typeof AgentTemplateCreator>> = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const onCreated = props.onCreated ?? vi.fn()
  const onClose = props.onClose ?? vi.fn()
  const utils = render(
    <QueryClientProvider client={qc}>
      <AgentTemplateCreator
        open={props.open ?? true}
        onClose={onClose}
        onCreated={onCreated}
        modelOptions={props.modelOptions ?? ['auto', 'claude-opus']}
        existingNames={props.existingNames ?? ['kirocrew', 'taken']}
        mcpServerNames={props.mcpServerNames ?? ['probe-server']}
      />
    </QueryClientProvider>,
  )
  return { ...utils, onCreated, onClose }
}

beforeEach(() => {
  mockApi.skills.mockReset()
  mockApi.agentCreate.mockReset()
  mockApi.skills.mockResolvedValue(CATALOG)
  mockApi.agentCreate.mockResolvedValue({ ok: true, name: 'my-agent', skills: [] })
})

const nameInput = () => screen.getByLabelText(/^name$/i)
const createBtn = () => screen.getByRole('button', { name: /create template/i })

describe('AgentTemplateCreator', () => {
  it('disables Create until a valid name is entered', async () => {
    renderCreator()
    expect(createBtn()).toBeDisabled()
    fireEvent.change(nameInput(), { target: { value: 'my-agent' } })
    expect(createBtn()).toBeEnabled()
  })

  it('rejects an invalid name client-side with the charset rule', async () => {
    renderCreator()
    fireEvent.change(nameInput(), { target: { value: 'Bad Name' } })
    expect(await screen.findByText(/lowercase letters, digits/i)).toBeInTheDocument()
    expect(createBtn()).toBeDisabled()
  })

  it('refuses a duplicate name before spending a request', async () => {
    renderCreator()
    fireEvent.change(nameInput(), { target: { value: 'taken' } })
    expect(await screen.findByText(/already exists/i)).toBeInTheDocument()
    expect(createBtn()).toBeDisabled()
    expect(mockApi.agentCreate).not.toHaveBeenCalled()
  })

  it('submits the complete draft in one POST and reports the created name', async () => {
    const { onCreated } = renderCreator()
    fireEvent.change(nameInput(), { target: { value: 'my-agent' } })
    fireEvent.change(screen.getByLabelText(/description/i), { target: { value: ' does things ' } })
    fireEvent.change(screen.getByLabelText(/system prompt/i), { target: { value: 'You review.' } })
    // Map a skill from the catalog.
    fireEvent.click(await screen.findByRole('button', { name: 'babysit' }))
    // Add a tool via the chip input.
    fireEvent.change(screen.getByLabelText(/add tool/i), { target: { value: 'fs_read' } })
    fireEvent.click(screen.getByRole('button', { name: /^add$/i }))
    fireEvent.click(createBtn())

    await waitFor(() => expect(mockApi.agentCreate).toHaveBeenCalledTimes(1))
    const body = mockApi.agentCreate.mock.calls[0][0]
    expect(body).toMatchObject({
      name: 'my-agent',
      description: 'does things',
      prompt: 'You review.',
      skills: ['babysit'],
      tools: ['fs_read'],
    })
    // Empty optional sections stay OFF the wire — the written spec carries
    // only what the user authored.
    expect(body).not.toHaveProperty('mcpServers')
    expect(body).not.toHaveProperty('deniedCommands')
    expect(body).not.toHaveProperty('model')
    await waitFor(() => expect(onCreated).toHaveBeenCalledWith('my-agent'))
  })

  it('toggling the shield marks a tool auto-approved (allowedTools)', async () => {
    renderCreator()
    fireEvent.change(nameInput(), { target: { value: 'my-agent' } })
    fireEvent.change(screen.getByLabelText(/add tool/i), { target: { value: 'fs_read' } })
    fireEvent.click(screen.getByRole('button', { name: /^add$/i }))
    fireEvent.click(screen.getByRole('button', { name: /toggle auto-approve for fs_read/i }))
    fireEvent.click(createBtn())
    await waitFor(() => expect(mockApi.agentCreate).toHaveBeenCalled())
    expect(mockApi.agentCreate.mock.calls[0][0]).toMatchObject({ allowedTools: ['fs_read'] })
  })

  it('removing a tool also drops it from allowedTools', async () => {
    renderCreator()
    fireEvent.change(nameInput(), { target: { value: 'my-agent' } })
    fireEvent.change(screen.getByLabelText(/add tool/i), { target: { value: 'fs_read' } })
    fireEvent.click(screen.getByRole('button', { name: /^add$/i }))
    fireEvent.click(screen.getByRole('button', { name: /toggle auto-approve for fs_read/i }))
    fireEvent.click(screen.getByRole('button', { name: /remove tool fs_read/i }))
    fireEvent.click(createBtn())
    await waitFor(() => expect(mockApi.agentCreate).toHaveBeenCalled())
    const body = mockApi.agentCreate.mock.calls[0][0]
    expect(body).not.toHaveProperty('tools')
    expect(body).not.toHaveProperty('allowedTools')
  })

  it('builds mcpServers from structured rows with space-split args', async () => {
    renderCreator()
    fireEvent.change(nameInput(), { target: { value: 'my-agent' } })
    fireEvent.click(screen.getByRole('button', { name: /add server/i }))
    fireEvent.change(screen.getByLabelText(/server name/i), { target: { value: 'sdpm' } })
    fireEvent.change(screen.getByLabelText(/^command$/i), { target: { value: 'npx' } })
    fireEvent.change(screen.getByLabelText(/arguments/i), { target: { value: '-y sdpm' } })
    fireEvent.click(createBtn())
    await waitFor(() => expect(mockApi.agentCreate).toHaveBeenCalled())
    expect(mockApi.agentCreate.mock.calls[0][0]).toMatchObject({
      mcpServers: { sdpm: { command: 'npx', args: ['-y', 'sdpm'] } },
    })
  })

  it('honors quotes in args so paths with spaces stay one token', async () => {
    renderCreator()
    fireEvent.change(nameInput(), { target: { value: 'my-agent' } })
    fireEvent.click(screen.getByRole('button', { name: /add server/i }))
    fireEvent.change(screen.getByLabelText(/server name/i), { target: { value: 's' } })
    fireEvent.change(screen.getByLabelText(/^command$/i), { target: { value: 'run' } })
    fireEvent.change(screen.getByLabelText(/arguments/i), { target: { value: '--path "/my dir" -v' } })
    fireEvent.click(createBtn())
    await waitFor(() => expect(mockApi.agentCreate).toHaveBeenCalled())
    expect(mockApi.agentCreate.mock.calls[0][0]).toMatchObject({
      mcpServers: { s: { command: 'run', args: ['--path', '/my dir', '-v'] } },
    })
  })

  it('refuses a named MCP row without a command client-side', async () => {
    renderCreator()
    fireEvent.change(nameInput(), { target: { value: 'my-agent' } })
    fireEvent.click(screen.getByRole('button', { name: /add server/i }))
    fireEvent.change(screen.getByLabelText(/server name/i), { target: { value: 'sdpm' } })
    fireEvent.click(createBtn())
    expect(await screen.findByText(/needs a command/i)).toBeInTheDocument()
    expect(mockApi.agentCreate).not.toHaveBeenCalled()
  })

  it('refuses an MCP row with a command but no name (silent-drop guard)', async () => {
    // The payload builder skips unnamed rows; without this guard the template
    // would be created WITHOUT the server the user just defined, silently.
    renderCreator()
    fireEvent.change(nameInput(), { target: { value: 'my-agent' } })
    fireEvent.click(screen.getByRole('button', { name: /add server/i }))
    fireEvent.change(screen.getByLabelText(/^command$/i), { target: { value: 'npx some-server' } })
    fireEvent.click(createBtn())
    expect(await screen.findByText(/needs a name/i)).toBeInTheDocument()
    expect(mockApi.agentCreate).not.toHaveBeenCalled()
  })

  it('normalizes uppercase name input to lowercase instead of erroring', async () => {
    renderCreator()
    fireEvent.change(nameInput(), { target: { value: 'Code-Reviewer' } })
    expect((nameInput() as HTMLInputElement).value).toBe('code-reviewer')
    expect(createBtn()).toBeEnabled()
  })

  it('refuses duplicate MCP server names (later row would silently win)', async () => {
    renderCreator()
    fireEvent.change(nameInput(), { target: { value: 'my-agent' } })
    fireEvent.click(screen.getByRole('button', { name: /add server/i }))
    fireEvent.click(screen.getByRole('button', { name: /add server/i }))
    const names = screen.getAllByLabelText(/server name/i)
    const commands = screen.getAllByLabelText(/^command$/i)
    fireEvent.change(names[0], { target: { value: 'dup' } })
    fireEvent.change(commands[0], { target: { value: 'cmd-one' } })
    fireEvent.change(names[1], { target: { value: 'dup' } })
    fireEvent.change(commands[1], { target: { value: 'cmd-two' } })
    fireEvent.click(createBtn())
    expect(await screen.findByText(/must be unique/i)).toBeInTheDocument()
    expect(mockApi.agentCreate).not.toHaveBeenCalled()
  })

  it('rejects an invalid tool ref at Add time with the charset rule', async () => {
    renderCreator()
    fireEvent.change(nameInput(), { target: { value: 'my-agent' } })
    fireEvent.change(screen.getByLabelText(/add tool/i), { target: { value: 'bad tool!' } })
    fireEvent.click(screen.getByRole('button', { name: /^add$/i }))
    expect(await screen.findByText(/letters, digits/i)).toBeInTheDocument()
    expect(mockApi.agentCreate).not.toHaveBeenCalled()
  })

  it('pins the client-side regexes to the backend literals (drift guard)', async () => {
    // NAME_RE / TOOL_REF_RE mirror _TEMPLATE_NAME_RE / _TOOL_REF_RE in
    // agents.py by copy; silent drift would accept input the server rejects
    // (or vice versa) with no test failing. Read the backend source and pin.
    const fs = await import('node:fs')
    const path = await import('node:path')
    const backend = fs.readFileSync(
      path.resolve(__dirname, '../../../src/kiro_crew/dashboard/handlers/agents.py'),
      'utf8',
    )
    const nameMatch = backend.match(/_TEMPLATE_NAME_RE = re\.compile\(r"(.+?)"\)/)
    const toolMatch = backend.match(/_TOOL_REF_RE = re\.compile\(r"(.+?)"\)/)
    expect(nameMatch?.[1]).toBe('^[a-z0-9][a-z0-9._-]{0,63}$')
    expect(toolMatch?.[1]).toBe('^@?[A-Za-z0-9][A-Za-z0-9._:/@-]{0,199}$')
    // And the client constants must be those same patterns.
    const client = fs.readFileSync(
      path.resolve(__dirname, '../components/AgentTemplateCreator.tsx'),
      'utf8',
    )
    expect(client).toContain('const NAME_RE = /^[a-z0-9][a-z0-9._-]{0,63}$/')
    expect(client).toContain('const TOOL_REF_RE = /^@?[A-Za-z0-9][A-Za-z0-9._:/@-]{0,199}$/')
  })

  it('Cancel preserves the draft (no reset), matching Escape behavior', async () => {
    const { onClose } = renderCreator()
    fireEvent.change(screen.getByLabelText(/system prompt/i), { target: { value: 'long draft' } })
    fireEvent.click(screen.getByRole('button', { name: /cancel/i }))
    expect(onClose).toHaveBeenCalled()
    // Component state survives the close — the field still holds the draft.
    expect(screen.getByLabelText(/system prompt/i)).toHaveValue('long draft')
  })

  it('granted tools show a visible auto badge, not tint alone', async () => {
    renderCreator()
    fireEvent.change(nameInput(), { target: { value: 'my-agent' } })
    fireEvent.change(screen.getByLabelText(/add tool/i), { target: { value: 'fs_read' } })
    fireEvent.click(screen.getByRole('button', { name: /^add$/i }))
    const shield = screen.getByRole('button', { name: /toggle auto-approve for fs_read/i })
    expect(shield).not.toHaveTextContent(/auto/i)
    fireEvent.click(shield)
    expect(shield).toHaveTextContent(/auto/i)
  })

  it('maps a server field rejection to the named field and keeps the draft', async () => {
    mockApi.agentCreate.mockRejectedValue(
      new MockApiError(400, 'prompt too long', JSON.stringify({
        error: 'prompt must be a string of at most 100000 characters',
        code: 'field_invalid',
        field: 'prompt',
      })),
    )
    const { onCreated } = renderCreator()
    fireEvent.change(nameInput(), { target: { value: 'my-agent' } })
    fireEvent.change(screen.getByLabelText(/system prompt/i), { target: { value: 'draft text' } })
    fireEvent.click(createBtn())
    expect(await screen.findByText(/at most 100000 characters/i)).toBeInTheDocument()
    // The draft survives the rejection.
    expect(screen.getByLabelText(/system prompt/i)).toHaveValue('draft text')
    expect((nameInput() as HTMLInputElement).value).toBe('my-agent')
    expect(onCreated).not.toHaveBeenCalled()
  })

  it('shows a 409 name conflict from the server on the name field', async () => {
    mockApi.agentCreate.mockRejectedValue(
      new MockApiError(409, 'exists', JSON.stringify({
        error: "agent 'racer' already exists", code: 'name_exists', field: 'name',
      })),
    )
    renderCreator({ existingNames: [] })
    fireEvent.change(nameInput(), { target: { value: 'racer' } })
    fireEvent.click(createBtn())
    // Known machine codes render the LOCALIZED message, not the raw English
    // backend prose — name_exists maps to the same string the client-side
    // duplicate check uses.
    expect(await screen.findByText(/already exists/i)).toBeInTheDocument()
  })

  it('offers probed MCP servers as @mount suggestions', async () => {
    renderCreator({ mcpServerNames: ['probe-server'] })
    fireEvent.change(nameInput(), { target: { value: 'my-agent' } })
    fireEvent.click(screen.getByRole('button', { name: /\+ @probe-server/i }))
    fireEvent.click(createBtn())
    await waitFor(() => expect(mockApi.agentCreate).toHaveBeenCalled())
    expect(mockApi.agentCreate.mock.calls[0][0]).toMatchObject({ tools: ['@probe-server'] })
  })
})
