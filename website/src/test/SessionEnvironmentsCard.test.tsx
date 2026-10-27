import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { Provider } from 'react-redux'
import { createTestStore } from './helpers'
import { http, HttpResponse } from 'msw'
import { server } from '../../integration/mocks/server'
import { SessionEnvironmentsCard } from '../pages/settings/SessionEnvironmentsCard'
import { FeaturePreviewsSection } from '../pages/settings/FeaturePreviewsSection'
import { PREVIEW_SESSION_ENVIRONMENTS } from '../utils/previewFlags'
import { settingEntryOffered } from '../components/commandPalette/settingsSearchCore'
import { SETTINGS_REGISTRY } from '../components/commandPalette/settingsRegistry.gen'

vi.mock('../pages/settings/DecisionsCard', () => ({ DecisionsCard: () => null }))

const profile = { id: 'dev', name: 'Development', template: 'python-dev', workdir: '/workspace/repo' }
interface Section {
  coder_url: string
  profiles: typeof profile[]
  default_profile?: string
}
let stored: { session_environments?: Section }
let reads: number
let writes: unknown[]
let qc: QueryClient

function mount(store = createTestStore()) {
  localStorage.setItem(PREVIEW_SESSION_ENVIRONMENTS, '1')
  qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(<Provider store={store}><QueryClientProvider client={qc}><MemoryRouter><SessionEnvironmentsCard /><Probe /></MemoryRouter></QueryClientProvider></Provider>)
}

/** Where the card navigated to. */
let location = ''
function Probe() {
  location = useLocation().pathname + useLocation().search
  return null
}

beforeEach(() => {
  localStorage.clear()
  reads = 0
  writes = []
  stored = { session_environments: { coder_url: 'https://coder.example.test', profiles: [{ ...profile }] } }
  server.use(
    http.get('*/api/config/kirocrew', () => { reads++; return HttpResponse.json(stored) }),
    http.patch('*/api/config/kirocrew', async ({ request }) => {
      const body = await request.json() as { path: string; value: Section }
      writes.push(body)
      stored = { session_environments: body.value }
      return HttpResponse.json(stored)
    }),
    http.get('*/api/session-environments/templates', () => HttpResponse.json({ templates: [] })),
    http.get('*/api/session-environments/workspaces', () => HttpResponse.json({ workspaces: [] })),
  )
})
afterEach(() => { cleanup(); qc?.clear(); localStorage.clear(); vi.useRealTimers() })

const url = () => screen.getByLabelText('Coder URL')
const typeUrl = (value: string) => fireEvent.change(url(), { target: { value } })
const save = () => screen.getByRole('button', { name: 'Save settings' })
const toggle = () => screen.getByRole('switch', { name: 'Session Environments' })
/** Saved profiles start collapsed; open one by its header (name, then subtitle). */
const expand = (title: string) => fireEvent.click(screen.getByRole('button', { name: new RegExp(`^${title}`) }))
const expandAll = () => screen.getAllByRole('button', { expanded: false }).forEach(header => fireEvent.click(header))
const SAVED = 'Settings saved.'
const LOAD_ERROR = 'Could not load environment settings.'
const check = () => screen.getByRole('button', { name: 'Check Coder connection' })
const savedSection = (value: object) => expect(writes).toEqual([{ path: 'session_environments', value }])
const configReads = (fail: boolean) => server.use(http.get('*/api/config/kirocrew', () =>
  fail ? HttpResponse.json({ error: 'offline' }, { status: 500 }) : HttpResponse.json(stored)))
const checkAnswers = (body: object, status = 200) =>
  server.use(http.post('*/api/session-environments/check', () => HttpResponse.json(body, { status })))
async function mounted() {
  const view = mount()
  await screen.findByLabelText('Coder URL')
  return view
}

