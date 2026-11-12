/**
 * EnrollAsBotDialog — "Enroll as bot" on a custom agent. A bot can never be
 * created under the agent's own name (the server answers 409
 * `template_name_in_use`), so the dialog proposes a free name, creates the bot
 * on the built-in worker, and then adds the agent's abilities through the
 * reviewed add.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const mockApi = vi.hoisted(() => ({ createKirocrewAgent: vi.fn(), kirocrewAgents: vi.fn() }))
vi.mock('../../api/client', async (orig) => {
  const real = await orig<typeof import('../../api/client')>()
  return { ...real, api: { ...real.api, ...mockApi } }
})
const mockKit = vi.hoisted(() => ({ get: vi.fn(), preview: vi.fn(), apply: vi.fn() }))
vi.mock('../../api/crewCapabilities', async (orig) => {
  const real = await orig<typeof import('../../api/crewCapabilities')>()
  return { ...real, crewWorkerKitApi: mockKit }
})
// The agent picker as plain buttons, so a test can pick the agent a second time.
vi.mock('../../components/SimpleSelect', () => ({
  default: ({ options, value, onChange, 'aria-label': ariaLabel }: {
    options: string[]; value: string; onChange: (v: string) => void; 'aria-label'?: string
  }) => (
    <div>
      <span role="combobox" aria-label={ariaLabel} aria-expanded={false} aria-controls="enroll-kit-options">{value}</span>
      <div id="enroll-kit-options" role="listbox">
        {options.map(o => (
          <button key={o} type="button" role="option" aria-selected={o === value} onClick={() => onChange(o)}>{o}</button>
        ))}
      </div>
    </div>
  ),
}))

import EnrollAsBotDialog, { freeBotName } from './EnrollAsBotDialog'
import { ApiError } from '../../api/apiError'
import { namedCeiling } from '../../test/namedCeiling'

// Step two sits behind a real chain: the create mutation settles, the pane
// mounts and reads the bot's kit (GET), its readiness effect picks the agent,
// and the preview request answers. On a loaded worker that is more than
// Testing Library's default second, so each wait on it carries this ceiling.
const PREVIEW_CHAIN = () => namedCeiling('ENROLL_PREVIEW_READY', 5000)
/** The abilities preview for the new bot, once that chain has run. */
const previewShown = () => screen.findByTestId('worker-kit-changes', undefined, PREVIEW_CHAIN())

const onCreated = vi.fn()
const onEnrolled = vi.fn()
const onClose = vi.fn()

function renderDialog(agent = 'research') {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <EnrollAsBotDialog agent={agent} agentNames={['research', 'writer']} onCreated={onCreated} onEnrolled={onEnrolled} onClose={onClose} />
    </QueryClientProvider>,
  )
}

/** The name field, once the roster has answered and the suggestion is settled. */
async function nameField(expected: string): Promise<HTMLInputElement> {
  const field = screen.getByLabelText('Bot name') as HTMLInputElement
  await waitFor(() => expect(field.value).toBe(expected))
  return field
}

beforeEach(() => {
  vi.clearAllMocks()
  mockApi.kirocrewAgents.mockResolvedValue({ agents: [], default_agent: '' })
  mockApi.createKirocrewAgent.mockResolvedValue({ ok: true, name: 'research-bot' })
  mockKit.get.mockResolvedValue({ member: 'research-bot', revision: 'r0' })
  mockKit.preview.mockResolvedValue({
    member: 'research-bot', template: 'research', revision: 'rev-1', runs_unasked: false,
    changes: [{ section: 'skills', id: 'notes', change: 'added' }],
  })
  mockKit.apply.mockResolvedValue({ member: 'research-bot', revision: 'r2' })
})

describe('freeBotName', () => {
  it('proposes <agent>-bot, then the first free numbered name', () => {
    expect(freeBotName('research', ['research'])).toBe('research-bot')
    expect(freeBotName('research', ['research', 'Research-Bot'])).toBe('research-bot-2')
    expect(freeBotName('research', ['research-bot', 'research-bot-2'])).toBe('research-bot-3')
  })
})

