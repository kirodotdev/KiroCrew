/** Tests for the job form's "Start each run in a new chat" switch (issue #22701).
 *
 *  The switch is `new_chat_per_run`: every run of a stateless agent job opens its
 *  own chat tab on its own fresh session. The form chooses a fresh session only
 *  when it creates a job, so the switch is offered on a new job and on a job that
 *  already starts fresh, and it is refused on a job that keeps one session.
 */

import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import { renderWithProviders } from './helpers'
import JobForm, { buildBody, parseJobDefaults } from '../components/JobForm'
import type { ChatFolder, CronJob } from '../types'
import { api } from '../api/client'

vi.mock('../api/client', () => ({
  api: {
    updateCron: vi.fn(),
    createCron: vi.fn(),
    models: vi.fn().mockResolvedValue({ models: [] }),
    kirocrewAgents: vi.fn().mockResolvedValue({ agents: [], default_agent: '' }),
    chatFolders: vi.fn(),
    kirocrewConfig: vi.fn(),
  },
}))

const FOLDERS: ChatFolder[] = [{ id: 'work', name: 'Work', order: 0 }]
const SWITCH = 'Start each run in a new chat'

function makeJob(overrides: Partial<CronJob> = {}): CronJob {
  return {
    id: 'nc1', name: 'monthly status', message: 'Draft the status update.', schedule: '', enabled: true,
    cron_expr: '0 9 8 * *', ...overrides,
  } as CronJob
}

beforeEach(() => {
  vi.mocked(api.chatFolders).mockResolvedValue(FOLDERS)
  vi.mocked(api.kirocrewConfig).mockResolvedValue({ dashboard: { folder_sort: 'custom' } })
  vi.mocked(api.createCron).mockResolvedValue({})
  vi.mocked(api.updateCron).mockResolvedValue({})
})

describe('the new-chat-per-run switch', () => {
  it('creates a job whose runs each open a new chat, filed into no folder', async () => {
    const onSaved = vi.fn()
    renderWithProviders(<JobForm layout="vertical" agents={[]} onSaved={onSaved} />)
    await userEvent.type(await screen.findByLabelText('Name'), 'monthly status')
    await userEvent.type(screen.getByLabelText('Message'), 'Draft the status update.')
    const toggle = screen.getByLabelText(SWITCH)
    expect(toggle).not.toBeChecked()
    await userEvent.click(toggle)
    expect(toggle).toBeChecked()
    // A run's own tab is not filed, so the picker says so instead of taking input.
    expect(screen.getByLabelText('Chat folder')).toBeDisabled()
    await userEvent.click(screen.getByRole('button', { name: 'Create' }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledOnce())
    expect(api.createCron).toHaveBeenCalledWith(expect.objectContaining({ new_chat_per_run: true, chat_folder_id: '' }))
  })

  it('loads the saved switch on a fresh-session job and saves turning it off', async () => {
    const onSaved = vi.fn()
    renderWithProviders(<JobForm
      layout="vertical" agents={[]} onSaved={onSaved}
      job={makeJob({ persistent_session: false, new_chat_per_run: true })}
    />)
    const toggle = await screen.findByLabelText(SWITCH)
    expect(toggle).toBeChecked()
    // The switch is a `role="switch"` div, so its disabled state is aria-disabled.
    expect(toggle).not.toHaveAttribute('aria-disabled')
    await userEvent.click(toggle)
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledOnce())
    expect(api.updateCron).toHaveBeenCalledWith('nc1', expect.objectContaining({ new_chat_per_run: false }))
  })

  it('refuses the switch on a job that keeps one session, and says why', async () => {
    renderWithProviders(<JobForm layout="vertical" agents={[]} onSaved={() => {}} job={makeJob({ persistent_session: true })} />)
    const toggle = await screen.findByLabelText(SWITCH)
    expect(toggle).toHaveAttribute('aria-disabled', 'true')
    expect(screen.getByText(/keeps one session for all its runs/)).toBeVisible()
    await userEvent.click(toggle)
    expect(toggle).not.toBeChecked()
  })

  it('leaves the switch off a script job, which never opens a chat', () => {
    const f = { ...parseJobDefaults(makeJob({ script: '~/.kiro/crew/crons/f.py:run', message: '' })), newChatPerRun: true }
    expect(buildBody(f, 'UTC', () => {})!.new_chat_per_run).toBeUndefined()
  })
})
