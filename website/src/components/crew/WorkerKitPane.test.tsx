/**
 * WorkerKitPane — the "Add abilities" flow: pick an agent, see what adding its
 * tools / MCP servers / skills would do (net-new added, already-present
 * skipped), confirm, and only then write. Behavior directions replace the
 * file's single prompt slot; an agent without directions leaves it alone.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const mockKit = vi.hoisted(() => ({ get: vi.fn(), preview: vi.fn(), apply: vi.fn() }))
vi.mock('../../api/crewCapabilities', async (orig) => {
  const real = await orig<typeof import('../../api/crewCapabilities')>()
  return { ...real, crewWorkerKitApi: mockKit }
})
vi.mock('../SimpleSelect', () => ({
  default: ({ options, value, onChange, 'aria-label': ariaLabel }: {
    options: string[]; value: string; onChange: (v: string) => void; 'aria-label'?: string
  }) => (
    <div>
      <span role="combobox" aria-label={ariaLabel} aria-expanded={false} aria-controls="kit-options">{value}</span>
      <div id="kit-options" role="listbox">
        {options.map(o => (
          <button key={o} type="button" role="option" aria-selected={o === value} onClick={() => onChange(o)}>{o}</button>
        ))}
      </div>
    </div>
  ),
}))

import WorkerKitPane from './WorkerKitPane'
import { ApiError } from '../../api/apiError'

const PREVIEW = {
  member: 'pixel',
  template: 'release-bot',
  revision: 'rev-1',
  runs_unasked: false,
  changes: [
    { section: 'mcpServers', id: 'jira', change: 'added' },
    { section: 'skills', id: 'notes', change: 'added' },
  ],
}

function renderPane(onOpenApprovals?: () => void) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <WorkerKitPane member="pixel" name="Pixel" templates={['kirocrew', 'release-bot']} onOpenApprovals={onOpenApprovals} />
    </QueryClientProvider>,
  )
}

/** Pick an agent and wait for the auto-preview. */
async function previewAdd(template = 'release-bot') {
  await screen.findByRole('option', { name: template })
  fireEvent.click(screen.getByRole('option', { name: template }))
  return screen.findByTestId('worker-kit-changes')
}

beforeEach(() => {
  vi.clearAllMocks()
  mockKit.get.mockResolvedValue({ member: 'pixel', revision: 'r0' })
  mockKit.preview.mockResolvedValue(PREVIEW)
  mockKit.apply.mockResolvedValue({ member: 'pixel', revision: 'r2' })
})

