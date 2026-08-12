/**
 * Agent template editor — what the submit payload is allowed to contain.
 *
 * The dialog only models a subset of a kiro-cli agent spec, and the backend
 * replaces a key the request carries while preserving one it omits. Those two
 * facts together mean any field the dialog sends WITHOUT an authoritative value
 * is a silent erase of stored configuration. Three review rounds produced three
 * instances of that same bug, so these tests pin the invariant itself rather than
 * the instances: a field is sent only when the loaded record carried it or the
 * user authored it.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const mockApi = vi.hoisted(() => ({
  skills: vi.fn(),
  agentCreate: vi.fn(),
  agentUpdate: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))

import AgentTemplateCreator from '../components/AgentTemplateCreator'

function renderDialog(props: Record<string, unknown> = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <AgentTemplateCreator
        open
        onClose={() => {}}
        onCreated={() => {}}
        modelOptions={[{ name: 'auto' }]}
        existingNames={[]}
        mcpServerNames={[]}
        {...props}
      />
    </QueryClientProvider>,
  )
}

/** The full record the detail endpoint returns. */
const FULL = {
  name: 'researcher',
  description: 'digs through sources',
  model: 'auto',
  prompt: 'You research.',
  skills: ['web-browse'],
  tools: ['fs_read'],
  allowedTools: [],
  mcpServers: { fetch: { command: 'uvx', args: ['mcp-server-fetch'] } },
}

/** What a LIST row carries when the detail fetch failed: no prompt, no tools,
 *  no mcpServers. Copying these into state makes them look merely empty. */
const LIST_ONLY = { name: 'researcher', description: 'digs through sources' }

beforeEach(() => {
  vi.clearAllMocks()
  mockApi.skills.mockResolvedValue([])
  mockApi.agentUpdate.mockResolvedValue({ ok: true })
  mockApi.agentCreate.mockResolvedValue({ ok: true })
})

async function submit() {
  fireEvent.click(screen.getByRole('button', { name: /save changes|create/i }))
  await waitFor(() => {
    expect(mockApi.agentUpdate.mock.calls.length + mockApi.agentCreate.mock.calls.length)
      .toBeGreaterThan(0)
  })
}

