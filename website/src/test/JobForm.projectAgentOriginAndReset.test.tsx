import { describe, it, expect, vi, afterEach } from 'vitest'
import { screen, waitFor, fireEvent, within } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import JobForm from '../components/JobForm'
import { api } from '../api/client'
import type { CronJob } from '../types'

/**
 * UX Review, two findings on the same surface:
 *
 * 1. Changing the Project directory silently cleared a project-only Agent pick,
 *    so the job saved under "default" with no acknowledgment anywhere -- the
 *    user's deliberate choice vanished between a keystroke and pressing Save.
 * 2. A folder-supplied agent rendered the same `kirocrew` badge as a configured
 *    one, so nobody could tell which picks the directory contributed (and would
 *    therefore lose when it was cleared). The `overrides global` chip covered
 *    only COLLIDING rows, a strict subset.
 *
 * NO `vi.clearAllMocks()` here, deliberately and for the same reason the sibling
 * collision test omits it: it wipes the factory's `mockResolvedValue` defaults,
 * after which the roster query resolves `undefined` and no project row ever
 * renders. Only call counts are reset, below.
 */

vi.mock('../api/client', () => ({
  api: {
    models: vi.fn().mockResolvedValue({ models: [] }),
    kirocrewAgents: vi.fn().mockResolvedValue({ agents: [], default_agent: 'default' }),
    updateCron: vi.fn().mockResolvedValue({}),
    createCron: vi.fn().mockResolvedValue({}),
    listDirs: vi.fn().mockResolvedValue({ dirs: [] }),
  },
}))

const globalRoster = [{ name: 'default', source: 'kirocrew', description: '' }]

// The endpoint returns the WHOLE roster, each row carrying its own scope -- so
// the badge must key on scope, not on mere presence in this payload.
const projectRoster = {
  agents: [
    { name: 'default', source: 'kirocrew', description: '', scope: 'global' },
    { name: 'repo-smoke', source: 'kirocrew', description: '', scope: 'project' },
  ],
  default_agent: 'default',
}

function messageJob(): CronJob {
  return {
    id: 'j1', name: 'nightly', message: 'do the thing', schedule: '', enabled: true,
    cron_expr: '0 3 * * *',
  } as CronJob
}

function mount() {
  renderWithProviders(
    <JobForm
      job={messageJob()}
      agents={globalRoster}
      defaultAgent="default"
      onSaved={() => {}}
      layout="vertical"
    />,
  )
}

afterEach(() => {
  vi.mocked(api.kirocrewAgents).mockClear()
})

describe('JobForm — project agent origin and reset', () => {
  it('marks a folder-supplied agent as coming from this folder, and only it', async () => {
    vi.mocked(api.kirocrewAgents).mockResolvedValueOnce(projectRoster)
    mount()
    fireEvent.change(screen.getByLabelText('Project directory'), { target: { value: '/repo-a' } })
    await waitFor(() => expect(api.kirocrewAgents).toHaveBeenCalled())
    fireEvent.click(screen.getByLabelText('Switch agent'))
    await waitFor(() =>
      expect(screen.getByRole('option', { name: /repo-smoke/ })).toBeInTheDocument(),
    )
    const listbox = screen.getByRole('listbox')

    expect(within(listbox).getByRole('option', { name: /repo-smoke/ })).toHaveTextContent(
      'folder agent',
    )
    // The configured row must NOT carry it -- that is the whole point of keying
    // on scope rather than on the payload the folder fetch returned.
    expect(within(listbox).getByRole('option', { name: /default/ })).not.toHaveTextContent(
      'folder agent',
    )
  })

  it('says which agent it cleared when the new folder no longer defines it', async () => {
    // Folder A supplies `repo-smoke`; folder B supplies only the global.
    vi.mocked(api.kirocrewAgents)
      .mockResolvedValueOnce(projectRoster)
      .mockResolvedValueOnce({ agents: globalRoster, default_agent: 'default' })
    mount()
    fireEvent.change(screen.getByLabelText('Project directory'), { target: { value: '/repo-a' } })
    await waitFor(() => expect(api.kirocrewAgents).toHaveBeenCalled())
    fireEvent.click(screen.getByLabelText('Switch agent'))
    await waitFor(() =>
      expect(screen.getByRole('option', { name: /repo-smoke/ })).toBeInTheDocument(),
    )
    fireEvent.click(screen.getByRole('option', { name: /repo-smoke/ }))

    // Move to a folder that does not define it -- the reset fires here.
    fireEvent.change(screen.getByLabelText('Project directory'), { target: { value: '/repo-b' } })

    const note = await screen.findByTestId('jobform-agent-reset-note')
    expect(note.textContent).toContain('repo-smoke')
  })
})
