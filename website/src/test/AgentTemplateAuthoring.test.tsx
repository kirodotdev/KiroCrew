import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

/* ── Mock api/client BEFORE the components import ── */
const mockApi = vi.hoisted(() => ({
  agentCreate: vi.fn(),
  agentPatch: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))

import AgentTemplateCreateDialog from '../components/AgentTemplateCreateDialog'
import AgentTemplateEditor from '../components/AgentTemplateEditor'

beforeEach(() => {
  mockApi.agentCreate.mockReset()
  mockApi.agentPatch.mockReset()
  mockApi.agentCreate.mockResolvedValue({ ok: true, name: 'researcher' })
  mockApi.agentPatch.mockResolvedValue({ ok: true })
})

function renderDialog(
  props: Partial<React.ComponentProps<typeof AgentTemplateCreateDialog>> = {},
) {
  const onCreated = props.onCreated ?? vi.fn()
  const onClose = props.onClose ?? vi.fn()
  const utils = render(
    <AgentTemplateCreateDialog
      open={props.open ?? true}
      templates={props.templates ?? ['kirocrew', 'base']}
      initialFrom={props.initialFrom}
      onClose={onClose}
      onCreated={onCreated}
    />,
  )
  return { ...utils, onCreated, onClose }
}

describe('AgentTemplateCreateDialog', () => {
  it('creates a blank template from just a name', async () => {
    const { onCreated } = renderDialog()

    fireEvent.change(screen.getByLabelText(/^Name$/i), { target: { value: 'researcher' } })
    fireEvent.click(screen.getByRole('button', { name: /Create template/i }))

    await waitFor(() => expect(mockApi.agentCreate).toHaveBeenCalledWith({ name: 'researcher' }))
    await waitFor(() => expect(onCreated).toHaveBeenCalledWith('researcher'))
  })

  it('never sends tools or an allowlist', async () => {
    // The privilege surface is not this dialog's to set — the endpoint refuses it
    // from a body, and the form must not imply otherwise by sending one.
    renderDialog()

    fireEvent.change(screen.getByLabelText(/^Name$/i), { target: { value: 'researcher' } })
    fireEvent.change(screen.getByLabelText(/System prompt/i), { target: { value: 'Be brief.' } })
    fireEvent.click(screen.getByRole('button', { name: /Create template/i }))

    await waitFor(() => expect(mockApi.agentCreate).toHaveBeenCalled())
    const body = mockApi.agentCreate.mock.calls[0][0]
    expect(body).not.toHaveProperty('tools')
    expect(body).not.toHaveProperty('allowedTools')
    expect(body).not.toHaveProperty('toolsSettings')
  })

  it('omits empty optional fields rather than sending blanks', async () => {
    renderDialog()

    fireEvent.change(screen.getByLabelText(/^Name$/i), { target: { value: 'researcher' } })
    fireEvent.change(screen.getByLabelText(/Description/i), { target: { value: '   ' } })
    fireEvent.click(screen.getByRole('button', { name: /Create template/i }))

    await waitFor(() => expect(mockApi.agentCreate).toHaveBeenCalledWith({ name: 'researcher' }))
  })

  it('refuses to submit without a name', async () => {
    renderDialog()

    fireEvent.click(screen.getByRole('button', { name: /Create template/i }))

    expect(await screen.findByRole('alert')).toHaveTextContent(/Name is required/i)
    expect(mockApi.agentCreate).not.toHaveBeenCalled()
  })

  it('shows the server error verbatim instead of a guess', async () => {
    // Reserved names and names already claimed by a package spec are rules only
    // the server knows; replacing its message would mislead.
    mockApi.agentCreate.mockResolvedValue({ error: "'kirocrew' is reserved" })
    const { onCreated } = renderDialog()

    fireEvent.change(screen.getByLabelText(/^Name$/i), { target: { value: 'kirocrew' } })
    fireEvent.click(screen.getByRole('button', { name: /Create template/i }))

    expect(await screen.findByRole('alert')).toHaveTextContent(/'kirocrew' is reserved/)
    expect(onCreated).not.toHaveBeenCalled()
  })

  it('does not report success when the request rejects', async () => {
    mockApi.agentCreate.mockRejectedValue(new Error('network down'))
    const { onCreated } = renderDialog()

    fireEvent.change(screen.getByLabelText(/^Name$/i), { target: { value: 'researcher' } })
    fireEvent.click(screen.getByRole('button', { name: /Create template/i }))

    expect(await screen.findByRole('alert')).toHaveTextContent(/network down/i)
    expect(onCreated).not.toHaveBeenCalled()
  })

  it('sends the duplicate source and seeds a distinct name', async () => {
    renderDialog({ initialFrom: 'base' })

    // A copy defaulting to the source's own name would 409 on every open.
    const nameField = screen.getByLabelText(/^Name$/i) as HTMLInputElement
    expect(nameField.value).toBe('base-copy')

    fireEvent.click(screen.getByRole('button', { name: /Create template/i }))
    await waitFor(() =>
      expect(mockApi.agentCreate).toHaveBeenCalledWith({ name: 'base-copy', from: 'base' }),
    )
  })

  it('titles itself for the entry point it was opened from', () => {
    const { unmount } = renderDialog({ initialFrom: 'base' })
    expect(screen.getByText(/Duplicate agent template/i)).toBeInTheDocument()
    unmount()

    renderDialog()
    expect(screen.getByText(/New agent template/i)).toBeInTheDocument()
  })
})

function renderEditor(props: Partial<React.ComponentProps<typeof AgentTemplateEditor>> = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const onSaved = props.onSaved ?? vi.fn()
  const utils = render(
    <QueryClientProvider client={qc}>
      <AgentTemplateEditor
        agentName={props.agentName ?? 'researcher'}
        description={props.description ?? 'Digs through papers'}
        prompt={props.prompt ?? 'Be rigorous.'}
        managed={props.managed}
        onSaved={onSaved}
      />
    </QueryClientProvider>,
  )
  return { ...utils, onSaved }
}

describe('AgentTemplateEditor', () => {
  it('saves only the field that changed', async () => {
    const { onSaved } = renderEditor()

    fireEvent.change(screen.getByLabelText(/System prompt/i), { target: { value: 'Be terse.' } })
    fireEvent.click(screen.getByRole('button', { name: /^Save$/i }))

    await waitFor(() =>
      expect(mockApi.agentPatch).toHaveBeenCalledWith('researcher', { prompt: 'Be terse.' }),
    )
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith('researcher', { prompt: 'Be terse.' }))
  })

  it('sends both fields when both changed', async () => {
    renderEditor()

    fireEvent.change(screen.getByLabelText(/Description/i), { target: { value: 'New purpose' } })
    fireEvent.change(screen.getByLabelText(/System prompt/i), { target: { value: 'Be terse.' } })
    fireEvent.click(screen.getByRole('button', { name: /^Save$/i }))

    await waitFor(() =>
      expect(mockApi.agentPatch).toHaveBeenCalledWith('researcher', {
        description: 'New purpose',
        prompt: 'Be terse.',
      }),
    )
  })

  it('keeps Save inert until something is dirty', () => {
    renderEditor()
    expect(screen.getByRole('button', { name: /^Save$/i })).toBeDisabled()
  })

  it('surfaces a rejected save instead of reporting it as done', async () => {
    // A 400 resolves the fetch, so an error read off the body is the only signal
    // — a component that ignores it shows "Saved" over an unwritten file.
    mockApi.agentPatch.mockResolvedValue({ error: 'prompt must be at most 100000 characters' })
    const { onSaved } = renderEditor()

    fireEvent.change(screen.getByLabelText(/System prompt/i), { target: { value: 'x' } })
    fireEvent.click(screen.getByRole('button', { name: /^Save$/i }))

    await waitFor(() => expect(screen.getByText(/at most 100000 characters/i)).toBeInTheDocument())
    expect(onSaved).not.toHaveBeenCalled()
  })

  it('reverts to the server values', async () => {
    renderEditor()
    const prompt = screen.getByLabelText(/System prompt/i) as HTMLTextAreaElement

    fireEvent.change(prompt, { target: { value: 'scratch' } })
    fireEvent.click(screen.getByRole('button', { name: /Revert/i }))

    expect(prompt.value).toBe('Be rigorous.')
    expect(mockApi.agentPatch).not.toHaveBeenCalled()
  })

  it('re-seeds when the selection moves to another template', () => {
    // The other two props are held IDENTICAL and only the agent changes: two
    // templates can legitimately share a description and prompt, and keying the
    // reset on the values alone would let an unsaved edit survive the switch and
    // be written into the WRONG template's spec on the next Save.
    const { rerender } = renderEditor({ description: 'Shared', prompt: 'Shared prompt.' })
    const prompt = () => screen.getByLabelText(/System prompt/i) as HTMLTextAreaElement

    fireEvent.change(prompt(), { target: { value: 'unsaved edit' } })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    rerender(
      <QueryClientProvider client={qc}>
        <AgentTemplateEditor
          agentName="other"
          description="Shared"
          prompt="Shared prompt."
          onSaved={vi.fn()}
        />
      </QueryClientProvider>,
    )

    expect(prompt().value).toBe('Shared prompt.')
  })

  it('offers no write for a managed template', () => {
    renderEditor({ managed: true, prompt: 'file:///home/u/.kiro/crew/prompt.md' })

    expect(screen.queryByRole('button', { name: /^Save$/i })).not.toBeInTheDocument()
    expect(screen.queryByLabelText(/System prompt/i)).not.toBeInTheDocument()
    // The reason is stated, not just the absence — a locked field with no
    // explanation reads as a bug.
    expect(screen.getByText(/rewrites this template on every install/i)).toBeInTheDocument()
  })

  it('shows a managed template its file:// prompt as-is', () => {
    renderEditor({ managed: true, prompt: 'file:///home/u/.kiro/crew/prompt.md' })
    expect(screen.getByText('file:///home/u/.kiro/crew/prompt.md')).toBeInTheDocument()
  })
})
