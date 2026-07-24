/**
 * McpCustomServerModal — parse matrix, add/edit flows, consent default,
 * and backend-error surfacing.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import McpCustomServerModal, { parseCustomJson } from '../components/McpCustomServerModal'
import { api, ApiError } from '../api/client'

vi.mock('../api/client', async () => {
  class ApiError extends Error {
    status: number
    constructor(status: number, message: string) {
      super(message)
      this.status = status
    }
  }
  return {
    ApiError,
    api: {
      mcpCustomAdd: vi.fn(),
      mcpCustomGet: vi.fn(),
      mcpCustomUpdate: vi.fn(),
    },
  }
})

const mockedApi = vi.mocked(api)

function renderModal(props: Partial<React.ComponentProps<typeof McpCustomServerModal>> = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <McpCustomServerModal open onClose={vi.fn()} {...props} />
    </QueryClientProvider>,
  )
}

const STDIO = { command: 'npx', args: ['-y', '@acme/weather'], env: { KEY: '' } }

beforeEach(() => {
  vi.clearAllMocks()
})

// ---------------------------------------------------------------------------
// parseCustomJson — normalize matrix
// ---------------------------------------------------------------------------

describe('parseCustomJson', () => {
  it('accepts a full mcpServers block', () => {
    const out = parseCustomJson(JSON.stringify({ mcpServers: { weather: STDIO } }), '')
    expect(out).toMatchObject({ ok: true, servers: { weather: STDIO } })
  })

  it('accepts a bare {name: spec} map', () => {
    const out = parseCustomJson(JSON.stringify({ weather: STDIO, remote: { url: 'https://x.example' } }), '')
    expect(out.ok && Object.keys(out.servers)).toEqual(['weather', 'remote'])
  })

  it('accepts a single bare spec only once a name is provided', () => {
    const text = JSON.stringify(STDIO)
    const withoutName = parseCustomJson(text, '')
    expect(withoutName).toMatchObject({ ok: true, needsName: true, servers: {} })
    const withName = parseCustomJson(text, 'weather')
    expect(withName.ok && withName.servers).toEqual({ weather: STDIO })
  })

  it('rejects invalid JSON, arrays, and empty objects', () => {
    expect(parseCustomJson('not json', '').ok).toBe(false)
    expect(parseCustomJson('[1,2]', '').ok).toBe(false)
    expect(parseCustomJson('{}', '').ok).toBe(false)
    expect(parseCustomJson('{"mcpServers": {}}', '').ok).toBe(false)
    expect(parseCustomJson('{"mcpServers": [1]}', '').ok).toBe(false)
  })
})

// ---------------------------------------------------------------------------
// Add mode
// ---------------------------------------------------------------------------

describe('add mode', () => {
  it('previews parsed servers and posts with enable=false by default', async () => {
    mockedApi.mcpCustomAdd.mockResolvedValue({ ok: true, added: ['weather'], enabled: false })
    const user = userEvent.setup()
    renderModal()

    const textarea = screen.getByLabelText('Servers JSON')
    await user.click(textarea)
    await user.paste(JSON.stringify({ mcpServers: { weather: STDIO } }))

    // Preview shows the server with a one-line command summary.
    expect(await screen.findByText('Will add')).toBeInTheDocument()
    expect(screen.getByText('weather')).toBeInTheDocument()
    expect(screen.getByText('npx -y @acme/weather')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /Add/ }))
    await waitFor(() =>
      expect(mockedApi.mcpCustomAdd).toHaveBeenCalledWith({ weather: STDIO }, false),
    )
  })

  it('threads the Enable immediately checkbox into the request', async () => {
    mockedApi.mcpCustomAdd.mockResolvedValue({ ok: true, added: ['weather'], enabled: true })
    const user = userEvent.setup()
    renderModal()

    const textarea = screen.getByLabelText('Servers JSON')
    await user.click(textarea)
    await user.paste(JSON.stringify({ mcpServers: { weather: STDIO } }))
    await user.click(screen.getByRole('checkbox'))
    await user.click(screen.getByRole('button', { name: /Add/ }))

    await waitFor(() =>
      expect(mockedApi.mcpCustomAdd).toHaveBeenCalledWith({ weather: STDIO }, true),
    )
  })

  it('asks for a name when a single bare spec is pasted', async () => {
    mockedApi.mcpCustomAdd.mockResolvedValue({ ok: true, added: ['weather'], enabled: false })
    const user = userEvent.setup()
    renderModal()

    const textarea = screen.getByLabelText('Servers JSON')
    await user.click(textarea)
    await user.paste(JSON.stringify(STDIO))

    expect(await screen.findByText(/give it a name/)).toBeInTheDocument()
    const addBtn = screen.getByRole('button', { name: /Add/ })
    expect(addBtn).toBeDisabled()

    await user.type(screen.getByLabelText('Server name'), 'weather')
    await waitFor(() => expect(screen.getByRole('button', { name: /Add/ })).toBeEnabled())
    await user.click(screen.getByRole('button', { name: /Add/ }))
    await waitFor(() =>
      expect(mockedApi.mcpCustomAdd).toHaveBeenCalledWith({ weather: STDIO }, false),
    )
  })

  it('shows an inline parse error for invalid JSON and disables Add', async () => {
    const user = userEvent.setup()
    renderModal()
    const textarea = screen.getByLabelText('Servers JSON')
    await user.click(textarea)
    await user.paste('{"mcpServers": nope}')
    expect(await screen.findByRole('alert')).toHaveTextContent(/Not valid JSON/)
    expect(screen.getByRole('button', { name: /Add/ })).toBeDisabled()
  })

  it('surfaces a 409 collision with a friendly message', async () => {
    mockedApi.mcpCustomAdd.mockRejectedValue(new ApiError(409, 'name already in use'))
    const user = userEvent.setup()
    renderModal()
    const textarea = screen.getByLabelText('Servers JSON')
    await user.click(textarea)
    await user.paste(JSON.stringify({ mcpServers: { weather: STDIO } }))
    await user.click(screen.getByRole('button', { name: /Add/ }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/Name already in use/)
  })

  it('surfaces backend 400 validation text inline', async () => {
    mockedApi.mcpCustomAdd.mockRejectedValue(new ApiError(400, "server 'weather': unknown spec key 'cwd'"))
    const user = userEvent.setup()
    renderModal()
    const textarea = screen.getByLabelText('Servers JSON')
    await user.click(textarea)
    await user.paste(JSON.stringify({ mcpServers: { weather: { ...STDIO, cwd: '/tmp' } } }))
    await user.click(screen.getByRole('button', { name: /Add/ }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/unknown spec key 'cwd'/)
  })
})

// ---------------------------------------------------------------------------
// Edit mode
// ---------------------------------------------------------------------------

describe('edit mode', () => {
  it('prefills from the full spec (env included) and PUTs the edited spec', async () => {
    mockedApi.mcpCustomGet.mockResolvedValue({ name: 'weather', spec: STDIO, enabled: false })
    mockedApi.mcpCustomUpdate.mockResolvedValue({ ok: true, name: 'weather' })
    const user = userEvent.setup()
    renderModal({ editName: 'weather' })

    const textarea = await screen.findByLabelText('Server spec JSON')
    await waitFor(() => expect(textarea).toHaveValue(JSON.stringify(STDIO, null, 2)))
    // env made it into the editor — a save cannot silently drop it.
    expect((textarea as HTMLTextAreaElement).value).toContain('"KEY"')

    await user.clear(textarea)
    await user.click(textarea)
    const newSpec = { url: 'https://mcp.example.com/sse' }
    await user.paste(JSON.stringify(newSpec))
    await user.click(screen.getByRole('button', { name: /Save/ }))

    await waitFor(() =>
      expect(mockedApi.mcpCustomUpdate).toHaveBeenCalledWith('weather', newSpec),
    )
    expect(mockedApi.mcpCustomAdd).not.toHaveBeenCalled()
  })

  it('shows the enabled-state preservation note instead of the consent checkbox', async () => {
    mockedApi.mcpCustomGet.mockResolvedValue({ name: 'weather', spec: STDIO, enabled: true })
    renderModal({ editName: 'weather' })
    expect(await screen.findByText(/keeps the server's enabled\/disabled state/)).toBeInTheDocument()
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument()
  })

  it('surfaces a failed spec load', async () => {
    mockedApi.mcpCustomGet.mockRejectedValue(new ApiError(404, "server 'ghost' not found"))
    renderModal({ editName: 'ghost' })
    expect(await screen.findByRole('alert')).toHaveTextContent(/not found/)
  })
})