describe('EnrollAsBotDialog', () => {
  it('creates the bot under a free name on the built-in worker, never under the agent name', async () => {
    renderDialog()
    await nameField('research-bot')
    fireEvent.click(screen.getByTestId('enroll-create'))
    await waitFor(() => expect(mockApi.createKirocrewAgent).toHaveBeenCalledTimes(1))
    const body = mockApi.createKirocrewAgent.mock.calls[0][0] as { name: string; kiro_agent: string }
    expect(body.name).toBe('research-bot')
    expect(body.name).not.toBe('research')
    expect(body.kiro_agent).toBe('kirocrew')
    await waitFor(() => expect(onCreated).toHaveBeenCalledTimes(1))
  })

  it('skips a name a bot already shows', async () => {
    mockApi.kirocrewAgents.mockResolvedValue({ agents: [{ name: 'rb', display_name: 'research-bot' }], default_agent: '' })
    renderDialog()
    await nameField('research-bot-2')
  })

  it('previews the agent abilities for the new bot and writes them only on confirm', async () => {
    renderDialog()
    await nameField('research-bot')
    fireEvent.click(screen.getByTestId('enroll-create'))
    expect(await screen.findByTestId('enroll-created')).toHaveTextContent('research-bot was created')
    await previewShown()
    expect(mockKit.preview).toHaveBeenCalledWith('research-bot', { template: 'research' })
    expect(mockKit.apply).not.toHaveBeenCalled()
    expect(onEnrolled).not.toHaveBeenCalled()
    // Until the add lands, the pane's Add is the one primary action: the
    // footer only offers to leave without it, and never says "Done".
    expect(screen.getByTestId('enroll-skip')).toHaveTextContent('Finish without abilities')
    expect(screen.queryByTestId('enroll-done')).toBeNull()
    fireEvent.click(screen.getByTestId('worker-kit-apply'))
    await waitFor(() => expect(mockKit.apply).toHaveBeenCalledWith('research-bot', { template: 'research', revision: 'rev-1' }))
    await waitFor(() => expect(onEnrolled).toHaveBeenCalledWith('research-bot'))
    expect(await screen.findByTestId('enroll-done')).toHaveTextContent('Done')
    expect(screen.queryByTestId('enroll-skip')).toBeNull()
  })

  it('keeps the dialog open while the add is being written, so a failed add is seen', async () => {
    let failAdd: (e: Error) => void = () => {}
    mockKit.apply.mockReturnValue(new Promise((_resolve, reject) => { failAdd = reject }))
    renderDialog()
    await nameField('research-bot')
    fireEvent.click(screen.getByTestId('enroll-create'))
    await previewShown()
    fireEvent.click(screen.getByTestId('worker-kit-apply'))
    // The write is in flight: the way out is disabled until it reports back.
    await waitFor(() => expect(screen.getByTestId('enroll-skip')).toBeDisabled())
    fireEvent.click(screen.getByTestId('enroll-skip'))
    expect(onClose).not.toHaveBeenCalled()
    failAdd(new Error('The add did not reach the gateway'))
    expect(await screen.findByTestId('worker-kit-error')).toBeInTheDocument()
    expect(onEnrolled).not.toHaveBeenCalled()
    await waitFor(() => expect(screen.getByTestId('enroll-skip')).toBeEnabled())
  })

  it('keeps Done disabled while a second add is being written', async () => {
    renderDialog()
    await nameField('research-bot')
    fireEvent.click(screen.getByTestId('enroll-create'))
    await previewShown()
    fireEvent.click(screen.getByTestId('worker-kit-apply'))
    expect(await screen.findByTestId('enroll-done')).toBeEnabled()
    // The first add landed. The agent is picked again and its add is sent,
    // and this time the write does not come back yet.
    let failAdd: (e: Error) => void = () => {}
    mockKit.apply.mockReturnValue(new Promise((_resolve, reject) => { failAdd = reject }))
    fireEvent.click(screen.getByRole('option', { name: 'research' }))
    await previewShown()
    fireEvent.click(screen.getByTestId('worker-kit-apply'))
    await waitFor(() => expect(screen.getByTestId('enroll-done')).toBeDisabled())
    fireEvent.click(screen.getByTestId('enroll-done'))
    expect(onClose).not.toHaveBeenCalled()
    failAdd(new Error('The add did not reach the gateway'))
    expect(await screen.findByTestId('worker-kit-error')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByTestId('enroll-done')).toBeEnabled())
  })

  it('shows an agent\'s auto-approvals in the preview and grants none before the user confirms', async () => {
    mockKit.preview.mockResolvedValue({
      member: 'research-bot', template: 'research', revision: 'rev-1', runs_unasked: false,
      changes: [
        { section: 'mcpServers', id: 'jira', change: 'added' },
        { section: 'allowedTools', id: '@jira/search', change: 'added' },
      ],
    })
    renderDialog()
    await nameField('research-bot')
    fireEvent.click(screen.getByTestId('enroll-create'))
    const changes = await previewShown()
    // The approval is a row of its own, and the hint says it will run unasked.
    expect(changes.querySelectorAll('[data-testid="worker-kit-change-badge"]')).toHaveLength(2)
    expect(screen.getByTestId('worker-kit-approvals-hint')).toHaveTextContent('will run without asking')
    // Creating the bot wrote no ability: the only write so far is the create.
    expect(mockApi.createKirocrewAgent).toHaveBeenCalledTimes(1)
    expect(mockKit.apply).not.toHaveBeenCalled()
    // Leaving without confirming adds nothing either.
    fireEvent.click(screen.getByTestId('enroll-skip'))
    expect(mockKit.apply).not.toHaveBeenCalled()
    expect(onEnrolled).not.toHaveBeenCalled()
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('says a refused name in words and keeps the field, with no raw 409', async () => {
    const reason = "A template is already named 'research-bot'; give this crew another name."
    mockApi.createKirocrewAgent.mockRejectedValue(
      new ApiError(409, reason, JSON.stringify({ error: reason, code: 'template_name_in_use' })),
    )
    renderDialog()
    await nameField('research-bot')
    fireEvent.click(screen.getByTestId('enroll-create'))
    const notice = await screen.findByTestId('enroll-error')
    expect(notice).toHaveTextContent('That name is already taken by a template. Pick another name for the bot.')
    expect(notice).not.toHaveTextContent('409')
    expect(notice).not.toHaveTextContent('template_name_in_use')
    expect(screen.getByLabelText('Bot name')).toBeInTheDocument()
    expect(onCreated).not.toHaveBeenCalled()
  })

  it('refuses a custom agent name in the field without asking the server', async () => {
    renderDialog()
    const field = await nameField('research-bot')
    fireEvent.change(field, { target: { value: 'Writer' } })
    fireEvent.click(screen.getByTestId('enroll-create'))
    // The form's own check is a hint under the field, never the failed-request notice.
    const hint = await screen.findByTestId('enroll-name-hint')
    expect(hint).toHaveTextContent('already taken by a template')
    expect(field).toHaveAttribute('aria-describedby', hint.id)
    expect(screen.queryByTestId('enroll-error')).toBeNull()
    expect(mockApi.createKirocrewAgent).not.toHaveBeenCalled()
  })

  it('says so when the bots could not be loaded, and still lets the user create', async () => {
    mockApi.kirocrewAgents.mockRejectedValue(new Error('connection lost'))
    renderDialog()
    expect(await screen.findByTestId('enroll-roster-error')).toHaveTextContent('Your bots could not be loaded')
    const field = screen.getByLabelText('Bot name') as HTMLInputElement
    expect(field.value).toBe('research-bot')
    fireEvent.click(screen.getByTestId('enroll-create'))
    await waitFor(() => expect(mockApi.createKirocrewAgent).toHaveBeenCalledTimes(1))
  })
})
