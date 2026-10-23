/**
 * WorkerKitPane — the Advanced pane's worker kit: pick one template, see what
 * copying it would change, confirm, and only then write. A re-copy asks whether
 * to keep what was added since the last copy.
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
  changes: [
    { section: 'prompt', id: 'prompt', change: 'changed' },
    { section: 'mcpServers', id: 'jira', change: 'added' },
  ],
}

function renderPane() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <WorkerKitPane member="pixel" name="Pixel" templates={['kirocrew', 'release-bot']} />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  mockKit.get.mockResolvedValue({ member: 'pixel', template: '', revision: 'r0', has_additions_since: false })
  mockKit.preview.mockResolvedValue(PREVIEW)
  mockKit.apply.mockResolvedValue({ member: 'pixel', template: 'release-bot', revision: 'r2', has_additions_since: false })
})

describe('WorkerKitPane', () => {
  it('names the crewmate by its display name and says nothing is copied yet', async () => {
    renderPane()
    expect(await screen.findByTestId('worker-kit-current')).toHaveTextContent('No template copied yet')
    expect(screen.getByText(/Pixel hands work to a worker/)).toBeInTheDocument()
    expect(screen.queryByText(/pixel hands/)).toBeNull()
  })

  it('previews first and writes only on Apply, with the preview revision', async () => {
    renderPane()
    await screen.findByTestId('worker-kit-current')
    fireEvent.click(screen.getByRole('option', { name: 'release-bot' }))
    fireEvent.click(screen.getByTestId('worker-kit-preview'))
    const changes = await screen.findByTestId('worker-kit-changes')
    expect(changes).toHaveTextContent('Copying release-bot would make these changes')
    expect(changes).toHaveTextContent('Added')
    expect(changes).toHaveTextContent('jira')
    expect(mockKit.preview).toHaveBeenCalledWith('pixel', { template: 'release-bot', keep_additions: true })
    expect(mockKit.apply).not.toHaveBeenCalled()
    fireEvent.click(screen.getByTestId('worker-kit-apply'))
    await waitFor(() => expect(mockKit.apply).toHaveBeenCalledWith('pixel', {
      template: 'release-bot', keep_additions: true, revision: 'rev-1',
    }))
  })

  it('names a skill and an approval in the preview, keeping the stored value in the title', async () => {
    const uri = 'skill:///home/user/.kiro/skills/release-notes/SKILL.md'
    mockKit.preview.mockResolvedValue({
      ...PREVIEW,
      changes: [
        { section: 'resources', id: uri, change: 'removed' },
        { section: 'allowedTools', id: '@notes/save_note', change: 'removed' },
      ],
    })
    renderPane()
    await screen.findByTestId('worker-kit-current')
    fireEvent.click(screen.getByRole('option', { name: 'release-bot' }))
    fireEvent.click(screen.getByTestId('worker-kit-preview'))
    const changes = await screen.findByTestId('worker-kit-changes')
    const names = within(changes).getAllByTestId('worker-kit-change-name')
    expect(names.map(n => n.textContent)).toEqual(['release-notes', 'save_note on notes'])
    expect(names[0]).toHaveAttribute('title', uri)
    expect(changes).not.toHaveTextContent('skill://')
  })

  it('cancelling the preview writes nothing', async () => {
    renderPane()
    await screen.findByTestId('worker-kit-current')
    fireEvent.click(screen.getByRole('option', { name: 'release-bot' }))
    fireEvent.click(screen.getByTestId('worker-kit-preview'))
    await screen.findByTestId('worker-kit-changes')
    fireEvent.click(screen.getByTestId('worker-kit-cancel'))
    expect(screen.queryByTestId('worker-kit-changes')).toBeNull()
    expect(mockKit.apply).not.toHaveBeenCalled()
  })

  it('offers a re-copy of the copied template and asks whether to keep additions', async () => {
    mockKit.get.mockResolvedValue({ member: 'pixel', template: 'release-bot', revision: 'r1', has_additions_since: true })
    renderPane()
    expect(await screen.findByTestId('worker-kit-current')).toHaveTextContent('Copied from release-bot')
    expect(screen.getByTestId('worker-kit-preview')).toHaveTextContent('Re-copy from template')
    const keep = screen.getByTestId('worker-kit-keep')
    expect(keep).toBeChecked()
    fireEvent.click(keep)
    fireEvent.click(screen.getByTestId('worker-kit-preview'))
    await screen.findByTestId('worker-kit-changes')
    expect(mockKit.preview).toHaveBeenCalledWith('pixel', { template: 'release-bot', keep_additions: false })
    fireEvent.click(screen.getByTestId('worker-kit-apply'))
    await waitFor(() => expect(mockKit.apply).toHaveBeenCalledWith('pixel', {
      template: 'release-bot', keep_additions: false, revision: 'rev-1',
    }))
  })

  it('names a limit the copy sets on a server the worker already has', async () => {
    mockKit.preview.mockResolvedValue({
      ...PREVIEW,
      changes: [
        { section: 'disabledServers', id: 'kirocrew-core', change: 'added' },
        { section: 'disabledTools', id: '@kirocrew-core/cron_add', change: 'added' },
      ],
    })
    renderPane()
    await screen.findByTestId('worker-kit-current')
    fireEvent.click(screen.getByRole('option', { name: 'release-bot' }))
    fireEvent.click(screen.getByTestId('worker-kit-preview'))
    const group = await screen.findByTestId('worker-kit-group-off')
    expect(within(group).getByTestId('panel-section-header')).toHaveTextContent('Turned off2')
    expect(within(group).getAllByTestId('worker-kit-change-name').map(n => n.textContent)).toEqual([
      'cron_add on kirocrew-core',
      'kirocrew-core',
    ])
    // A restriction's badge states its effect, never a green "Added".
    const badges = within(group).getAllByTestId('worker-kit-change-badge')
    expect(badges.map(b => b.textContent)).toEqual(['Turns off', 'Turns off'])
    expect(badges[0]).toHaveClass('bg-danger-subtle')
  })

  it('says a lifted restriction turns the tool back on', async () => {
    mockKit.preview.mockResolvedValue({
      ...PREVIEW,
      changes: [{ section: 'disabledTools', id: '@kirocrew-core/cron_add', change: 'removed' }],
    })
    renderPane()
    await screen.findByTestId('worker-kit-current')
    fireEvent.click(screen.getByRole('option', { name: 'release-bot' }))
    fireEvent.click(screen.getByTestId('worker-kit-preview'))
    const badge = within(await screen.findByTestId('worker-kit-group-off')).getByTestId('worker-kit-change-badge')
    expect(badge).toHaveTextContent('Turns back on')
    expect(badge).toHaveClass('bg-ok-subtle')
  })

  it('groups the preview by kind, each change word a coloured badge that keeps its word', async () => {
    mockKit.preview.mockResolvedValue({
      ...PREVIEW,
      changes: [
        { section: 'allowedTools', id: '@jira/search', change: 'added' },
        { section: 'mcpServers', id: 'slack', change: 'removed' },
        { section: 'prompt', id: 'prompt', change: 'changed' },
        { section: 'mcpServers', id: 'jira', change: 'added' },
        { section: 'mcpServers', id: 'builder', change: 'changed' },
      ],
    })
    renderPane()
    await screen.findByTestId('worker-kit-current')
    fireEvent.click(screen.getByRole('option', { name: 'release-bot' }))
    fireEvent.click(screen.getByTestId('worker-kit-preview'))
    const changes = await screen.findByTestId('worker-kit-changes')
    const headers = within(changes).getAllByTestId('panel-section-header').map(h => h.textContent)
    expect(headers).toEqual(['Instructions1', 'Connections3', 'Auto-approvals1'])
    const connections = within(changes).getByTestId('worker-kit-group-connections')
    // Added, then changed, then removed.
    expect(within(connections).getAllByTestId('worker-kit-change-name').map(n => n.textContent)).toEqual(['jira', 'builder', 'slack'])
    const badges = within(connections).getAllByTestId('worker-kit-change-badge')
    expect(badges.map(b => b.textContent)).toEqual(['Added', 'Changed', 'Removed'])
    // Tinted by hue; the word itself in the text colour, the hue on the glyph.
    expect(badges[0]).toHaveClass('bg-ok-subtle', 'text-text')
    expect(badges[1]).toHaveClass('bg-warn-subtle', 'text-text')
    expect(badges[2]).toHaveClass('bg-danger-subtle', 'text-text')
    expect(badges[0].querySelector('svg')).toHaveClass('text-ok-text')
    expect(badges[2].querySelector('svg')).toHaveClass('text-danger-text')
  })

  it.each([
    ['stale_revision', 'changed after the preview. Press “Copy into the worker” again to see the new changes.'],
    ['template_restrictions_uncarried', "hooks the worker can't keep. Choose another template."],
    ['template_unreadable', "That template can't be read."],
  ])('says what a %s refusal means, never the code', async (code, text) => {
    mockKit.apply.mockRejectedValue(new ApiError(409, code, JSON.stringify({ error: code, code })))
    renderPane()
    await screen.findByTestId('worker-kit-current')
    fireEvent.click(screen.getByRole('option', { name: 'release-bot' }))
    fireEvent.click(screen.getByTestId('worker-kit-preview'))
    await screen.findByTestId('worker-kit-changes')
    fireEvent.click(screen.getByTestId('worker-kit-apply'))
    const notice = await screen.findByTestId('worker-kit-error')
    expect(notice).toHaveTextContent(text)
    expect(notice).not.toHaveTextContent(code)
  })

  it('names the re-copy button when that is the button the pane shows', async () => {
    mockKit.get.mockResolvedValue({ member: 'pixel', template: 'release-bot', revision: 'r1', has_additions_since: false })
    mockKit.apply.mockRejectedValue(new ApiError(409, 'stale_revision', '{"error":"stale_revision","code":"stale_revision"}'))
    renderPane()
    await screen.findByTestId('worker-kit-current')
    fireEvent.click(screen.getByTestId('worker-kit-preview'))
    await screen.findByTestId('worker-kit-changes')
    fireEvent.click(screen.getByTestId('worker-kit-apply'))
    const notice = await screen.findByTestId('worker-kit-error')
    expect(notice).toHaveTextContent('Press “Re-copy from template” again')
    // The button the sentence names is the one on screen.
    expect(screen.getByTestId('worker-kit-preview')).toHaveTextContent('Re-copy from template')
  })

  it('drops a stale preview so the next step is a fresh one', async () => {
    mockKit.apply.mockRejectedValue(new ApiError(409, 'stale_revision', '{"error":"stale_revision","code":"stale_revision"}'))
    renderPane()
    await screen.findByTestId('worker-kit-current')
    fireEvent.click(screen.getByRole('option', { name: 'release-bot' }))
    fireEvent.click(screen.getByTestId('worker-kit-preview'))
    await screen.findByTestId('worker-kit-changes')
    fireEvent.click(screen.getByTestId('worker-kit-apply'))
    await screen.findByTestId('worker-kit-error')
    expect(screen.queryByTestId('worker-kit-changes')).toBeNull()
    expect(screen.getByTestId('worker-kit-preview')).toBeEnabled()
  })

  it('keeps a readable message the transport already made', async () => {
    mockKit.preview.mockRejectedValue(new ApiError(403, 'Only the owner can do this.', '{"error":"Only the owner can do this."}'))
    renderPane()
    await screen.findByTestId('worker-kit-current')
    fireEvent.click(screen.getByRole('option', { name: 'release-bot' }))
    fireEvent.click(screen.getByTestId('worker-kit-preview'))
    expect(await screen.findByTestId('worker-kit-error')).toHaveTextContent('Only the owner can do this.')
  })

  it('does not ask about additions when there are none', async () => {
    renderPane()
    await screen.findByTestId('worker-kit-current')
    expect(screen.queryByTestId('worker-kit-keep')).toBeNull()
  })
})