describe('edit payload omits fields it never loaded', () => {
  it('does not send prompt, tools or mcpServers when the record lacked them', async () => {
    renderDialog({ editTarget: LIST_ONLY })
    await submit()

    const payload = mockApi.agentUpdate.mock.calls[0][1] as Record<string, unknown>
    // Absent from the body => the backend preserves whatever is stored.
    expect(payload).not.toHaveProperty('prompt')
    expect(payload).not.toHaveProperty('tools')
    expect(payload).not.toHaveProperty('mcpServers')
    expect(payload).not.toHaveProperty('skills')
  })

  it('still sends a loaded field the user cleared, so editing works', async () => {
    renderDialog({ editTarget: FULL })

    const prompt = screen.getByPlaceholderText(/custom system prompt/i)
    fireEvent.change(prompt, { target: { value: '' } })
    await submit()

    const payload = mockApi.agentUpdate.mock.calls[0][1] as Record<string, unknown>
    // Loaded => present, even when emptied. This is how a deliberate clear is
    // expressed, and it is what distinguishes a clear from a failed load.
    expect(payload).toHaveProperty('prompt', '')
  })

  it('sends a field the user authored even though it was never loaded', async () => {
    renderDialog({ editTarget: LIST_ONLY })

    fireEvent.change(screen.getByPlaceholderText(/custom system prompt/i), {
      target: { value: 'authored by the user' },
    })
    await submit()

    const payload = mockApi.agentUpdate.mock.calls[0][1] as Record<string, unknown>
    expect(payload).toHaveProperty('prompt', 'authored by the user')
  })

  it('omits an untouched mcpServers block rather than echoing it back', async () => {
    // Previously this asserted the block was re-sent VERBATIM, which preserved the
    // unmodelled `command` field but also meant an untouched edit overwrote whatever
    // a concurrent external edit had written to it. Omitting it preserves the same
    // fields through absent-means-preserve, with nothing to clobber. The verbatim
    // round trip is still asserted below for the case where the user DOES edit args.
    renderDialog({ editTarget: FULL })
    await submit()

    const payload = mockApi.agentUpdate.mock.calls[0][1] as Record<string, unknown>
    expect('mcpServers' in payload).toBe(false)
  })

  it('sends the unmodelled MCP field verbatim when the block IS edited', async () => {
    renderDialog({ editTarget: FULL })
    // Add a server so mcpServers counts as changed and is submitted; the untouched
    // `fetch` entry must still carry the `command` the dialog never renders.
    fireEvent.change(screen.getByPlaceholderText(/server name…/i), {
      target: { value: 'extra' },
    })
    fireEvent.change(screen.getByLabelText(/server command or url/i), {
      target: { value: 'npx' },
    })
    fireEvent.click(screen.getByRole('button', { name: /add mcp server/i }))
    await submit()

    const payload = mockApi.agentUpdate.mock.calls[0][1] as Record<string, unknown>
    const servers = payload.mcpServers as Record<string, Record<string, unknown>>
    // command is not rendered by the dialog but must survive the round trip.
    expect(servers.fetch.command).toBe('uvx')
  })

  it('preserves an argument containing a space when the block is edited', async () => {
    // The args field is a space-joined string and submit re-splits on whitespace,
    // so an argument with a space inside would come back as two arguments and the
    // server would receive a different argv. The loaded array is therefore sent
    // verbatim unless the args field itself was edited -- asserted here on a
    // submission that DOES send the block, since an untouched one is now omitted.
    const WITH_SPACED_ARG = {
      ...FULL,
      mcpServers: {
        fetch: { command: 'uvx', args: ['--header', 'User Agent', '--quiet'] },
      },
    }
    renderDialog({ editTarget: WITH_SPACED_ARG })
    // Add a second server so mcpServers counts as changed and is submitted; the
    // untouched `fetch` entry must still carry its original argv.
    fireEvent.change(screen.getByPlaceholderText(/server name…/i), {
      target: { value: 'extra' },
    })
    fireEvent.change(screen.getByLabelText(/server command or url/i), {
      target: { value: 'npx' },
    })
    fireEvent.click(screen.getByRole('button', { name: /add mcp server/i }))
    await submit()

    const payload = mockApi.agentUpdate.mock.calls[0][1] as Record<string, unknown>
    const servers = payload.mcpServers as Record<string, Record<string, unknown>>
    expect(servers.fetch.args).toEqual(['--header', 'User Agent', '--quiet'])
  })
})

describe('a clone carries what the create endpoint can express', () => {
  it('sends resources so a cloned template keeps its steering globs', async () => {
    const WITH_RESOURCES = { ...FULL, resources: ['file://.kiro/steering/**/*.md'] }
    renderDialog({ editTarget: WITH_RESOURCES, cloneMode: true })

    fireEvent.change(screen.getByPlaceholderText('my-agent'), { target: { value: 'copy' } })
    await submit()

    const payload = mockApi.agentCreate.mock.calls[0][0] as Record<string, unknown>
    // A clone is a POST: there is nothing on disk to preserve it against, so the
    // dialog must send it explicitly or the copy silently reads no steering files.
    expect(payload.resources).toEqual(['file://.kiro/steering/**/*.md'])
  })

  it('warns when the source sets keys a clone cannot reproduce', async () => {
    renderDialog({
      editTarget: { ...FULL, hooks: { postToolUse: [] }, includeMcpJson: true },
      cloneMode: true,
    })

    // The create endpoint builds the spec from an allowlist that excludes these,
    // so the copy WILL differ -- say so before the user saves it.
    const warning = await screen.findByText(/cannot copy/i)
    expect(warning.textContent).toMatch(/hooks/)
    expect(warning.textContent).toMatch(/includeMcpJson/)
  })
})

