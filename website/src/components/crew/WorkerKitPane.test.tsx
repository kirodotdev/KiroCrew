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

  it('does not ask about additions when there are none', async () => {
    renderPane()
    await screen.findByTestId('worker-kit-current')
    expect(screen.queryByTestId('worker-kit-keep')).toBeNull()
  })
})
