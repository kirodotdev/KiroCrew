/** Tests for the job form's chat-folder picker (issue #1620).
 *
 *  The setting decides where a recurring job's RUNS land in the chat sidebar, so
 *  the properties that matter are: the reader can tell two same-named folders
 *  apart before picking one; an existing job's placement round-trips instead of
 *  being cleared by the next unrelated save; and clearing it actually reaches the
 *  backend, because "" is the real value for "do not file" and an omitted field
 *  on a PATCH would make the clear a no-op.
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
  },
}))

const FOLDERS: ChatFolder[] = [
  { id: 'work', name: 'Work', order: 0 },
  { id: 'standups', name: 'Standups', order: 0, parent_id: 'work' },
  { id: 'home', name: 'Home', order: 1 },
]

function makeJob(overrides: Partial<CronJob> = {}): CronJob {
  return {
    id: 'cf1', name: 'standup brief', message: 'Write the brief.', schedule: '', enabled: true,
    every_secs: 3600, ...overrides,
  } as CronJob
}

beforeEach(() => {
  vi.mocked(api.chatFolders).mockResolvedValue(FOLDERS)
})

describe('the chat-folder picker', () => {
  it('offers every sidebar folder, a nested one by its full path', async () => {
    renderWithProviders(<JobForm layout="vertical" agents={[]} onSaved={() => {}} />)
    await userEvent.click(await screen.findByLabelText('Chat folder'))
    // The full path, not an indent: a select shows one row at a time with its
    // siblings hidden, so depth alone would not say which "Standups" this is.
    expect(await screen.findByText('Work › Standups')).toBeInTheDocument()
    expect(screen.getByText('Work')).toBeInTheDocument()
    expect(screen.getByText('Home')).toBeInTheDocument()
  })

  it('opens on "do not file runs" for a new job', async () => {
    renderWithProviders(<JobForm layout="vertical" agents={[]} onSaved={() => {}} />)
    expect(await screen.findByLabelText('Chat folder')).toHaveTextContent('Do not file runs')
  })

  it('explains what filing does without claiming it replaces delivery', async () => {
    renderWithProviders(<JobForm layout="vertical" agents={[]} onSaved={() => {}} />)
    // "does not hide anything" is the part that disambiguates this control from
    // the "Hide in chat" toggle directly above it, in the DEFAULT state -- a blind
    // reader could not tell the two apart while it only appeared after checking
    // the box.
    expect(await screen.findByText(/Filing does not hide anything/)).toBeVisible()
    expect(screen.getByText(/notifications and Slack delivery are unchanged/)).toBeVisible()
  })

  it('sends the picked folder on create', async () => {
    const onSaved = vi.fn()
    vi.mocked(api.createCron).mockResolvedValue({})
    renderWithProviders(<JobForm layout="vertical" agents={[]} onSaved={onSaved} />)
    await userEvent.type(await screen.findByLabelText('Name'), 'Standup brief')
    await userEvent.type(screen.getByLabelText('Message'), 'Write the brief.')
    await userEvent.click(await screen.findByLabelText('Chat folder'))
    await userEvent.click(await screen.findByText('Work › Standups'))
    await userEvent.click(screen.getByRole('button', { name: 'Create' }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledOnce())
    expect(api.createCron).toHaveBeenCalledWith(
      expect.objectContaining({ chat_folder_id: 'standups' }),
    )
  })

  it('shows an existing job as filed where it is filed', async () => {
    renderWithProviders(
      <JobForm layout="vertical" agents={[]} onSaved={() => {}} job={makeJob({ chat_folder_id: 'home' })} />,
    )
    const picker = await screen.findByLabelText('Chat folder')
    await waitFor(() => expect(picker).toHaveTextContent('Home'))
  })

  it('carries the unchanged placement through an unrelated edit', async () => {
    // Without the read side the picker would open empty and this save would
    // silently unfile the job.
    const onSaved = vi.fn()
    vi.mocked(api.updateCron).mockResolvedValue({})
    renderWithProviders(
      <JobForm layout="vertical" agents={[]} onSaved={onSaved} job={makeJob({ chat_folder_id: 'home' })} />,
    )
    await userEvent.type(await screen.findByLabelText('Name'), ' v2')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledOnce())
    expect(api.updateCron).toHaveBeenCalledWith(
      'cf1',
      expect.objectContaining({ chat_folder_id: 'home' }),
    )
  })

  it('sends the empty value when the reader clears it, so the clear lands', async () => {
    const onSaved = vi.fn()
    vi.mocked(api.updateCron).mockResolvedValue({})
    renderWithProviders(
      <JobForm layout="vertical" agents={[]} onSaved={onSaved} job={makeJob({ chat_folder_id: 'home' })} />,
    )
    await userEvent.click(await screen.findByLabelText('Chat folder'))
    await userEvent.click(await screen.findByText('Do not file runs'))
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledOnce())
    expect(api.updateCron).toHaveBeenCalledWith(
      'cf1',
      expect.objectContaining({ chat_folder_id: '' }),
    )
  })

  it('defaults to unfiled for a new job', () => {
    expect(parseJobDefaults(undefined).chatFolderId).toBe('')
  })

  it('reads the existing setting rather than defaulting over it', () => {
    expect(parseJobDefaults(makeJob({ chat_folder_id: 'work' })).chatFolderId).toBe('work')
  })

  it('never confuses the placement with the Schedule-page grouping', () => {
    // folder_id groups the job's ROW on the Schedule page; chat_folder_id decides
    // where its RUNS land. Reading one off the other would move the wrong thing.
    const defaults = parseJobDefaults(makeJob({ folder_id: 'sched9' }))
    expect(defaults.chatFolderId).toBe('')
  })

  it('sends no placement for a script job, which never gets a chat session', () => {
    // A script cron takes no agent turn and creates no slot, so a folder it
    // named could never receive anything. Storing the setting anyway is how a
    // setting starts lying -- the same reason minimal_context is omitted there.
    const body = buildBody(
      { ...parseJobDefaults(makeJob({ script: 'a.py:run' })), chatFolderId: 'home' },
      'UTC',
      () => {},
      true,
    )
    expect(body?.chat_folder_id).toBe('')
  })

  it('does not offer the picker on a script job at all', async () => {
    renderWithProviders(
      <JobForm layout="vertical" agents={[]} onSaved={() => {}} job={makeJob({ script: 'a.py:run' })} />,
    )
    await screen.findByLabelText('Name')
    expect(screen.queryByLabelText('Chat folder')).not.toBeInTheDocument()
  })

  it('refuses input and says why while Hide in chat is on', async () => {
    // hide_in_chat is the explicit "no tab" opt-out, and a run with no tab has
    // nothing to file -- so an enabled picker there would accept a setting whose
    // only observable effect is a folder that never fills up.
    renderWithProviders(<JobForm layout="vertical" agents={[]} onSaved={() => {}} />)
    const picker = await screen.findByLabelText('Chat folder')
    expect(picker).toBeEnabled()

    await userEvent.click(screen.getByLabelText('Hide in chat'))
    expect(await screen.findByText(/Turn off/)).toBeVisible()
    expect(screen.getByLabelText('Chat folder')).toBeDisabled()
  })

  it('refuses input and says why on a stateless job', async () => {
    // A job that runs on a fresh session every fire has no job-wide tab to file,
    // and the backend refuses the pair at save time. Saying so here beats a 400
    // under Save. The form never edits persistent_session; it only reads it.
    renderWithProviders(
      <JobForm layout="vertical" agents={[]} onSaved={() => {}} job={makeJob({ persistent_session: false })} />,
    )
    expect(await screen.findByLabelText('Chat folder')).toBeDisabled()
    const hint = screen.getByText(/needs a persistent session/)
    expect(hint).toBeVisible()
    // The form has no persistence switch, so the hint has to say where the
    // choice lives instead of sending the reader hunting for one.
    expect(hint).toHaveTextContent(/created|creation/)
    expect(screen.queryByText(/Group this job|Put this job/)).toBeNull()
  })

  it('keeps the stored folder while Hide in chat is on, rather than wiping it', () => {
    // The hint says "turn off Hide in chat to use this", which promises that
    // unchecking restores what was there. Wiping on save would make that false:
    // a reader who checks the box, saves, and unchecks later would find the
    // folder silently gone. The runtime already ignores it for a hidden job.
    const body = buildBody(
      { ...parseJobDefaults(undefined), name: 'n', message: 'm', chatFolderId: 'home', hideInChat: true },
      'UTC',
      () => {},
    )
    expect(body?.chat_folder_id).toBe('home')
  })

  it('says the folder is kept, so the hint and the save agree', async () => {
    renderWithProviders(<JobForm layout="vertical" agents={[]} onSaved={() => {}} />)
    await screen.findByLabelText('Chat folder')
    await userEvent.click(screen.getByLabelText('Hide in chat'))
    expect(await screen.findByText(/The folder you pick is kept/)).toBeVisible()
  })

  it('submits nothing for a saved folder that has since been deleted', async () => {
    // The backend refuses a dangling id at save time, so left in state it rides
    // along on the next unrelated edit and turns a rename into a 400 the reader
    // cannot act on. The trigger already reads "Do not file runs" here, so
    // submitting '' makes the request agree with what they are looking at.
    const onSaved = vi.fn()
    vi.mocked(api.updateCron).mockResolvedValue({})
    renderWithProviders(
      <JobForm layout="vertical" agents={[]} onSaved={onSaved} job={makeJob({ chat_folder_id: 'deleted99' })} />,
    )
    const picker = await screen.findByLabelText('Chat folder')
    await waitFor(() => expect(picker).toHaveTextContent('Do not file runs'))
    await userEvent.type(screen.getByLabelText('Name'), ' v2')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledOnce())
    expect(api.updateCron).toHaveBeenCalledWith('cf1', expect.objectContaining({ chat_folder_id: '' }))
  })

  it('explains a saved folder that has been deleted', async () => {
    // The trigger reads "Do not file runs" here, because the saved id names no
    // folder the list offers. Without the line, a reader who opened the job to
    // change something unrelated sees a setting they never cleared.
    renderWithProviders(
      <JobForm layout="vertical" agents={[]} onSaved={() => {}} job={makeJob({ chat_folder_id: 'deleted99' })} />,
    )
    const line = await screen.findByText(/folder this job filed into was deleted/)
    expect(line).toBeVisible()
    // A reversion the SYSTEM made, so it must not be dressed as the hint under the
    // picker: in the hint's muted grey it reads as the reader's own choice.
    expect(line).toHaveAttribute('role', 'status')
    expect(line.className).toMatch(/text-warn/)
    expect(line.className).not.toMatch(/text-muted/)
  })

  it('shows no deleted-folder line for a folder that still exists', async () => {
    renderWithProviders(
      <JobForm layout="vertical" agents={[]} onSaved={() => {}} job={makeJob({ chat_folder_id: 'home' })} />,
    )
    const picker = await screen.findByLabelText('Chat folder')
    await waitFor(() => expect(picker).toHaveTextContent('Home'))
    expect(screen.queryByText(/folder this job filed into was deleted/)).not.toBeInTheDocument()
  })

  it('does not clear a saved folder just because the list failed to load', async () => {
    // While the fetch is failing every id looks missing, and clearing on that
    // would unfile a job for being offline.
    vi.mocked(api.chatFolders).mockRejectedValue(new Error('offline'))
    const onSaved = vi.fn()
    vi.mocked(api.updateCron).mockResolvedValue({})
    renderWithProviders(
      <JobForm layout="vertical" agents={[]} onSaved={onSaved} job={makeJob({ chat_folder_id: 'home' })} />,
    )
    expect(await screen.findByText(/Could not load your chat folders/)).toBeVisible()
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(onSaved).toHaveBeenCalledOnce())
    expect(api.updateCron).toHaveBeenCalledWith('cf1', expect.objectContaining({ chat_folder_id: 'home' }))
  })

  it('says where folders come from when there are none yet', async () => {
    // An empty tree is not an error, but it IS a dead end without this: the picker
    // offers nothing and the reader has no idea where one is created. The notice
    // must also keep them ON the form: telling them to go make a folder first
    // would cost the name, message and schedule they have drafted.
    vi.mocked(api.chatFolders).mockResolvedValue([])
    renderWithProviders(<JobForm layout="vertical" agents={[]} onSaved={() => {}} />)
    const notice = await screen.findByText(/Save this job now/)
    expect(notice).toBeVisible()
    expect(notice).toHaveTextContent(/chat sidebar/)
  })

  it('does not tell the reader to reload while their input is unsaved', async () => {
    // "Reload to pick one" beside unsaved Name and Message means obeying the
    // notice discards what they typed.
    vi.mocked(api.chatFolders).mockRejectedValue(new Error('offline'))
    renderWithProviders(<JobForm layout="vertical" agents={[]} onSaved={() => {}} />)
    const notice = await screen.findByText(/Could not load your chat folders/)
    expect(notice).toBeVisible()
    expect(notice.textContent).not.toMatch(/reload/i)
    expect(notice.textContent).toMatch(/Everything else on this form still saves/)
  })

  it('offers a retry that refetches the list in place, keeping the draft', async () => {
    // The same shape as the agent roster's retry in this form: one query is
    // refetched, nothing is reloaded, and what the reader typed stays put.
    vi.mocked(api.chatFolders).mockRejectedValueOnce(new Error('offline')).mockResolvedValue(FOLDERS)
    renderWithProviders(<JobForm layout="vertical" agents={[]} onSaved={() => {}} />)
    await userEvent.type(screen.getByLabelText('Name'), 'standup brief')
    const retry = await screen.findByRole('button', { name: 'Retry' })
    const callsBefore = vi.mocked(api.chatFolders).mock.calls.length

    await userEvent.click(retry)

    await waitFor(() => expect(screen.queryByText(/Could not load your chat folders/)).toBeNull())
    expect(api.chatFolders).toHaveBeenCalledTimes(callsBefore + 1)
    expect(screen.getByLabelText('Name')).toHaveValue('standup brief')
    await userEvent.click(screen.getByLabelText('Chat folder'))
    expect(await screen.findByText('Work › Standups')).toBeInTheDocument()
  })

  it('shows the retry as busy while the refetch is in flight', async () => {
    let release: (folders: ChatFolder[]) => void = () => {}
    vi.mocked(api.chatFolders)
      .mockRejectedValueOnce(new Error('offline'))
      .mockImplementationOnce(() => new Promise(resolve => { release = resolve }))
    renderWithProviders(<JobForm layout="vertical" agents={[]} onSaved={() => {}} />)
    await userEvent.click(await screen.findByRole('button', { name: 'Retry' }))

    // The query has no data, so it goes back to pending while it refetches and
    // the error state drops -- the notice would vanish mid-retry with nothing
    // saying why. It stays, with the button reporting the retry instead.
    const busy = await screen.findByRole('button', { name: 'Retrying…' })
    expect(busy).toBeDisabled()
    expect(busy).toHaveAttribute('aria-busy', 'true')
    expect(screen.getByText(/Could not load your chat folders/)).toBeVisible()
    release(FOLDERS)
    await waitFor(() => expect(screen.queryByText(/Could not load your chat folders/)).toBeNull())
  })

  it('says the folder list failed rather than showing an empty one', async () => {
    // An empty list is indistinguishable from the real empty tree, so the reader
    // would conclude they need to create a folder they already have.
    vi.mocked(api.chatFolders).mockRejectedValue(new Error('offline'))
    renderWithProviders(<JobForm layout="vertical" agents={[]} onSaved={() => {}} />)
    expect(await screen.findByText(/Could not load your chat folders/)).toBeVisible()
  })
})