describe('an MCP entry with nothing launchable is refused', () => {
  it('blocks submit and explains, rather than persisting a husk', async () => {
    renderDialog({ mcpServerNames: ['configured-server'] })

    // The template-name field, addressed by its label rather than a loose
    // placeholder regex -- "Tool name…" and "Server name…" also end in "name…".
    fireEvent.change(screen.getByPlaceholderText('my-agent'), { target: { value: 'my-template' } })
    fireEvent.change(screen.getByPlaceholderText(/server name…/i), { target: { value: 'bare' } })
    fireEvent.click(screen.getByRole('button', { name: /add mcp server/i }))

    fireEvent.click(screen.getByRole('button', { name: /create/i }))

    // kiro-cli rejects a server with no command or url, and an invalid spec falls
    // the session back to the default agent -- so nothing is written at all.
    await waitFor(() => {
      expect(screen.getByRole('alert')).toBeTruthy()
    })
    expect(mockApi.agentCreate).not.toHaveBeenCalled()
  })
})

describe('a hand-added MCP server carries a launchable transport', () => {
  // The backend refuses an entry with neither `command` nor `url` (an unlaunchable
  // husk makes kiro-cli reject the whole spec). The dialog collected only a name
  // and args, so every hand-added row was dropped with an error the user could not
  // act on -- the Add button could not produce a saveable server at all.
  it('sends the command the user typed', async () => {
    renderDialog()
    fireEvent.change(screen.getByPlaceholderText('my-agent'), { target: { value: 'my-agent' } })
    fireEvent.change(
      screen.getByPlaceholderText(/server name…/i),
      { target: { value: 'fetch' } },
    )
    fireEvent.change(
      screen.getByLabelText(/server command or url/i),
      { target: { value: 'uvx' } },
    )
    fireEvent.change(
      screen.getByLabelText(/server arguments/i),
      { target: { value: 'mcp-server-fetch' } },
    )
    fireEvent.click(screen.getByRole('button', { name: /add mcp server/i }))
    await submit()

    const body = mockApi.agentCreate.mock.calls[0][0] as Record<string, unknown>
    const servers = body.mcpServers as Record<string, Record<string, unknown>>
    expect(servers.fetch.command).toBe('uvx')
    expect(servers.fetch.args).toEqual(['mcp-server-fetch'])
  })

  it('routes an http value to url rather than command', async () => {
    renderDialog()
    fireEvent.change(screen.getByPlaceholderText('my-agent'), { target: { value: 'my-agent' } })
    fireEvent.change(
      screen.getByPlaceholderText(/server name…/i),
      { target: { value: 'remote' } },
    )
    fireEvent.change(
      screen.getByLabelText(/server command or url/i),
      { target: { value: 'https://mcp.example.com/sse' } },
    )
    fireEvent.click(screen.getByRole('button', { name: /add mcp server/i }))
    await submit()

    const body = mockApi.agentCreate.mock.calls[0][0] as Record<string, unknown>
    const servers = body.mcpServers as Record<string, Record<string, unknown>>
    expect(servers.remote.url).toBe('https://mcp.example.com/sse')
    expect(servers.remote.command).toBeUndefined()
  })

  it('submits a row typed but never added, transport included', async () => {
    // The name input still holds a pending row when the user clicks Create
    // without pressing Add; it must carry the transport too.
    renderDialog()
    fireEvent.change(screen.getByPlaceholderText('my-agent'), { target: { value: 'my-agent' } })
    fireEvent.change(
      screen.getByPlaceholderText(/server name…/i),
      { target: { value: 'pending' } },
    )
    fireEvent.change(
      screen.getByLabelText(/server command or url/i),
      { target: { value: 'npx' } },
    )
    await submit()

    const body = mockApi.agentCreate.mock.calls[0][0] as Record<string, unknown>
    const servers = body.mcpServers as Record<string, Record<string, unknown>>
    expect(servers.pending.command).toBe('npx')
  })
})