describe('WorkerKitPane', () => {
  it('names the crewmate by its display name', async () => {
    renderPane()
    expect(await screen.findByText(/Copy the tools, MCP servers and skills from another agent into Pixel/)).toBeInTheDocument()
    expect(screen.queryByText(/into pixel\./)).toBeNull()
  })

  it('previews first and writes only on Add, with the preview revision and no keep flag', async () => {
    renderPane()
    const changes = await previewAdd()
    expect(changes).toHaveTextContent('Adds 2 from release-bot')
    expect(changes).toHaveTextContent('jira')
    expect(mockKit.preview).toHaveBeenCalledWith('pixel', { template: 'release-bot' })
    expect(mockKit.apply).not.toHaveBeenCalled()
    fireEvent.click(screen.getByTestId('worker-kit-apply'))
    await waitFor(() => expect(mockKit.apply).toHaveBeenCalledWith('pixel', {
      template: 'release-bot', revision: 'rev-1',
    }))
  })

  it('shows the behavior directions it writes first, and says when they replace the current ones', async () => {
    mockKit.preview.mockResolvedValue({
      ...PREVIEW,
      directions: 'Release SOP: tag, build, announce.',
      changes: [
        { section: 'prompt', id: 'prompt', change: 'replaced' },
        { section: 'mcpServers', id: 'jira', change: 'added' },
      ],
    })
    renderPane()
    const changes = await previewAdd()
    expect(within(changes).getByTestId('worker-kit-group-directions')).toHaveTextContent('Behavior directions')
    expect(within(changes).getByTestId('worker-kit-directions')).toHaveTextContent('Release SOP: tag, build, announce.')
    const badges = within(changes).getAllByTestId('worker-kit-change-badge')
    expect(badges[0]).toHaveTextContent('Replaces current')
    expect(badges[0]).toHaveAttribute('data-change', 'replaced')
  })

  it('shows the directions a replacement overwrites and counts it apart from the adds', async () => {
    mockKit.preview.mockResolvedValue({
      ...PREVIEW,
      directions: 'Release SOP: tag, build, announce.',
      current_directions: 'Old',
      changes: [
        { section: 'prompt', id: 'prompt', change: 'replaced' },
        { section: 'mcpServers', id: 'jira', change: 'added' },
      ],
    })
    renderPane()
    const changes = await previewAdd()
    expect(changes).toHaveTextContent('Replaces behavior directions and adds 1 from release-bot')
    const current = within(changes).getByTestId('worker-kit-current-directions')
    expect(current).toHaveTextContent('Current directions (will be replaced): Old')
    expect(current).toHaveAttribute('title', 'Old')
    const button = screen.getByTestId('worker-kit-apply')
    expect(button).toHaveTextContent('Replace directions and add 1')
    fireEvent.click(button)
    const done = await screen.findByTestId('worker-kit-done')
    expect(done).toHaveTextContent('Added 1 to Pixel. Behavior directions replaced.')
  })

  it('offers a replacement alone as Replace directions, not as nothing new', async () => {
    mockKit.preview.mockResolvedValue({
      ...PREVIEW,
      directions: 'New SOP.',
      current_directions: 'Old',
      changes: [
        { section: 'prompt', id: 'prompt', change: 'replaced' },
        { section: 'mcpServers', id: 'jira', change: 'skipped' },
      ],
    })
    renderPane()
    const changes = await previewAdd()
    expect(changes).toHaveTextContent('Replaces behavior directions from release-bot')
    const button = screen.getByTestId('worker-kit-apply')
    expect(button).toHaveTextContent('Replace directions')
    expect(button).toBeEnabled()
    fireEvent.click(button)
    const done = await screen.findByTestId('worker-kit-done')
    expect(done).toHaveTextContent('Behavior directions replaced.')
    expect(done).not.toHaveTextContent('Added')
  })

  it('counts directions into an empty slot as an add, with no current directions shown', async () => {
    mockKit.preview.mockResolvedValue({
      ...PREVIEW,
      directions: 'New SOP.',
      current_directions: '',
      changes: [
        { section: 'prompt', id: 'prompt', change: 'added' },
        { section: 'mcpServers', id: 'jira', change: 'added' },
      ],
    })
    renderPane()
    const changes = await previewAdd()
    expect(changes).toHaveTextContent('Adds 2 from release-bot')
    expect(within(changes).queryByTestId('worker-kit-current-directions')).toBeNull()
    expect(screen.getByTestId('worker-kit-apply')).toHaveTextContent('Add these 2')
  })

  it('confirms a finished add and offers Approvals when it granted any', async () => {
    const onOpenApprovals = vi.fn()
    mockKit.preview.mockResolvedValue({
      ...PREVIEW,
      runs_unasked: true,
      changes: [
        { section: 'mcpServers', id: 'jira', change: 'added' },
        { section: 'allowedTools', id: '@jira/search', change: 'added' },
      ],
    })
    renderPane(onOpenApprovals)
    await previewAdd()
    fireEvent.click(screen.getByTestId('worker-kit-apply'))
    const done = await screen.findByTestId('worker-kit-done')
    expect(done).toHaveTextContent('Added 2 to Pixel.')
    expect(done).toHaveTextContent('Tools under Auto-approvals now run without asking.')
    fireEvent.click(within(done).getByTestId('worker-kit-open-approvals'))
    expect(onOpenApprovals).toHaveBeenCalledTimes(1)
  })

  it('confirms a finished add with no Approvals link when it granted none', async () => {
    renderPane(vi.fn())
    await previewAdd()
    fireEvent.click(screen.getByTestId('worker-kit-apply'))
    const done = await screen.findByTestId('worker-kit-done')
    expect(done).toHaveTextContent('Added 2 to Pixel.')
    expect(within(done).queryByTestId('worker-kit-open-approvals')).toBeNull()
  })

  it('shows an already-present item as skipped, not added', async () => {
    mockKit.preview.mockResolvedValue({
      ...PREVIEW,
      changes: [
        { section: 'mcpServers', id: 'notes', change: 'added' },
        { section: 'mcpServers', id: 'jira', change: 'skipped' },
      ],
    })
    renderPane()
    const changes = await previewAdd()
    const badges = within(changes).getAllByTestId('worker-kit-change-badge')
    expect(badges.map(b => b.textContent)).toEqual(['New', 'Already has'])
    expect(badges[0]).toHaveClass('bg-ok-subtle')
  })

  it('disables Add and says nothing is new when every item is already present', async () => {
    mockKit.preview.mockResolvedValue({
      ...PREVIEW,
      changes: [{ section: 'mcpServers', id: 'jira', change: 'skipped' }],
    })
    renderPane()
    const changes = await previewAdd()
    expect(changes).toHaveTextContent('release-bot adds nothing new')
    expect(screen.getByTestId('worker-kit-apply')).toBeDisabled()
    expect(mockKit.apply).not.toHaveBeenCalled()
  })

  it('says which tools will run unasked when the add carries an auto-approval', async () => {
    mockKit.preview.mockResolvedValue({
      ...PREVIEW,
      changes: [
        { section: 'mcpServers', id: 'jira', change: 'added' },
        { section: 'allowedTools', id: '@jira/search', change: 'added' },
      ],
    })
    renderPane()
    await previewAdd()
    expect(screen.getByTestId('worker-kit-approvals-hint')).toHaveTextContent('will run without asking')
  })

  it('shows the granted hint when a restored tool already has approval', async () => {
    mockKit.preview.mockResolvedValue({
      ...PREVIEW,
      runs_unasked: true,
      changes: [{ section: 'tools', id: '@notes/write', change: 'added' }],
    })
    renderPane()
    await previewAdd()
    expect(screen.getByTestId('worker-kit-approvals-hint')).toHaveTextContent('will run without asking')
  })

  it('keeps the ask-first hint when the add carries no auto-approval', async () => {
    mockKit.preview.mockResolvedValue({
      ...PREVIEW,
      changes: [{ section: 'mcpServers', id: 'notes', change: 'added' }],
    })
    renderPane()
    await previewAdd()
    expect(screen.getByTestId('worker-kit-approvals-hint')).toHaveTextContent('ask before running')
  })

  it('names a skill and an approval in the preview, keeping the stored value in the title', async () => {
    const uri = 'skill:///home/user/.kiro/skills/release-notes/SKILL.md'
    mockKit.preview.mockResolvedValue({
      ...PREVIEW,
      changes: [
        { section: 'resources', id: uri, change: 'added' },
        { section: 'allowedTools', id: '@notes/save_note', change: 'added' },
      ],
    })
    renderPane()
    const changes = await previewAdd()
    const names = within(changes).getAllByTestId('worker-kit-change-name')
    expect(names.map(n => n.textContent)).toEqual(['release-notes', 'save_note on notes'])
    expect(names[0]).toHaveAttribute('title', uri)
    expect(changes).not.toHaveTextContent('skill://')
  })

  it('cancelling the preview writes nothing', async () => {
    renderPane()
    await previewAdd()
    const dontAdd = screen.getByTestId('worker-kit-cancel')
    expect(dontAdd).toHaveTextContent("Don't add")
    fireEvent.click(dontAdd)
    expect(screen.queryByTestId('worker-kit-changes')).toBeNull()
    expect(mockKit.apply).not.toHaveBeenCalled()
  })

  it('groups the preview by kind, each change word a coloured badge that keeps its word', async () => {
    mockKit.preview.mockResolvedValue({
      ...PREVIEW,
      changes: [
        { section: 'allowedTools', id: '@jira/search', change: 'added' },
        { section: 'mcpServers', id: 'slack', change: 'skipped' },
        { section: 'mcpServers', id: 'jira', change: 'added' },
        { section: 'skills', id: 'notes', change: 'added' },
      ],
    })
    renderPane()
    const changes = await previewAdd()
    const headers = within(changes).getAllByTestId('panel-section-header').map(h => h.textContent)
    expect(headers).toEqual(['MCP servers2', 'Skills1', 'Auto-approvals1'])
    const connections = within(changes).getByTestId('worker-kit-group-connections')
    // Added before skipped.
    expect(within(connections).getAllByTestId('worker-kit-change-name').map(n => n.textContent)).toEqual(['jira', 'slack'])
    const badges = within(connections).getAllByTestId('worker-kit-change-badge')
    expect(badges.map(b => b.textContent)).toEqual(['New', 'Already has'])
    expect(badges[0]).toHaveClass('bg-ok-subtle', 'text-text')
  })

  it.each([
    ['stale_revision', 'changed after the preview. The changes below are up to date'],
    ['template_restrictions_uncarried', "hooks the worker can't keep. Choose another one."],
    ['template_unreadable', "That agent can't be read."],
  ])('says what a %s refusal means, never the code', async (code, text) => {
    mockKit.apply.mockRejectedValue(new ApiError(409, code, JSON.stringify({ error: code, code })))
    renderPane()
    await previewAdd()
    fireEvent.click(screen.getByTestId('worker-kit-apply'))
    const notice = await screen.findByTestId('worker-kit-error')
    expect(notice).toHaveTextContent(text)
    expect(notice).not.toHaveTextContent(code)
  })

  it('replaces a stale preview with a fresh one for the same agent, which then applies', async () => {
    mockKit.apply.mockRejectedValueOnce(new ApiError(409, 'stale_revision', '{"error":"stale_revision","code":"stale_revision"}'))
    renderPane()
    await previewAdd()
    mockKit.preview.mockResolvedValue({ ...PREVIEW, revision: 'rev-2' })
    fireEvent.click(screen.getByTestId('worker-kit-apply'))
    await waitFor(() => expect(mockKit.preview).toHaveBeenCalledTimes(2))
    expect(mockKit.preview).toHaveBeenLastCalledWith('pixel', { template: 'release-bot' })
    await screen.findByTestId('worker-kit-changes')
    // The stale-revision notice comes from the rejected apply mutation.
    expect(await screen.findByTestId('worker-kit-error', {}, { timeout: 2000 })).toHaveTextContent('The changes below are up to date')
    fireEvent.click(screen.getByTestId('worker-kit-apply'))
    await waitFor(() => expect(mockKit.apply).toHaveBeenLastCalledWith('pixel', {
      template: 'release-bot', revision: 'rev-2',
    }))
  })

  it('offers Try again after a failed preview, which re-previews the same agent', async () => {
    mockKit.preview.mockRejectedValueOnce(new Error('network down'))
    renderPane()
    await screen.findByRole('option', { name: 'release-bot' })
    fireEvent.click(screen.getByRole('option', { name: 'release-bot' }))
    expect(await screen.findByTestId('worker-kit-error')).toHaveTextContent('network down')
    fireEvent.click(screen.getByTestId('worker-kit-retry'))
    await screen.findByTestId('worker-kit-changes')
    expect(mockKit.preview).toHaveBeenCalledTimes(2)
    expect(mockKit.preview).toHaveBeenLastCalledWith('pixel', { template: 'release-bot' })
    expect(screen.queryByTestId('worker-kit-retry')).toBeNull()
  })

  it('Cancel after a failed preview clears the pick and the error, writing nothing', async () => {
    mockKit.preview.mockRejectedValueOnce(new Error('network down'))
    renderPane()
    await screen.findByRole('option', { name: 'release-bot' })
    fireEvent.click(screen.getByRole('option', { name: 'release-bot' }))
    await screen.findByTestId('worker-kit-retry')
    fireEvent.click(screen.getByTestId('worker-kit-retry-cancel'))
    expect(screen.queryByTestId('worker-kit-retry')).toBeNull()
    expect(screen.getByRole('combobox')).toHaveTextContent('')
    expect(screen.queryByTestId('worker-kit-error')).toBeNull()
    expect(mockKit.apply).not.toHaveBeenCalled()
  })

  it('keeps the preview to retry Add after a refusal that is not about staleness', async () => {
    mockKit.apply.mockRejectedValueOnce(new Error('network down'))
    renderPane()
    await previewAdd()
    fireEvent.click(screen.getByTestId('worker-kit-apply'))
    expect(await screen.findByTestId('worker-kit-error')).toHaveTextContent('network down')
    expect(screen.getByTestId('worker-kit-apply')).toBeEnabled()
    expect(mockKit.preview).toHaveBeenCalledTimes(1)
  })

  it('keeps a readable message the transport already made', async () => {
    mockKit.preview.mockRejectedValue(new ApiError(403, 'Only the owner can do this.', '{"error":"Only the owner can do this."}'))
    renderPane()
    await screen.findByRole('option', { name: 'release-bot' })
    fireEvent.click(screen.getByRole('option', { name: 'release-bot' }))
    expect(await screen.findByTestId('worker-kit-error')).toHaveTextContent('Only the owner can do this.')
  })
})