describe('Session Environments configuration preview', () => {
  it('indexes the opt-in but hides the URL search result until enabled', () => {
    const governance = { decisionsEnabled: true, tipsEnabled: true }
    const gate = SETTINGS_REGISTRY.find(entry => entry.id === 'developer.session-environments')!
    const connection = SETTINGS_REGISTRY.find(entry => entry.configKey === 'session_environments.coder_url')!
    expect(gate).toBeDefined()
    expect(connection).toBeDefined()
    expect(connection.tab).toBe('session-environments')
    expect(settingEntryOffered(gate, governance)).toBe(true)
    expect(settingEntryOffered(connection, governance)).toBe(false)
    localStorage.setItem(PREVIEW_SESSION_ENVIRONMENTS, '1')
    expect(settingEntryOffered(connection, governance)).toBe(true)
  })

  it('withholds every row of the preview tab, including one with no configKey', () => {
    const governance = { decisionsEnabled: true, tipsEnabled: true, updateSwitches: { app: true, gateway: true } }
    const picker = SETTINGS_REGISTRY.find(entry => entry.id === 'session-environments.default-for-new-chats')!
    expect(picker.configKey).toBeUndefined()
    const rows = SETTINGS_REGISTRY.filter(entry => entry.tab === 'session-environments')
    expect(rows.length).toBeGreaterThan(1)
    for (const row of rows) expect(settingEntryOffered(row, governance)).toBe(false)
    localStorage.setItem(PREVIEW_SESSION_ENVIRONMENTS, '1')
    for (const row of rows) expect(settingEntryOffered(row, governance)).toBe(true)
  })

  it('keeps only the opt-in in Developer, without reading or writing configuration', () => {
    qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<QueryClientProvider client={qc}><MemoryRouter><FeaturePreviewsSection /></MemoryRouter></QueryClientProvider>)
    expect(toggle()).not.toBeChecked()
    expect(screen.queryByRole('textbox', { name: 'Coder URL' })).not.toBeInTheDocument()
    fireEvent.click(toggle())
    expect(toggle()).toBeChecked()
    expect(screen.queryByRole('textbox', { name: 'Coder URL' })).not.toBeInTheDocument()
    expect(reads).toBe(0)
    expect(writes).toEqual([])
    expect(localStorage.getItem(PREVIEW_SESSION_ENVIRONMENTS)).toBe('1')
  })

  it('writes the complete section only on Save and reloads saved profiles', async () => {
    const first = await mounted()
    typeUrl('https://new.example.test')
    expand('Development')
    fireEvent.change(screen.getByLabelText('Coder template'), { target: { value: 'node-dev' } })
    expect(writes).toEqual([])
    fireEvent.click(save())
    expect(await screen.findByText(SAVED)).toBeVisible()
    savedSection({ coder_url: 'https://new.example.test', profiles: [{ ...profile, template: 'node-dev' }] })
    expect(save()).toBeDisabled()
    first.unmount(); qc.clear()
    mount()
    expect(await screen.findByLabelText('Coder URL')).toHaveValue('https://new.example.test')
    expand('Development')
    expect(screen.getByLabelText('Coder template')).toHaveValue('node-dev')
  })

  it('marks every chat\'s cached placement stale after a save, so new edits use the saved folder', async () => {
    await mounted()
    qc.setQueryData(['slot-environment', 'chat-9'], { profile: 'dev', profiles: [{ ...profile }] })
    expand('Development')
    fireEvent.change(screen.getByLabelText('Working folder'), { target: { value: '/workspace/other' } })
    fireEvent.click(save())
    expect(await screen.findByText(SAVED)).toBeVisible()
    expect(qc.getQueryState(['slot-environment', 'chat-9'])?.isInvalidated).toBe(true)
  })

  it('round-trips a per-profile "files survive a stop" switch, on by default', async () => {
    const first = await mounted()
    expand('Development')
    const survives = () => screen.getByRole('switch', { name: 'This template keeps its files when a workspace stops' })
    expect(survives()).toBeChecked()
    // It says what happens to the files either way, that it wins over the global
    // shutdown switch, and what to do when unsure.
    expect(screen.getByText(/Off: files are lost when a workspace stops, so .* never stops them, even when “Stop workspaces when .* shuts down” below is on/)).toBeInTheDocument()
    expect(screen.getByText("Not sure? Leave it on unless this template's files are lost when a workspace stops.")).toBeInTheDocument()
    fireEvent.click(survives())
    expect(survives()).not.toBeChecked()
    fireEvent.click(save())
    expect(await screen.findByText(SAVED)).toBeVisible()
    savedSection({ coder_url: 'https://coder.example.test', profiles: [{ ...profile, compute_survives_stop: false }] })
    first.unmount(); qc.clear()
    await mounted()
    expand('Development')
    expect(survives()).not.toBeChecked()
  })

  it('offers no retention control: archived workspaces are kept a fixed week', async () => {
    stored.session_environments = { ...stored.session_environments!, archived_retention_days: 6 } as Section
    await mounted()
    expect(screen.queryByRole('button', { name: 'Increase' })).not.toBeInTheDocument()
    expect(screen.queryByText(/archived workspaces/i)).not.toBeInTheDocument()
  })

  it('keeps edits across refetch and discards explicitly', async () => {
    await mounted()
    typeUrl('https://draft.example.test')
    act(() => qc.setQueryData(['kirocrewConfig'], { session_environments: { coder_url: 'https://other.example.test', profiles: [] } }))
    expect(url()).toHaveValue('https://draft.example.test')
    fireEvent.click(screen.getByRole('button', { name: 'Discard changes' }))
    expect(url()).toHaveValue('https://other.example.test')
    expect(writes).toEqual([])
  })

  it('offers the deployment templates in the template field, and still takes a typed name', async () => {
    server.use(http.get('*/api/session-environments/templates', () => HttpResponse.json({ templates: ['gpu', 'python-dev'] })))
    await mounted()
    fireEvent.click(screen.getByRole('button', { name: 'Add Coder profile' }))
    // Open the saved profile by its header; expandAll would also open the pickers.
    expand('Development')
    const pickers = await screen.findAllByRole('button', { name: 'Coder template' })
    expect(pickers).toHaveLength(2)
    expect(pickers[1]).toHaveTextContent('Choose a template')
    fireEvent.click(pickers[1])
    expect(await screen.findByRole('option', { name: 'python-dev' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('option', { name: 'gpu' }))
    await waitFor(() => expect(screen.queryByRole('listbox')).toBeNull())
    expect(screen.getAllByRole('button', { name: 'Coder template' })[1]).toHaveTextContent('gpu')
    // Free text stays available for a template the list does not show.
    fireEvent.click(screen.getAllByRole('button', { name: 'Coder template' })[1])
    fireEvent.change(await screen.findByLabelText('Search or type a template name'), { target: { value: 'custom-dev' } })
    fireEvent.click(await screen.findByRole('option', { name: 'Use "custom-dev"' }))
    fireEvent.click(save())
    expect(await screen.findByText(SAVED)).toBeVisible()
    expect(writes[0].value.profiles.map(p => p.template)).toEqual(['python-dev', 'custom-dev'])
  })

  it('keeps the template field as free text when the list cannot be read', async () => {
    server.use(http.get('*/api/session-environments/templates', () => HttpResponse.json({ templates: [], error: 'coder: not logged in' })))
    await mounted()
    expandAll()
    expect(screen.getByLabelText('Coder template')).toHaveValue(profile.template)
    expect(screen.queryByRole('button', { name: 'Coder template' })).toBeNull()
    // Says why, under the field, with no hand-off (the card may hold a draft).
    const notice = await screen.findByText("Couldn't load your Coder templates, so type the template's name.")
    expect(within(notice.closest('[role="alert"]') as HTMLElement).queryByRole('button', { name: /Ask the agent/ })).toBeNull()
    // Coder's own words are kept for diagnosing, folded under Details, not in the sentence.
    expect(notice).not.toHaveTextContent('not logged in')
    const raw = screen.getByText('coder: not logged in')
    expect(raw.closest('details')).not.toBeNull()
  })

  it('says a failed refresh even while cached templates keep the picker', async () => {
    server.use(http.get('*/api/session-environments/templates', () => HttpResponse.json({ templates: ['gpu', 'python-dev'] })))
    await mounted()
    expand('Development')
    expect(await screen.findByRole('button', { name: 'Coder template' })).toBeInTheDocument()
    expect(screen.queryByText(/Couldn't load your Coder templates/)).toBeNull()
    server.use(http.get('*/api/session-environments/templates', () => HttpResponse.json({ error: 'gateway down' }, { status: 500 })))
    await act(async () => { await qc!.invalidateQueries({ queryKey: ['sessionEnvironmentsTemplates'] }) })
    expect(await screen.findByText(/Couldn't load your Coder templates/)).toBeInTheDocument()
    // The cached choices stay usable, and typing a name still works.
    expect(screen.getByRole('button', { name: 'Coder template' })).toBeInTheDocument()
  })

  it('says so when the template list request itself fails', async () => {
    server.use(http.get('*/api/session-environments/templates', () => HttpResponse.json({ error: 'gateway down' }, { status: 500 })))
    await mounted()
    expandAll()
    expect(await screen.findByText(/Couldn't load your Coder templates/)).toBeInTheDocument()
    expect(screen.getByLabelText('Coder template')).toHaveValue(profile.template)
  })

  it('adds unique profile drafts and removes them without provisioning', async () => {
    stored.session_environments!.profiles.push({ ...profile, id: 'profile-1' })
    await mounted()
    fireEvent.click(screen.getByRole('button', { name: 'Add Coder profile' }))
    expandAll()
    expect(screen.getAllByLabelText('Profile ID').map(input => (input as HTMLInputElement).value)).toEqual(['dev', 'profile-1', 'profile-2'])
    fireEvent.click(screen.getAllByRole('button', { name: 'Remove profile' })[2])
    expect(screen.getAllByLabelText('Profile ID')).toHaveLength(2)
    expect(writes).toEqual([])
  })

  it('says which live chats a removed profile strands, before saving', async () => {
    // Named as the list shows it, with the ID the chats are bound by.
    const WARN = /Chats still on this profile: 1\. Once you save, they can't run until Development \(ID dev\) is added back\./
    server.use(http.get('*/api/session-environments/workspaces', () => HttpResponse.json({ workspaces: [
      { workspace: 'crewenv-live', slot: 'chat-live', profile: 'dev', state: 'running', template: 't', created_at: '2026-10-01T12:00:00Z', deletion_due_at: '' },
      // Archived and other-profile rows strand nothing new.
      { workspace: 'crewenv-old', slot: 'chat-old', profile: 'dev', state: 'stopped', template: 't', created_at: '2026-10-01T12:00:00Z', deletion_due_at: '2026-10-30T12:00:00Z' },
      { workspace: 'crewenv-gpu', slot: 'chat-gpu', profile: 'gpu', state: 'running', template: 't', created_at: '2026-10-01T12:00:00Z', deletion_due_at: '' },
    ] })))
    await mounted()
    await screen.findByRole('link', { name: 'crewenv-live' })
    expect(screen.queryByText(WARN)).toBeNull()
    expandAll()
    fireEvent.click(screen.getByRole('button', { name: 'Remove profile' }))
    expect(await screen.findByText(WARN)).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Discard changes' }))
    expect(screen.queryByText(WARN)).toBeNull()
  })

  it('says it could not check a removed profile\'s chats when the workspace list fails', async () => {
    const UNKNOWN = /Could not check which chats still use Development \(ID dev\)\./
    server.use(http.get('*/api/session-environments/workspaces', () =>
      HttpResponse.json({ error: 'the workspace registry could not be read', code: 'workspaces_unreadable' }, { status: 500 })))
    await mounted()
    await screen.findAllByText('The workspace list could not be read.')
    // The failed read is not "no chats on it": no count is claimed either way.
    expect(screen.queryByText(/Chats still on this profile/)).toBeNull()
    expect(screen.queryByText(UNKNOWN)).toBeNull()
    expandAll()
    fireEvent.click(screen.getByRole('button', { name: 'Remove profile' }))
    expect(await screen.findByText(UNKNOWN)).toBeVisible()
    expect(screen.queryByText(/Chats still on this profile/)).toBeNull()
    expect(screen.queryByText(/workspace registry could not be read/)).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Discard changes' }))
    expect(screen.queryByText(UNKNOWN)).toBeNull()
  })

  it('names a removed profile that has no name by its ID alone', async () => {
    stored.session_environments!.profiles[0] = { ...profile, name: '' }
    server.use(http.get('*/api/session-environments/workspaces', () =>
      HttpResponse.json({ error: 'the workspace registry could not be read', code: 'workspaces_unreadable' }, { status: 500 })))
    await mounted()
    await screen.findAllByText('The workspace list could not be read.')
    expandAll()
    fireEvent.click(screen.getByRole('button', { name: 'Remove profile' }))
    expect(await screen.findByText(/Could not check which chats still use dev\. After/)).toBeVisible()
  })

  it('defines a profile once, in the intro, not again under Profiles', async () => {
    await mounted()
    expect(screen.getAllByText(/A profile is a saved setup/)).toHaveLength(1)
    expect(screen.getByText(/Each profile is a Coder template plus a working folder/)).toBeInTheDocument()
  })

  it('says a removal is undone by Discard changes, until it is saved', async () => {
    const UNDO = 'Removed. Discard changes to undo.'
    await mounted()
    expandAll()
    fireEvent.click(screen.getByRole('button', { name: 'Remove profile' }))
    expect(screen.getByRole('status')).toHaveTextContent(UNDO)
    fireEvent.click(screen.getByRole('button', { name: 'Discard changes' }))
    expect(screen.queryByText(UNDO)).toBeNull()
    expect(screen.getByRole('button', { name: /^Development/ })).toBeInTheDocument()
    expandAll()
    fireEvent.click(screen.getByRole('button', { name: 'Remove profile' }))
    fireEvent.click(save())
    expect(await screen.findByText(SAVED)).toBeVisible()
    expect(screen.queryByText(UNDO)).toBeNull()
    savedSection({ coder_url: 'https://coder.example.test', profiles: [] })
  })

  it('retains a rejected draft and shows the backend validation detail', async () => {
    server.use(http.patch('*/api/config/kirocrew', () => HttpResponse.json({ error: 'profiles[0].id is a duplicate' }, { status: 400 })))
    await mounted()
    typeUrl('https://draft.example.test')
    fireEvent.click(save())
    expect(await screen.findByText('Could not save the settings: profiles[0].id is a duplicate')).toBeVisible()
    expect(url()).toHaveValue('https://draft.example.test')
    expect(save()).toBeEnabled()
    expect(screen.queryByText(SAVED)).not.toBeInTheDocument()
  })

  it('marks the Coder URL box when the gateway refuses the URL, and only then', async () => {
    server.use(http.patch('*/api/config/kirocrew', () => HttpResponse.json({ error: 'coder_url must include a hostname' }, { status: 400 })))
    await mounted()
    expect(url()).not.toHaveAttribute('aria-invalid')
    typeUrl('https://')
    fireEvent.click(save())
    // The banner points at the box; under it, plain words say what to enter.
    await screen.findByText('Could not save the settings: fix the Coder URL above.')
    expect(url()).toHaveAttribute('aria-invalid', 'true')
    const describedBy = url().getAttribute('aria-describedby') ?? ''
    const reason = document.getElementById(describedBy.split(' ').pop()!)!
    expect(reason).toHaveTextContent('Enter the full address of your Coder deployment, for example https://coder.example.com.')
    expect(reason).not.toHaveTextContent('coder_url')
    // The gateway's own words are said once, folded under Details.
    expect(screen.getAllByText(/coder_url must include a hostname/)).toHaveLength(1)
    expect(screen.getByText('coder_url must include a hostname').closest('details')).not.toBeNull()
    // Editing clears the refusal and the mark with it.
    typeUrl('https://coder.example.test')
    expect(url()).not.toHaveAttribute('aria-invalid')
  })

  it('does not fabricate settings support on an older gateway', async () => {
    stored = {}
    mount()
    expect(await screen.findByText(/This version of Kiro Crew does not support Session Environments settings/)).toBeVisible()
    expect(screen.queryByLabelText('Coder URL')).not.toBeInTheDocument()
    expect(writes).toEqual([])
  })

  it('retries loading after a failed configuration read', async () => {
    configReads(true)
    mount()
    expect(await screen.findByText(LOAD_ERROR)).toBeVisible()
    configReads(false)
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await screen.findByLabelText('Coder URL')
    expect(save()).toBeDisabled()
  })

  it.each([false, true])('reports failed refreshes without losing cached settings or drafts (draft=%s)', async draft => {
    await mounted()
    const value = draft ? 'https://draft.example.test' : stored.session_environments!.coder_url
    if (draft) fireEvent.change(url(), { target: { value } })
    configReads(true)
    await act(async () => { await qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }) })
    const notice = await screen.findByText(LOAD_ERROR)
    expect(notice).toBeVisible()
    expect(url()).toHaveValue(value)
    expect(save().hasAttribute('disabled')).toBe(!draft)
    expect(within(notice.closest('[role="alert"]') as HTMLElement).queryByRole('button', { name: /Ask the agent/i })).not.toBeInTheDocument()
    configReads(false)
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(screen.queryByText(LOAD_ERROR)).not.toBeInTheDocument())
    expect(url()).toHaveValue(value)
    expect(writes).toEqual([])
    if (draft) {
      fireEvent.click(save())
      await screen.findByText(SAVED)
      savedSection({ ...stored.session_environments!, coder_url: value })
    }
  })

  it('freezes editing during a save and does not submit twice', async () => {
    let finish!: () => void
    const pending = new Promise<void>(resolve => { finish = resolve })
    server.use(http.patch('*/api/config/kirocrew', async ({ request }) => {
      writes.push(await request.json())
      await pending
      return HttpResponse.json(stored)
    }))
    await mounted()
    typeUrl('https://draft.example.test')
    fireEvent.click(save())
    await waitFor(() => expect(url()).toBeDisabled())
    expect(save()).toBeDisabled()
    await act(async () => finish())
    await screen.findByText(SAVED)
    expect(writes).toHaveLength(1)
  })
})

describe('Coder setup and saved profile management', () => {
  it('shows Coder setup without claiming a live connection', async () => {
    await mounted()
    // jsdom keeps closed <details> content in the tree, so assert the
    // disclosure state itself: instructions start collapsed.
    const instructions = screen.getByText('Setup instructions', { exact: true }).closest('details')!
    expect(instructions.open).toBe(false)
    instructions.open = true
    expect(screen.getByRole('link', { name: 'Open provider setup documentation' })).toHaveAttribute('href', 'https://coder.com/docs/admin/setup')
    // One provider, so there is no provider choice to make.
    expect(screen.queryByRole('radio')).not.toBeInTheDocument()
    // Where the "Run this chat on" menu is, said once on the card (under Profiles).
    expect(screen.getAllByText(/“Run this chat on” menu next to the message box/)).toHaveLength(1)
    expect(screen.getByText('Archiving a chat stops its workspace and deletes it 7 days later.')).toBeVisible()
    expect(writes).toEqual([])
  })

  it('collapses saved profiles, opens new ones, and keeps edits across collapse', async () => {
    await mounted()
    const header = screen.getByRole('button', { name: /^Development/ })
    expect(header).toHaveAttribute('aria-expanded', 'false')
    expect(header).toHaveTextContent('python-dev')
    expect(screen.queryByLabelText('Coder template')).not.toBeInTheDocument()
    fireEvent.click(header)
    expect(header).toHaveAttribute('aria-expanded', 'true')
    for (const hint of ['A short unique code: letters, digits, _ and - only. Chats remember it, so changing it later stops chats already on this profile.', 'Shown in the “Run this chat on” menu.', "Which kind of machine each chat gets: Coder creates the chat's workspace from this template, for example python-dev. Run coder templates list on this machine to see yours; Check Coder connection then confirms it works.", "Full path of the folder in the workspace where the chat's files and commands run. It starts with /, for example /workspace/project."]) expect(screen.getByText(hint)).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Coder template'), { target: { value: 'node-dev' } })
    fireEvent.click(header)
    await waitFor(() => expect(screen.queryByLabelText('Coder template')).not.toBeInTheDocument())
    expect(header).toHaveTextContent('node-dev')
    fireEvent.click(header)
    expect(screen.getByLabelText('Coder template')).toHaveValue('node-dev')
    fireEvent.click(screen.getByRole('button', { name: 'Add Coder profile' }))
    expect(screen.getByRole('button', { name: /^profile-1/ })).toHaveAttribute('aria-expanded', 'true')
    expect(writes).toEqual([])
  })

  it('keeps configure, verify and manage on one page without treating a save as readiness', async () => {
    await mounted()
    expect(screen.queryByRole('tablist')).not.toBeInTheDocument()
    for (const name of ['1. Connect and configure', '2. Verify readiness', '3. Manage']) {
      expect(screen.getByRole('region', { name })).toBeVisible()
    }
    typeUrl('https://changed.example.test')
    // An unsaved draft cannot be checked: the check reads what the gateway saved.
    expect(check()).toBeDisabled()
    expect(screen.getByText('Save first: the check uses the saved settings.')).toBeVisible()
    fireEvent.click(save())
    await screen.findByText(SAVED)
    expect(check()).toBeEnabled()
    expect(check()).toHaveAttribute('aria-describedby')
    // The controls that did nothing are gone.
    for (const name of ['Start session', 'Manage resources']) {
      expect(screen.queryByRole('button', { name })).not.toBeInTheDocument()
    }
    expect(writes).toHaveLength(1)
  })

  it('cannot check before a Coder URL is saved', async () => {
    stored = { session_environments: { coder_url: '', profiles: [] } }
    await mounted()
    expect(check()).toBeDisabled()
    // A disabled button says what turns it on.
    expect(check()).toHaveAccessibleDescription('Enter a Coder URL above, then save, to run the check.')
    expect(screen.queryByRole('link', { name: 'Open Coder' })).toBeNull()
  })

  it('picks a default environment, and drops it with the profile', async () => {
    const second = { ...profile, id: 'gpu', name: 'GPU box' }
    stored = { session_environments: { coder_url: 'https://coder.example.test', profiles: [{ ...profile }, second], default_profile: '' } }
    await mounted()
    // Global, after the numbered steps: off by default, no picker until it is on.
    const defaults = screen.getByRole('region', { name: 'Global defaults' })
    const manage = screen.getByRole('region', { name: '3. Manage' })
    expect(manage.compareDocumentPosition(defaults) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    const toggle = within(defaults).getByRole('switch', { name: 'Start new chats on a profile' })
    expect(toggle).not.toBeChecked()
    expect(within(defaults).queryByRole('combobox', { name: 'Default for new chats' })).toBeNull()
    fireEvent.click(toggle)
    const select = within(defaults).getByRole('combobox', { name: 'Default for new chats' })
    expect(select).toHaveTextContent('Development')
    fireEvent.click(select)
    fireEvent.click(await screen.findByRole('option', { name: 'GPU box' }))
    fireEvent.click(save())
    await waitFor(() => expect(writes).toHaveLength(1))
    expect((writes[0] as { value: Section }).value.default_profile).toBe('gpu')
    // Removing the default profile moves the default to the one left, and says so.
    expand('GPU box')
    const header = screen.getByRole('button', { name: /^GPU box/ })
    const panel = header.closest('div')!.parentElement!
    fireEvent.click(within(panel).getByRole('button', { name: 'Remove profile' }))
    expect(within(defaults).getByRole('switch', { name: 'Start new chats on a profile' })).toBeChecked()
    expect(within(defaults).getByRole('combobox', { name: 'Default for new chats' })).toHaveTextContent('Development')
    expect(defaults).toHaveTextContent('Now Development: the default profile was removed. Discard changes to undo.')
    fireEvent.click(save())
    await waitFor(() => expect(writes).toHaveLength(2))
    expect((writes[1] as { value: Section }).value).toMatchObject({ default_profile: 'dev', profiles: [profile] })
    expect(defaults).not.toHaveTextContent('the default profile was removed')
    // With no profile left, the default turns off, and the switch says why.
    expand('Development')
    const last = screen.getByRole('button', { name: /^Development/ }).closest('div')!.parentElement!
    fireEvent.click(within(last).getByRole('button', { name: 'Remove profile' }))
    expect(within(defaults).getByRole('switch', { name: 'Start new chats on a profile' })).not.toBeChecked()
    expect(defaults).toHaveTextContent('Turned off: its profile was removed. Discard changes to undo.')
    fireEvent.click(save())
    await waitFor(() => expect(writes).toHaveLength(3))
    expect((writes[2] as { value: Section }).value).toMatchObject({ default_profile: '', profiles: [] })
    expect(defaults).not.toHaveTextContent('Turned off: its profile was removed.')
  })

  it('carries the default along when its profile id is renamed', async () => {
    const second = { ...profile, id: 'gpu', name: 'GPU box' }
    stored = { session_environments: { coder_url: 'https://coder.example.test', profiles: [{ ...profile }, second], default_profile: 'gpu' } }
    await mounted()
    expand('GPU box')
    const panel = screen.getByRole('button', { name: /^GPU box/ }).closest('div')!.parentElement!
    fireEvent.change(within(panel).getByLabelText('Profile ID'), { target: { value: 'gpu-2' } })
    expect(screen.getByRole('combobox', { name: 'Default for new chats' })).toHaveTextContent('GPU box')
    // Clearing the id to retype it keeps the default for the retyped id.
    fireEvent.change(within(panel).getByLabelText('Profile ID'), { target: { value: '' } })
    fireEvent.change(within(panel).getByLabelText('Profile ID'), { target: { value: 'gpu-3' } })
    // Renaming another profile leaves the default where it is.
    expand('Development')
    const first = screen.getByRole('button', { name: /^Development/ }).closest('div')!.parentElement!
    fireEvent.change(within(first).getByLabelText('Profile ID'), { target: { value: 'dev-2' } })
    fireEvent.click(save())
    await waitFor(() => expect(writes).toHaveLength(1))
    expect((writes[0] as { value: Section }).value).toMatchObject({
      default_profile: 'gpu-3', profiles: [{ ...profile, id: 'dev-2' }, { ...second, id: 'gpu-3' }],
    })
  })

  it('disables the shutdown switch until a profile exists', async () => {
    stored = { session_environments: { coder_url: 'https://coder.example.test', profiles: [], default_profile: '', stop_on_shutdown: true } as Section }
    await mounted()
    const stop = () => screen.getByRole('switch', { name: 'Stop workspaces when Kiro Crew shuts down' })
    // Shown off while it cannot apply, so it never reads as "on and locked".
    expect(stop()).toHaveAttribute('aria-disabled', 'true')
    expect(screen.getAllByText('Save a profile above to turn this on.')).toHaveLength(2)
    fireEvent.click(stop())
    expect(stop()).not.toBeChecked()
    fireEvent.click(screen.getByRole('button', { name: 'Add Coder profile' }))
    expect(stop()).not.toHaveAttribute('aria-disabled', 'true')
    expect(screen.getByText(/Off: they keep running, so chats reconnect faster/)).toBeInTheDocument()
    expect(stop()).toBeChecked()
    expect(writes).toEqual([])
  })
})

describe('Session Environments readiness and workspaces', () => {
  it('runs the read-only check and reports Coder and each template', async () => {
    let checks = 0
    server.use(http.post('*/api/session-environments/check', () => {
      checks++
      return HttpResponse.json({
        coder: { ok: true, owner: 'devuser' },
        profiles: [{ id: 'dev', ok: false, error: 'Coder template is unavailable' }],
      })
    }))
    await mounted()
    fireEvent.click(check())
    const results = await screen.findByRole('list', { name: 'Check results' })
    expect(checks).toBe(1)
    expect(results).toHaveTextContent('Signed in to Coder as devuser')
    expect(results).toHaveTextContent('Development: Coder template is unavailable')
    const failure = within(results).getByRole('alert')
    expect(failure).toHaveTextContent('Development: Coder template is unavailable')
    expect(within(failure).getByRole('button', { name: /Ask the agent/ })).toBeVisible()
    expect(writes).toEqual([])
  })

  it('surfaces failed Coder checks with a safe hand-off and drops them while editing', async () => {
    checkAnswers({ coder: { ok: false, error: 'Coder login expired' }, profiles: [] })
    await mounted()
    fireEvent.click(check())
    const results = await screen.findByRole('list', { name: 'Check results' })
    const failure = within(results).getByRole('alert')
    // What to do leads; the raw detail follows.
    expect(failure).toHaveTextContent(/^The Coder check failed\. Make sure the Coder CLI is installed and signed in on this machine, then check again\./)
    // The CLI's raw words are folded under Details, not repeated in red.
    expect(failure).not.toHaveTextContent('Coder login expired')
    const details = within(results).getByText('Details').closest('details')!
    expect(details).not.toHaveAttribute('open')
    expect(details).toHaveTextContent('Coder login expired')
    const command = within(results).getByText('coder login')
    expect(command.tagName).toBe('CODE')
    expect(command.closest('p')).toHaveTextContent('To sign in, run coder login in a terminal on this machine.')
    expect(within(failure).getByRole('button', { name: /Ask the agent/ })).toBeVisible()
    typeUrl('https://draft.example.test')
    expect(screen.queryByRole('list', { name: 'Check results' })).toBeNull()
  })

  it.each([[[{ id: 'dev', ok: true }]], [[]]])('marks templates not checked when Coder sign-in fails (%j)', async rows => {
    checkAnswers({ coder: { ok: false, error: 'Coder login expired' }, profiles: rows })
    await mounted()
    fireEvent.click(check())
    const results = await screen.findByRole('list', { name: 'Check results' })
    expect(results).toHaveTextContent('Development: not checked until Coder sign-in works')
    expect(results).not.toHaveTextContent(/is available/)
  })

  it('reports a check that could not run through the error notice', async () => {
    checkAnswers({ error: 'x' }, 500)
    await mounted()
    fireEvent.click(check())
    expect(await screen.findByText('The check could not run.')).toBeVisible()
    // An edit makes the answer stale and starts an unsaved draft: the notice,
    // and its hand-off that would leave the page, go away.
    typeUrl('https://draft.example.test')
    expect(screen.queryByText('The check could not run.')).toBeNull()
  })

  it('offers no hand-off on a failed workspace list while a draft is unsaved', async () => {
    server.use(http.get('*/api/session-environments/workspaces', () => HttpResponse.json({ error: 'x' }, { status: 500 })))
    await mounted()
    const notice = (await screen.findByText('The workspace list could not be read.')).closest('[role="alert"]') as HTMLElement
    expect(within(notice).getByRole('button', { name: /Ask the agent/ })).toBeVisible()
    typeUrl('https://draft.example.test')
    const after = screen.getByText('The workspace list could not be read.').closest('[role="alert"]') as HTMLElement
    expect(within(after).queryByRole('button', { name: /Ask the agent/ })).toBeNull()
  })

  it('lists the workspaces Kiro Crew owns, with when an archived one is deleted', async () => {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date('2026-10-12T10:00:00Z'))
    const gone = { slot: 'chat-4', state: 'stopped', template: 't', created_at: '2026-10-01T12:00:00Z' }
    server.use(http.get('*/api/session-environments/workspaces', () => HttpResponse.json({
      workspaces: [
        { workspace: 'crewenv-devuser-aaaa', slot: 'chat-1', state: 'running', template: 't', created_at: '2026-10-09T10:00:00Z', deletion_due_at: '' },
        { workspace: 'crewenv-devuser-bbbb', slot: 'chat-2', state: 'running', template: 't', created_at: '2026-10-09T11:00:00Z', deletion_due_at: '2026-10-16T11:00:00Z' },
        { workspace: 'crewenv-devuser-cccc', slot: 'chat-3', state: 'deleting', template: 't', created_at: '2026-10-09T12:00:00Z', deletion_due_at: '' },
        { ...gone, workspace: 'later-today', deletion_due_at: '2026-10-12T22:00:00Z' },
        { ...gone, workspace: 'overdue', deletion_due_at: '2026-10-08T12:00:00Z' },
      ],
    })))
    mount()
    const list = await screen.findByRole('list', { name: 'Workspaces' })
    vi.useRealTimers()
    const rows = list.querySelectorAll('li')
    expect(rows).toHaveLength(5)
    expect(rows[0]).toHaveTextContent('crewenv-devuser-aaaa')
    expect(rows[0]).toHaveTextContent('In use by a chat')
    expect(rows[0]).not.toHaveTextContent('Archived')
    // An archived row is not "in use": it says when it goes instead.
    expect(rows[1]).toHaveTextContent(/Archived\. Will be deleted on/)
    expect(rows[1]).not.toHaveTextContent('In use by a chat')
    expect(rows[2]).toHaveTextContent('Being deleted')
    expect(rows[2]).not.toHaveTextContent('In use by a chat')
    // A due row still gives its date, so the two archived wordings read as one stage.
    // Later today still reads as a date; a passed one says it is overdue, and how to keep it.
    expect(rows[3]).toHaveTextContent(/Archived\. Will be deleted on .*2026/)
    expect(rows[4]).toHaveTextContent(/^.*Archived\. Will be deleted soon\. To keep it, reopen the chat and send a message; that starts the workspace again\.Reopen chat$/)
    // Every archived row says how keeping works: reopening alone is not enough.
    expect(rows[1]).toHaveTextContent('To keep it, reopen the chat and send a message')
    expect(within(rows[0]).getByRole('link', { name: 'crewenv-devuser-aaaa' })).toHaveAttribute('href', '/chat?sid=chat-1')
    expect(within(rows[1]).queryByRole('link')).toBeNull()
    // An archived row offers the step that keeps it; a deleting or live one does not.
    expect(within(rows[1]).getByRole('button', { name: 'Reopen chat' })).toBeVisible()
    expect(within(rows[0]).queryByRole('button', { name: 'Reopen chat' })).toBeNull()
    expect(within(rows[2]).queryByRole('button', { name: 'Reopen chat' })).toBeNull()
    // The Coder deployment is one click away, opened safely in a new tab.
    const link = screen.getByRole('link', { name: 'Open Coder' })
    expect(link).toHaveAttribute('href', 'https://coder.example.test')
    expect(link).toHaveAttribute('rel', 'noopener noreferrer')
  })

  const archived = { workspace: 'crewenv-devuser-dddd', slot: 'chat-9', state: 'stopped', template: 't', created_at: '2026-10-01T12:00:00Z', deletion_due_at: '2026-10-16T11:00:00Z' }

  it('reopens an archived chat from its transcript key and opens the returned slot', async () => {
    let resumedPath = ''
    let resumedBody: { key?: string } = {}
    server.use(
      http.get('*/api/session-environments/workspaces', () => HttpResponse.json({ workspaces: [archived] })),
      http.post('*/api/chat/slots/:key/resume', async ({ params, request }) => {
        resumedPath = String(params.key)
        resumedBody = await request.json() as { key?: string }
        return HttpResponse.json({ ok: true, key: 'chat-9', mode: 'default', messages: [] })
      }),
    )
    mount()
    fireEvent.click(await screen.findByRole('button', { name: 'Reopen chat' }))
    await waitFor(() => expect(location).toBe('/chat?sid=chat-9'))
    // The bare slot would read `chat-9.jsonl` and open an empty chat; the
    // history key must be the canonical `dashboard:` one.
    expect(resumedBody.key).toBe('dashboard:chat-9')
    expect(resumedPath).toBe('dashboard:chat-9')
  })

  it('reopens an archived mate thread on its crewmate page, not the chat page', async () => {
    let resumedKey = ''
    server.use(
      http.get('*/api/session-environments/workspaces', () => HttpResponse.json({ workspaces: [{ ...archived, slot: 'member-radar' }] })),
      http.post('*/api/chat/slots/:key/resume', async ({ request }) => {
        resumedKey = String((await request.json() as { key?: string }).key)
        return HttpResponse.json({ ok: true, key: 'member-radar', mode: 'member', surface: 'member', messages: [] })
      }),
    )
    mount()
    fireEvent.click(await screen.findByRole('button', { name: 'Reopen chat' }))
    // The chat page cannot show a mate's thread, so it opens where that thread lives.
    await waitFor(() => expect(location).toBe('/members?member=radar'))
    expect(resumedKey).toBe('dashboard:member-radar')
  })

  it('stays on Settings when edits start while a reopen is still running', async () => {
    let release: () => void = () => {}
    server.use(
      http.get('*/api/session-environments/workspaces', () => HttpResponse.json({ workspaces: [archived] })),
      http.post('*/api/chat/slots/:key/resume', async () => {
        await new Promise<void>(resolve => { release = resolve })
        return HttpResponse.json({ ok: true, key: 'chat-9', mode: 'default', messages: [] })
      }),
    )
    await mounted()
    fireEvent.click(await screen.findByRole('button', { name: 'Reopen chat' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Reopen chat' })).toBeDisabled())
    typeUrl('https://draft.example.test')
    act(() => release())
    // Reopened, but leaving would drop the draft typed meanwhile: it says so instead.
    expect(await screen.findByText('Reopened. Save or discard your changes, then open it from your chats.')).toBeVisible()
    expect(location).not.toContain('/chat')
    expect(screen.getByLabelText('Coder URL')).toHaveValue('https://draft.example.test')
  })

  it('says so when an archived chat cannot be reopened, and waits for a draft to be saved', async () => {
    server.use(
      http.get('*/api/session-environments/workspaces', () => HttpResponse.json({ workspaces: [archived] })),
      http.post('*/api/chat/slots/:key/resume', () => HttpResponse.json({ error: 'gone' }, { status: 404 })),
    )
    await mounted()
    fireEvent.click(await screen.findByRole('button', { name: 'Reopen chat' }))
    const failed = await screen.findByText('Could not reopen that chat.')
    expect(failed).toBeVisible()
    // No draft is open, so the hand-off loses nothing.
    expect(within(failed.closest('[role="alert"]') as HTMLElement).getByRole('button', { name: /Ask the agent/ })).toBeVisible()
    expect(location).not.toContain('/chat')
    typeUrl('https://draft.example.test')
    // Kept, disabled, with the reason, instead of vanishing.
    const reopen = screen.getByRole('button', { name: 'Reopen chat' })
    expect(reopen).toBeDisabled()
    expect(reopen).toHaveAccessibleDescription('Save or discard your changes first.')
    // The failure is still said; only the hand-off goes, since leaving would lose the draft.
    const kept = screen.getByText('Could not reopen that chat.').closest('[role="alert"]') as HTMLElement
    expect(kept).toBeVisible()
    expect(within(kept).queryByRole('button', { name: /Ask the agent/ })).toBeNull()
  })

  it('says a reopen failed even when edits started while it was running', async () => {
    let release: () => void = () => {}
    server.use(
      http.get('*/api/session-environments/workspaces', () => HttpResponse.json({ workspaces: [archived] })),
      http.post('*/api/chat/slots/:key/resume', async () => {
        await new Promise<void>(resolve => { release = resolve })
        return HttpResponse.json({ error: 'gone' }, { status: 404 })
      }),
    )
    await mounted()
    fireEvent.click(await screen.findByRole('button', { name: 'Reopen chat' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Reopen chat' })).toBeDisabled())
    typeUrl('https://draft.example.test')
    act(() => release())
    const notice = (await screen.findByText('Could not reopen that chat.')).closest('[role="alert"]') as HTMLElement
    expect(notice).toBeVisible()
    expect(within(notice).queryByRole('button', { name: /Ask the agent/ })).toBeNull()
    expect(screen.getByLabelText('Coder URL')).toHaveValue('https://draft.example.test')
  })

  it('offers no link into a live chat while the card holds unsaved edits', async () => {
    const live = { workspace: 'crewenv-devuser-live', slot: 'chat-live', state: 'running', template: 't', created_at: '2026-10-01T12:00:00Z', deletion_due_at: '' }
    server.use(http.get('*/api/session-environments/workspaces', () => HttpResponse.json({ workspaces: [live] })))
    await mounted()
    expect(await screen.findByRole('link', { name: 'crewenv-devuser-live' })).toHaveAttribute('href', '/chat?sid=chat-live')
    typeUrl('https://draft.example.test')
    // Following it would leave the card and drop the draft: plain text, with why.
    expect(screen.queryByRole('link', { name: 'crewenv-devuser-live' })).toBeNull()
    const row = screen.getByText('crewenv-devuser-live').closest('li') as HTMLElement
    expect(row).not.toHaveTextContent('Save or discard your changes first.')
    expect(screen.getByText('Save or discard your changes first.')).toBeVisible()
  })

  it('names the chat and the profile each workspace row belongs to', async () => {
    stored.session_environments!.profiles.push({ id: 'gpu', name: 'Coder GPU', template: 'gpu-dev', workdir: '/workspace/project' })
    server.use(http.get('*/api/session-environments/workspaces', () => HttpResponse.json({ workspaces: [
      { workspace: 'crewenv-a', slot: 'chat-a', profile: 'gpu', state: 'running', template: 'gpu-dev', created_at: '2026-10-01T12:00:00Z', deletion_due_at: '' },
      { workspace: 'crewenv-b', slot: 'chat-b', profile: 'dev', state: 'stopped', template: 't', created_at: '2026-10-01T12:00:00Z', deletion_due_at: '2026-10-30T12:00:00Z' },
      { workspace: 'crewenv-c', slot: 'chat-gone', profile: 'removed', state: 'running', template: 't', created_at: '2026-10-01T12:00:00Z', deletion_due_at: '' },
    ] })))
    const base = createTestStore().getState().dashboard
    mount(createTestStore({ dashboard: { ...base, slots: [{ key: 'chat-a', title: 'Fix the flaky test' } as never] } }))
    const list = await screen.findByRole('list', { name: 'Workspaces' })
    const rows = list.querySelectorAll('li')
    expect(rows[0]).toHaveTextContent('Profile: Coder GPU')
    expect(rows[0]).toHaveTextContent('In use by Fix the flaky test')
    expect(rows[1]).toHaveTextContent('Profile: Development')
    // No title on hand, or a profile no longer saved: said with what is known.
    expect(rows[2]).toHaveTextContent('In use by a chat')
    expect(rows[2]).toHaveTextContent('Profile: removed')
  })

  it('opens a mate thread\'s workspace on the Members page, not the chat page', async () => {
    const rows = [
      { workspace: 'crewenv-devuser-mate', slot: 'member-oncall', state: 'running', template: 't', created_at: '2026-10-01T12:00:00Z', deletion_due_at: '' },
      { workspace: 'crewenv-devuser-shut', slot: 'member-closed', state: 'running', template: 't', created_at: '2026-10-01T12:00:00Z', deletion_due_at: '' },
    ]
    server.use(http.get('*/api/session-environments/workspaces', () => HttpResponse.json({ workspaces: rows })))
    // The open thread's `agent` names its mate; a closed one has no open slot.
    const base = createTestStore().getState().dashboard
    mount(createTestStore({ dashboard: { ...base, slots: [{ key: 'member-oncall', mode: 'member', agent: 'oncall' } as never] } }))
    expect(await screen.findByRole('link', { name: 'crewenv-devuser-mate' })).toHaveAttribute('href', '/members?member=oncall')
    expect(screen.getByRole('link', { name: 'crewenv-devuser-shut' })).toHaveAttribute('href', '/members')
  })

  it('says "save first" once for the whole list, not on every row', async () => {
    const live = { workspace: 'crewenv-devuser-live', slot: 'chat-live', state: 'running', template: 't', created_at: '2026-10-01T12:00:00Z', deletion_due_at: '' }
    server.use(http.get('*/api/session-environments/workspaces', () => HttpResponse.json({ workspaces: [live, archived, { ...archived, workspace: 'crewenv-devuser-eeee', slot: 'chat-10' }] })))
    await mounted()
    await screen.findByRole('link', { name: 'crewenv-devuser-live' })
    expect(screen.queryByText('Save or discard your changes first.')).toBeNull()
    typeUrl('https://draft.example.test')
    expect(screen.getAllByText('Save or discard your changes first.')).toHaveLength(1)
    // Each waiting button still names the reason, by pointing at that one line.
    for (const reopen of screen.getAllByRole('button', { name: 'Reopen chat' })) {
      expect(reopen).toBeDisabled()
      expect(reopen).toHaveAccessibleDescription('Save or discard your changes first.')
    }
  })
})