describe('draft state does not leak between opens or between fields', () => {
  it('clears the MCP command when the dialog is reset', async () => {
    // The command field was added with the transport fix but left out of the
    // dialog-open reset. Unmounting and re-rendering would NOT catch that (fresh
    // useState is empty regardless), so the SAME mounted component is re-driven
    // through the reset effect by changing `editTarget`, which is one of its deps.
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const props = {
      open: true,
      onClose: () => {},
      onCreated: () => {},
      modelOptions: [{ name: 'auto' }],
      existingNames: [],
      mcpServerNames: [],
    }
    const { rerender } = render(
      <QueryClientProvider client={qc}>
        <AgentTemplateCreator {...props} />
      </QueryClientProvider>,
    )
    fireEvent.change(
      screen.getByLabelText(/server command or url/i),
      { target: { value: 'leaked-command' } },
    )

    rerender(
      <QueryClientProvider client={qc}>
        <AgentTemplateCreator {...props} editTarget={LIST_ONLY} />
      </QueryClientProvider>,
    )

    expect((screen.getByLabelText(/server command or url/i) as HTMLInputElement).value)
      .toBe('')
  })

  it('drops the auto-approve grant when its tool is removed', async () => {
    // allowedTools IS the auto-approve list, so an entry left behind is a live
    // privilege: remove an approved tool, re-add it, and confirmation is skipped
    // for it again without the user ever re-granting.
    renderDialog()
    fireEvent.change(screen.getByPlaceholderText('my-agent'), { target: { value: 'a' } })
    fireEvent.change(screen.getByPlaceholderText(/tool name/i), {
      target: { value: 'fs_write' },
    })
    fireEvent.click(screen.getByRole('button', { name: /add tool/i }))
    fireEvent.click(screen.getByLabelText(/toggle auto-approve for fs_write/i))
    fireEvent.click(screen.getByLabelText(/remove tool fs_write/i))

    // Re-add the same tool; it must come back WITHOUT the old grant.
    fireEvent.change(screen.getByPlaceholderText(/tool name/i), {
      target: { value: 'fs_write' },
    })
    fireEvent.click(screen.getByRole('button', { name: /add tool/i }))
    await submit()

    const body = mockApi.agentCreate.mock.calls[0][0] as Record<string, unknown>
    expect(body.tools).toContain('fs_write')
    expect(body.allowedTools ?? []).not.toContain('fs_write')
  })
})

describe('an edit submits only what the user changed', () => {
  // Re-sending a field the dialog merely displayed replaces whatever a concurrent
  // external edit wrote to it: the backend cannot tell a stale echo of the loaded
  // value from a deliberate write, so every unchanged field it receives is an
  // overwrite it must honour. Omitting them lets absent-means-preserve keep the
  // external edit.
  it('omits every field when nothing was touched', async () => {
    renderDialog({ editTarget: FULL })
    await submit()

    const payload = mockApi.agentUpdate.mock.calls[0][1] as Record<string, unknown>
    // `name` is always present (it identifies the target), nothing else should be.
    expect(Object.keys(payload).sort()).toEqual(['name'])
  })

  it('sends only the field that changed', async () => {
    renderDialog({ editTarget: FULL })
    fireEvent.change(screen.getByPlaceholderText(/what this agent template does/i), {
      target: { value: 'a new description' },
    })
    await submit()

    const payload = mockApi.agentUpdate.mock.calls[0][1] as Record<string, unknown>
    expect(payload.description).toBe('a new description')
    expect('prompt' in payload).toBe(false)
    expect('tools' in payload).toBe(false)
    expect('mcpServers' in payload).toBe(false)
  })

  it('sends a cleared field, because clearing IS a change', async () => {
    // The distinction that makes this safe: an empty value is only sent when it
    // differs from what loaded. A never-loaded empty field stays omitted.
    renderDialog({ editTarget: FULL })
    fireEvent.change(screen.getByPlaceholderText(/custom system prompt/i), {
      target: { value: '' },
    })
    await submit()

    const payload = mockApi.agentUpdate.mock.calls[0][1] as Record<string, unknown>
    expect(payload.prompt).toBe('')
    expect('description' in payload).toBe(false)
  })
})
