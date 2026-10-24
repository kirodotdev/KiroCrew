import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { Provider } from 'react-redux'
import { createTestStore } from './helpers'
import { http, HttpResponse } from 'msw'
import { server } from '../../integration/mocks/server'
import { api } from '../api/client'
import { ApiError } from '../api/apiError'
import { SessionEnvironmentsCard } from '../pages/settings/SessionEnvironmentsCard'
import { FeaturePreviewsSection } from '../pages/settings/FeaturePreviewsSection'
import { PREVIEW_SESSION_ENVIRONMENTS } from '../utils/previewFlags'
import { settingEntryOffered } from '../components/commandPalette/settingsSearchCore'
import { SETTINGS_REGISTRY } from '../components/commandPalette/settingsRegistry.gen'
import { setSettingsDeepLinkTarget } from '../components/settings'

vi.mock('../pages/settings/DecisionsCard', () => ({ DecisionsCard: () => null }))

const profile = { id: 'dev', name: 'Development', template: 'python-dev', workdir: '/workspace/repo' }
interface SpritesRow { id: string; name: string; token_secret: string; workdir: string }
interface BoxdRow { id: string; name: string; snapshot: string; vcpu: number; workdir: string }
interface Section {
  coder_url: string
  profiles: typeof profile[]
  sprites_profiles?: SpritesRow[]
  boxd_profiles?: BoxdRow[]
  default_profile?: string
  kiro_cloud_profiles?: { id: string; name: string; repositories: string[] }[]
  stop_on_shutdown?: boolean
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
  )
})
afterEach(() => { cleanup(); qc?.clear(); localStorage.clear(); vi.useRealTimers(); vi.restoreAllMocks() })

const url = () => screen.getByLabelText('Coder URL')
const typeUrl = (value: string) => fireEvent.change(url(), { target: { value } })
/** The Sprites token field's label: the token's format is part of it. */
const TOKEN_LABEL = 'Sprites API token'
/** The section's one Save. */
const save = () => screen.getByRole('button', { name: 'Save settings' })
/** The visible tab's Add button (each provider section has one). */
const addVisible = () => fireEvent.click(screen.getAllByRole('button', { name: /^Add (Coder|Sprites|boxd) profile$/ }).find(b => !b.closest('[hidden]'))!)
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
/** The status glyph of the check line holding `node`. */
const glyph = (node: HTMLElement) => node.closest('li')!.querySelector('svg')!
const SANDBOX_LINK = 'Change the agent sandbox (Developer → Config)'

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
    const notice = await screen.findByText(/Couldn't load your Coder templates, so type the template's name\. coder: not logged in/)
    expect(within(notice.closest('[role="alert"]') as HTMLElement).queryByRole('button', { name: /Ask the agent/ })).toBeNull()
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
    const WARN = /Chats still on this profile: 1\. Once you save, they can't run until a profile with the ID dev is added back\./
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
    // The banner points at the box; the gateway's reason is said once, under it.
    await screen.findByText('Could not save the settings: fix the Coder URL above.')
    expect(screen.getAllByText(/coder_url must include a hostname/)).toHaveLength(1)
    expect(url()).toHaveAttribute('aria-invalid', 'true')
    // The reason sits under the box too, and the box points at it.
    const describedBy = url().getAttribute('aria-describedby') ?? ''
    expect(document.getElementById(describedBy.split(' ').pop()!)).toHaveTextContent('coder_url must include a hostname')
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

  it('offers no agent hand-off from a missing provider list while a draft is unsaved', async () => {
    // A gateway older than Sprites and boxd returns no lists for them.
    stored = { session_environments: { coder_url: 'https://coder.example.test', profiles: [{ ...profile }], default_profile: '' } }
    mount()
    await screen.findByLabelText('Coder URL')
    for (const tab of ['Fly.io Sprites', 'boxd']) {
      fireEvent.click(screen.getByRole('radio', { name: tab }))
      const clean = await screen.findByText(/does not support Session Environments settings/)
      expect(within(clean.closest('[role="alert"]') as HTMLElement).getByRole('button', { name: /Ask the agent/ })).toBeVisible()
    }
    fireEvent.click(screen.getByRole('radio', { name: 'Coder' }))
    fireEvent.change(url(), { target: { value: 'https://draft.example.test' } })
    for (const tab of ['Fly.io Sprites', 'boxd']) {
      fireEvent.click(screen.getByRole('radio', { name: tab }))
      // The hand-off navigates away, which would drop the unsaved Coder edit.
      const dirty = await screen.findByText(/does not support Session Environments settings/)
      expect(within(dirty.closest('[role="alert"]') as HTMLElement).queryByRole('button', { name: /Ask the agent/ })).toBeNull()
    }
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
    // A saved Coder setup opens on the Coder tab.
    expect(screen.getByRole('radio', { name: 'Coder', exact: true })).toBeChecked()
    expect(screen.getByRole('radio', { name: 'Fly.io Sprites' })).not.toBeChecked()
    expect(screen.getByText(/A chat runs on a profile once you choose it in the “Run this chat on” menu next to the message box/)).toBeVisible()
    expect(writes).toEqual([])
  })

  it('collapses saved profiles, opens new ones, and keeps edits across collapse', async () => {
    await mounted()
    const header = screen.getByRole('button', { name: /^Development/ })
    expect(header).toHaveAttribute('aria-expanded', 'false')
    expect(header).toHaveTextContent('python-dev')
    expect(screen.getByLabelText('Coder template')).not.toBeVisible()
    fireEvent.click(header)
    expect(header).toHaveAttribute('aria-expanded', 'true')
    for (const hint of ['A short unique code: letters, digits, _ and - only. Chats remember it, so changing it later stops chats already on this profile.', 'Shown in the “Run this chat on” menu.', "Which kind of machine each chat gets: Coder creates the chat's workspace from this template, for example python-dev. Run coder templates list on this machine to see yours; Check Coder connection then confirms it works.", "Folder where the chat's files and commands run."]) expect(screen.getByText(hint)).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Coder template'), { target: { value: 'node-dev' } })
    fireEvent.click(header)
    expect(screen.getByLabelText('Coder template')).not.toBeVisible()
    expect(header).toHaveTextContent('node-dev')
    fireEvent.click(header)
    expect(screen.getByLabelText('Coder template')).toHaveValue('node-dev')
    fireEvent.click(screen.getByRole('button', { name: 'Add Coder profile' }))
    expect(screen.getByRole('button', { name: /^profile-1/ })).toHaveAttribute('aria-expanded', 'true')
    expect(writes).toEqual([])
  })

  it('keeps an unsaved Sprites token mounted but unfocusable across collapse', async () => {
    const fly = { id: 'fly', name: 'Fly', token_secret: 'TOKEN', workdir: '/w' }
    stored = { session_environments: { coder_url: '', profiles: [], sprites_profiles: [fly] } }
    server.use(http.get('*/api/secrets', () => HttpResponse.json({ names: [], managed: [] })))
    const saveToken = vi.spyOn(api, 'secretsSave')
    mount()
    await screen.findByLabelText('Coder URL')
    expand('Fly')
    const header = screen.getByRole('button', { name: /^Fly/ })
    const field = screen.getByLabelText(TOKEN_LABEL)
    fireEvent.change(field, { target: { value: 'unsaved-token' } })
    fireEvent.click(header)
    const body = document.getElementById(header.getAttribute('aria-controls')!)!
    // Inert at once (nothing inside takes focus), hidden once the collapse lands,
    // so the body is still drawn while it animates shut.
    expect(body).toHaveAttribute('inert')
    expect(body).not.toHaveAttribute('hidden')
    await waitFor(() => expect(body).toHaveAttribute('hidden'))
    expect(field).toBeInTheDocument()
    expect(field).not.toBeVisible()
    expect(screen.queryByRole('button', { name: 'Save token' })).toBeNull()
    fireEvent.click(header)
    expect(screen.getByLabelText(TOKEN_LABEL)).toBe(field)
    await waitFor(() => expect(field).toBeVisible())
    expect(field).toHaveValue('unsaved-token')
    expect(body).not.toHaveAttribute('inert')
    expect(saveToken).not.toHaveBeenCalled()
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

  it('keeps the shutdown switch off and locked until a profile exists', async () => {
    stored = { session_environments: { coder_url: 'https://coder.example.test', profiles: [], default_profile: '', stop_on_shutdown: false } as Section }
    await mounted()
    const stop = () => screen.getByRole('switch', { name: 'Stop workspaces when Kiro Crew shuts down' })
    expect(stop()).toHaveAttribute('aria-disabled', 'true')
    expect(screen.getAllByText('Save a profile above to turn this on.')).toHaveLength(2)
    fireEvent.click(stop())
    expect(stop()).not.toBeChecked()
    fireEvent.click(screen.getByRole('button', { name: 'Add Coder profile' }))
    expect(stop()).not.toHaveAttribute('aria-disabled', 'true')
    expect(screen.getByText(/Off: they keep running, so chats reconnect faster/)).toBeInTheDocument()
    expect(stop()).not.toBeChecked()
    expect(writes).toEqual([])
  })

  it('shows a stored "on" shutdown switch as on, and lets it be turned off, with no profile left', async () => {
    // Shutdown still stops or hibernates every registered machine, so a switch
    // drawn off here would hide that the next restart acts on them.
    stored = { session_environments: { coder_url: 'https://coder.example.test', profiles: [], default_profile: '', stop_on_shutdown: true } as Section }
    await mounted()
    const stop = () => screen.getByRole('switch', { name: 'Stop workspaces when Kiro Crew shuts down' })
    expect(stop()).toBeChecked()
    expect(stop()).not.toHaveAttribute('aria-disabled', 'true')
    expect(screen.getByText(/Off: they keep running, so chats reconnect faster/)).toBeInTheDocument()
    fireEvent.click(stop())
    expect(stop()).not.toBeChecked()
    fireEvent.click(save())
    await waitFor(() => expect(writes).toHaveLength(1))
    expect((writes[0] as { value: Section }).value).toMatchObject({ stop_on_shutdown: false })
  })
})

describe('Session Environments readiness and workspaces', () => {
  it('runs the read-only check and reports Coder, each template and each git host', async () => {
    let checks = 0
    server.use(http.post('*/api/session-environments/check', () => {
      checks++
      return HttpResponse.json({
        coder: { ok: true, owner: 'devuser' },
        profiles: [{ id: 'dev', ok: false, error: 'Coder template is unavailable' }],
        git: { 'github.com': 'signed_in', 'dev.azure.com': 'not_signed_in', 'bitbucket.org': 'unchecked' },
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
    expect(results).toHaveTextContent('GitHub: signed in on this machine')
    expect(results).toHaveTextContent(
      'Azure DevOps: not signed in on this machine. To sign in, run az login in a terminal on this machine.',
    )
    expect(results).toHaveTextContent(
      "Bitbucket: can't be checked from here; git will use this machine's saved Bitbucket login if there is one. If git can't sign in, clone a private Bitbucket repository over HTTPS in a terminal on this machine; git's credential helper saves the login it asks for.",
    )
    // No bare `git credential approve`: typed alone it waits on stdin.
    expect(results).not.toHaveTextContent('git credential approve')
    // Bitbucket has a display name of its own, like the other hosts.
    expect(results).not.toHaveTextContent('bitbucket.org')
    // The sign-in command reads as a command the reader copies, untranslated.
    const command = within(results).getByText('az login')
    expect(command.tagName).toBe('CODE')
    expect(command).toHaveAttribute('translate', 'no')
    // A git host is optional: signed out is a muted state ring, never the
    // danger cross the blocking Coder and template failures use, and never an
    // arrow that reads as a sign-in button.
    const signedOut = glyph(command)
    expect(signedOut).toHaveClass('lucide-circle-dashed', 'text-muted')
    expect(signedOut).not.toHaveClass('lucide-log-in')
    expect(signedOut).not.toHaveClass('text-danger')
    expect(results.querySelector('svg.text-danger, svg.lucide-x, svg.lucide-log-in')).toBeNull()
    expect(within(results).queryAllByRole('button').filter(b => !b.closest('[role="alert"]'))).toEqual([])
    const unchecked = glyph(within(results).getByText(/^Bitbucket: can't be checked/))
    expect(unchecked).toHaveClass('lucide-minus', 'text-muted')
    expect(glyph(within(results).getByText('GitHub: signed in on this machine'))).toHaveClass('lucide-check', 'text-ok')
    // Nothing was withheld, so there is no sandbox to point at.
    expect(screen.queryByRole('link', { name: SANDBOX_LINK })).toBeNull()
    expect(writes).toEqual([])
  })

  it('says once that a Windows gateway that cannot verify its account shares no git login, with no sandbox link', async () => {
    server.use(http.post('*/api/session-environments/check', () => HttpResponse.json({
      coder: { ok: true, owner: 'devuser' }, profiles: [],
      git: { 'github.com': 'unsupported', 'gitlab.com': 'unsupported', 'bitbucket.org': 'unsupported', 'dev.azure.com': 'unsupported' },
    })))
    mount()
    await screen.findByLabelText('Coder URL')
    fireEvent.click(check())
    const results = await screen.findByRole('list', { name: 'Check results' })
    within(results).getByText(
      "This machine can't verify its own Windows account, so its git logins aren't shared with remote sessions. Each workspace uses its own git login.",
    )
    expect(within(results).getAllByRole('listitem')).toHaveLength(2)
    expect(results).not.toHaveTextContent(/GitHub|GitLab|Bitbucket|Azure DevOps|signed in on this machine/)
    // The sandbox tier is not the reason, so nothing points at it.
    expect(screen.queryByRole('link', { name: SANDBOX_LINK })).toBeNull()
  })

  it('says once that a strict sandbox shares no git login and links to where the sandbox is set', async () => {
    server.use(http.post('*/api/session-environments/check', () => HttpResponse.json({
      coder: { ok: true, owner: 'devuser' }, profiles: [],
      git: { 'github.com': 'withheld', 'gitlab.com': 'withheld', 'bitbucket.org': 'withheld', 'dev.azure.com': 'withheld' },
    })))
    mount()
    await screen.findByLabelText('Coder URL')
    fireEvent.click(check())
    const results = await screen.findByRole('list', { name: 'Check results' })
    const withheld = within(results).getByText(
      "Git logins aren't shared while the agent sandbox is set to strict. Each workspace uses its own git login.",
    )
    // One line for every host, not four identical ones.
    expect(within(results).getAllByRole('listitem')).toHaveLength(2)
    expect(results).not.toHaveTextContent(/GitHub|GitLab|Bitbucket|Azure DevOps|signed in on this machine/)
    // Withheld has a glyph of its own, distinct from the unchecked dash.
    expect(glyph(withheld)).toHaveClass('lucide-lock', 'text-muted')
    expect(glyph(withheld)).toHaveAttribute('aria-hidden', 'true')
    const link = screen.getByRole('link', { name: SANDBOX_LINK })
    expect(link).toHaveAttribute('href', '/developer?tab=config&highlight=key%3Aagent.sandbox')
    expect(results).not.toContainElement(link)
  })

  it('keeps per-host rows when only some hosts are withheld', async () => {
    server.use(http.post('*/api/session-environments/check', () => HttpResponse.json({
      coder: { ok: true, owner: 'devuser' }, profiles: [], git: { 'github.com': 'withheld', 'gitlab.com': 'unchecked' },
    })))
    mount()
    await screen.findByLabelText('Coder URL')
    fireEvent.click(check())
    const results = await screen.findByRole('list', { name: 'Check results' })
    expect(glyph(within(results).getByText("GitHub: not shared with remote sessions under the 'strict' sandbox"))).toHaveClass('lucide-lock')
    expect(glyph(within(results).getByText(/^GitLab: can't be checked/))).toHaveClass('lucide-minus')
    expect(results).not.toHaveTextContent(/Git logins aren't shared/)
    expect(screen.getByRole('link', { name: SANDBOX_LINK })).toBeVisible()
  })

  it('renders the Coder and profile rows from a gateway that sends no git field', async () => {
    server.use(http.post('*/api/session-environments/check', () => HttpResponse.json({
      coder: { ok: true, owner: 'devuser' }, profiles: [{ id: 'dev', ok: true }],
    })))
    mount()
    await screen.findByLabelText('Coder URL')
    fireEvent.click(check())
    const results = await screen.findByRole('list', { name: 'Check results' })
    expect(results).toHaveTextContent('Signed in to Coder as devuser')
    expect(results).toHaveTextContent('Development: template python-dev is available')
    expect(within(results).getAllByRole('listitem')).toHaveLength(2)
    expect(results).not.toHaveTextContent(/GitHub|Git logins|signed in on this machine/)
    expect(screen.queryByRole('link', { name: SANDBOX_LINK })).toBeNull()
  })

  it('says the readiness check also reads git sign-ins, and leaves the Manage hint to p1', async () => {
    mount()
    await screen.findByLabelText('Coder URL')
    // The readiness rows say how git signs in; the Manage hint does not repeat it.
    expect(screen.queryByText(/Git to GitHub, GitLab, Bitbucket and Azure DevOps/)).toBeNull()
    // The readiness check says it also reads which git hosts this machine is signed in to.
    expect(screen.getByText(
      "Checks that the Coder CLI on this machine is signed in, that each profile's template exists, and which git hosts this machine is signed in to. Nothing is started and no credential is read.",
    )).toBeVisible()
    expect(screen.queryByText(/which keeps the credentials/)).toBeNull()
  })

  it('surfaces failed Coder checks with a safe hand-off and drops them while editing', async () => {
    checkAnswers({ coder: { ok: false, error: 'Coder login expired' }, profiles: [], git: {} })
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
    expect(screen.queryByText('Could not reopen that chat.')).toBeNull()
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

describe('Kiro Cloud Sessions profiles', () => {
  it('adds a cloud profile with its repositories, and offers it as the default', async () => {
    stored = { session_environments: { coder_url: '', profiles: [], kiro_cloud_profiles: [], default_profile: '' } }
    mount()
    const add = await screen.findByRole('button', { name: 'Add Kiro Cloud profile' })
    // The Kiro Cloud list needs no Coder URL.
    fireEvent.click(add)
    const field = screen.getByRole('group', { name: 'kiro-cloud-1' })
    fireEvent.change(within(field).getByLabelText('Profile name'), { target: { value: 'Cloud' } })
    // A trailing comma while typing is kept, then dropped on save.
    fireEvent.change(within(field).getByLabelText('Repositories'), { target: { value: 'acme/web, ' } })
    expect(within(field).getByLabelText('Repositories')).toHaveValue('acme/web, ')
    fireEvent.click(screen.getByRole('switch', { name: 'Start new chats on a profile' }))
    fireEvent.click(save())
    expect(await screen.findByText(SAVED)).toBeVisible()
    expect(writes).toEqual([{ path: 'session_environments', value: {
      coder_url: '', profiles: [], default_profile: 'kiro-cloud-1',
      kiro_cloud_profiles: [{ id: 'kiro-cloud-1', name: 'Cloud', repositories: ['acme/web'] }],
    } }])
  })

  it('carries the default along when a cloud profile id is renamed', async () => {
    stored = {
      session_environments: {
        coder_url: 'https://coder.example.test', profiles: [{ ...profile }], default_profile: 'cloud',
        kiro_cloud_profiles: [{ id: 'cloud', name: 'Cloud', repositories: [] }],
      },
    }
    await mounted()
    expand('Cloud')
    const cloudId = within(screen.getByRole('group', { name: 'Cloud' })).getByLabelText('Profile ID')
    fireEvent.change(cloudId, { target: { value: 'cloud-2' } })
    // Clearing the id to retype it keeps the default for the retyped id.
    fireEvent.change(cloudId, { target: { value: '' } })
    fireEvent.change(cloudId, { target: { value: 'cloud-3' } })
    // A Coder id passing through empty does not take a cloud default.
    expand('Development')
    const coder = screen.getByRole('button', { name: /^Development/ }).closest('div')!.parentElement!
    fireEvent.change(within(coder).getByLabelText('Profile ID'), { target: { value: '' } })
    fireEvent.change(within(coder).getByLabelText('Profile ID'), { target: { value: 'dev-2' } })
    fireEvent.click(save())
    await waitFor(() => expect(writes).toHaveLength(1))
    expect((writes[0] as { value: Section }).value).toMatchObject({
      default_profile: 'cloud-3',
      profiles: [{ ...profile, id: 'dev-2' }],
      kiro_cloud_profiles: [{ id: 'cloud-3', name: 'Cloud', repositories: [] }],
    })
  })

  it('moves a removed cloud default to another cloud profile, and leaves it on a Coder removal', async () => {
    stored = {
      session_environments: {
        coder_url: 'https://coder.example.test', profiles: [{ ...profile }], default_profile: 'cloud',
        kiro_cloud_profiles: [
          { id: 'cloud', name: 'Cloud', repositories: [] },
          { id: 'scratch', name: 'Scratch', repositories: [] },
        ],
      },
    }
    await mounted()
    const defaults = screen.getByRole('region', { name: 'Global defaults' })
    // Removing a Coder profile leaves a cloud default alone, with no notice.
    expand('Development')
    const coder = screen.getByRole('button', { name: /^Development/ }).closest('div')!.parentElement!
    fireEvent.click(within(coder).getByRole('button', { name: 'Remove profile' }))
    expect(defaults).not.toHaveTextContent('the default profile was removed')
    // Removing the cloud default moves it to the other cloud profile, and says so.
    expand('Cloud')
    // Only the expanded row shows its Remove button.
    fireEvent.click(screen.getByRole('button', { name: 'Remove Kiro Cloud profile' }))
    expect(defaults).toHaveTextContent('Now Scratch: the default profile was removed. Discard changes to undo.')
    fireEvent.click(save())
    await waitFor(() => expect(writes).toHaveLength(1))
    expect((writes[0] as { value: Section }).value).toMatchObject({
      default_profile: 'scratch', profiles: [],
      kiro_cloud_profiles: [{ id: 'scratch', name: 'Scratch', repositories: [] }],
    })
  })

  it('says Kiro Cloud profiles are not checked ahead of time, without pointing at the Coder steps', async () => {
    stored = { session_environments: { coder_url: '', profiles: [], kiro_cloud_profiles: [], default_profile: '' } }
    mount()
    // The readiness check probes Coder and git hosts only, so the cloud list says
    // so in words that stand on their own: its reader cannot see the Coder steps.
    expect(await screen.findByText(/These profiles aren't checked ahead of time/)).toBeVisible()
    expect(screen.getByText(/If a cloud chat can't start, the chat explains why/)).toBeVisible()
  })

  it('keeps Kiro Cloud outside the numbered Coder steps, with its cost in plain view', async () => {
    stored = {
      session_environments: {
        coder_url: '', profiles: [], default_profile: '',
        kiro_cloud_profiles: [{ id: 'cloud', name: 'Cloud', repositories: [] }],
      },
    }
    mount()
    const cloud = await screen.findByRole('region', { name: 'Kiro Cloud Sessions' })
    const configure = screen.getByRole('region', { name: '1. Connect and configure' })
    const manage = screen.getByRole('region', { name: '3. Manage' })
    expect(configure.contains(cloud)).toBe(false)
    // After the last numbered step, not between step 1 and step 2.
    expect(manage.compareDocumentPosition(cloud) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    // What opting in gives up is not folded inside the setup instructions.
    const limits = within(cloud).getByText(/runs commands without asking you first, and can't use the tools and connections you set up in/)
    expect(limits).toBeVisible()
    expect(limits.closest('details')).toBeNull()
    // The cost of opting in reads as a warning, not body text.
    expect(limits.closest('[data-tone]')).toHaveAttribute('data-tone', 'warn')
    // The setup step names the backend by the label the Agent Harness picker shows.
    expect(within(cloud).getByText(/choose the Kiro Agent backend/)).toHaveTextContent('Settings → Agent Harness')
    // Its controls are named for it, so they are not twins of the Coder ones.
    expect(within(cloud).getByRole('button', { name: 'Add Kiro Cloud profile' })).toBeEnabled()
    expect(within(cloud).getByText('Kiro Cloud setup instructions')).toBeInTheDocument()
    expect(within(cloud).queryByRole('button', { name: 'Add Coder profile' })).toBeNull()
    fireEvent.click(within(cloud).getByRole('button', { name: /^Cloud/, expanded: false }))
    expect(within(cloud).getByRole('button', { name: 'Remove Kiro Cloud profile' })).toBeInTheDocument()
  })

  it('hides the Kiro Cloud list for a gateway that does not report it', async () => {
    mount()
    await screen.findByLabelText('Coder URL')
    expect(screen.queryByText('Kiro Cloud Sessions')).not.toBeInTheDocument()
    expect(screen.queryByText(/These profiles aren't checked ahead of time/)).not.toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: 'Add Coder profile' })).toHaveLength(1)
  })
})

describe('Sprites and boxd providers', () => {
  it('opens the tab, row and disclosure holding a deep-linked setting', async () => {
    // With Coder open and every row closed, the palette's "Machine size" and
    // "Secret name" links reveal the tab, row and disclosure that hold them.
    const box = { id: 'box', name: 'Box', snapshot: '', vcpu: 2, workdir: '/w' }
    const fly = { id: 'fly', name: 'Fly', token_secret: 'SPRITES_TOKEN', workdir: '/w' }
    stored = { session_environments: { coder_url: 'https://coder.example.test', profiles: [{ ...profile }], sprites_profiles: [fly], boxd_profiles: [box] } }
    mount()
    await screen.findByLabelText('Coder URL')
    expect(screen.getByRole('radio', { name: 'Coder' })).toBeChecked()
    expect(screen.getByLabelText('Machine size')).not.toBeVisible()
    act(() => setSettingsDeepLinkTarget('[data-setting-label="Machine size"]'))
    await waitFor(() => expect(screen.getByLabelText('Machine size')).toBeVisible())
    expect(screen.getByRole('radio', { name: 'boxd' })).toBeChecked()
    act(() => setSettingsDeepLinkTarget(null))
    const secretName = 'Secret name for the token (usually SPRITES_TOKEN)'
    expect(screen.getByLabelText(secretName)).not.toBeVisible()
    act(() => setSettingsDeepLinkTarget(`[data-setting-label="${secretName}"]`))
    await waitFor(() => expect(screen.getByLabelText(secretName)).toBeVisible())
    expect(screen.getByRole('radio', { name: 'Fly.io Sprites' })).toBeChecked()
    act(() => setSettingsDeepLinkTarget(null))
  })
  it('shows the one shutdown switch the same on every tab, with what it does per provider', async () => {
    const box = { id: 'box', name: 'boxd', snapshot: '', vcpu: 2, workdir: '/w' }
    stored = { session_environments: { coder_url: 'https://coder.example.test', profiles: [{ ...profile }], boxd_profiles: [box], stop_on_shutdown: true } }
    mount()
    await screen.findByLabelText('Coder URL')
    const shutdown = () => screen.getByRole('switch', { name: 'Stop workspaces when Kiro Crew shuts down' })
    const coder = /^On: Kiro Crew stops your workspaces when it shuts down\..*Coder's own auto-stop still applies\.$/
    const sprites = 'Sprites are not stopped when Kiro Crew shuts down: a sprite hibernates on its own when idle and keeps its files.'
    const boxd = 'boxd: when this is on, the machines this computer created are hibernated, so they keep their files.'
    for (const tab of ['Coder', 'Fly.io Sprites', 'boxd']) {
      fireEvent.click(screen.getByRole('radio', { name: tab, exact: true }))
      // One switch, one label and one Coder line on every tab, never a per-tab variant.
      expect(shutdown()).toBeChecked()
      expect(screen.getByText(coder)).toBeVisible()
      const notes = screen.getByTestId('shutdown-provider-notes')
      expect(within(notes).getByText(boxd)).toBeVisible()
      expect(within(notes).getByText(sprites)).toBeVisible()
      expect(shutdown()).toHaveAccessibleDescription(`${boxd} ${sprites}`)
    }
  })

  it('reports only the selected provider in the check, even with a Coder URL still saved', async () => {
    const fly = { id: 'fly', name: 'Fly.io Sprites', token_secret: 'SPRITES_TOKEN', workdir: '/w' }
    stored = { session_environments: { coder_url: 'https://coder.example.test', profiles: [{ ...profile }], sprites_profiles: [fly] } }
    server.use(http.post('*/api/session-environments/check', () => HttpResponse.json({
      coder: { ok: true, owner: 'devuser' },
      profiles: [{ id: 'dev', ok: true }],
      sprites: [{ id: 'fly', ok: true }],
      git: { 'github.com': 'signed_in' },
    })))
    mount()
    await screen.findByLabelText('Coder URL')
    fireEvent.click(screen.getByRole('radio', { name: 'Fly.io Sprites' }))
    expect(screen.getByText(/Sprites API token is accepted/)).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Check Sprites connection' }))
    const results = await screen.findByRole('list', { name: 'Check results' })
    expect(results).toHaveTextContent('Fly.io Sprites: Sprites API token accepted')
    expect(results).toHaveTextContent('GitHub: signed in on this machine')
    expect(results).not.toHaveTextContent('Coder')
    // Switching back to Coder shows the Coder rows from the same result.
    fireEvent.click(screen.getByRole('radio', { name: 'Coder', exact: true }))
    expect(results).toHaveTextContent('Signed in to Coder as devuser')
    expect(results).not.toHaveTextContent('Sprites API token')
  })

  it('says why Check is unavailable when the open provider has nothing saved', async () => {
    const fly = { id: 'fly', name: 'Fly.io Sprites', token_secret: 'SPRITES_TOKEN', workdir: '/w' }
    stored = { session_environments: { coder_url: '', profiles: [], sprites_profiles: [fly] } }
    mount()
    await screen.findByLabelText('Coder URL')
    const checkSprites = () => screen.getByRole('button', { name: 'Check Sprites connection' })
    expect(checkSprites()).toBeEnabled()
    expect(checkSprites()).toHaveAccessibleDescription(/Sprites API token is accepted/)
    fireEvent.click(screen.getByRole('radio', { name: 'boxd' }))
    const checkBoxd = screen.getByRole('button', { name: 'Check boxd connection' })
    expect(checkBoxd).toBeDisabled()
    expect(checkBoxd).toHaveAccessibleDescription('Nothing is saved for this provider yet. Save a profile first, then check it.')
  })

  it('opens on Sprites when only Sprites is saved, and checks without a Coder URL', async () => {
    stored = { session_environments: { coder_url: '', profiles: [], sprites_profiles: [] } }
    server.use(http.post('*/api/session-environments/check', () => HttpResponse.json({
      coder: { ok: false }, profiles: [], git: {}, sprites: [{ id: 'sprites-1', ok: false, error: 'the Sprites API token was refused' }],
    })))
    mount()
    await screen.findByLabelText('Coder URL')
    fireEvent.click(screen.getByRole('radio', { name: 'Fly.io Sprites' }))
    addVisible()
    expect(screen.getByLabelText('Secret name for the token (usually SPRITES_TOKEN)')).toHaveValue('SPRITES_TOKEN')
    // Where the token is kept is rarely changed, so it starts tucked away.
    expect(screen.getByText('Advanced: secret name').closest('details')).not.toHaveAttribute('open')
    // The secret name says what it is: the key the token is stored under, not the token.
    expect(screen.getByText(/The name the token is stored under in Settings → Secrets\./)).toBeInTheDocument()
    expect(screen.getByLabelText(TOKEN_LABEL)).toHaveAttribute('placeholder', 'Paste a Sprites API token')
    // A new profile's working folder starts blank: the sprite's own home folder.
    expect(screen.getByLabelText('Working folder')).toHaveValue('')
    expect(screen.getByText("Leave blank to use the machine's home folder.")).toBeInTheDocument()
    // Until the configuration is saved, the profile says it is not in the composer yet.
    expect(screen.getByTestId('sprites-profile-unsaved')).toHaveTextContent('This profile is not saved yet')
    fireEvent.click(save())
    await waitFor(() => expect(writes).toHaveLength(1))
    await waitFor(() => expect(screen.queryByTestId('sprites-profile-unsaved')).toBeNull())
    expect((writes[0] as { value: Section }).value.sprites_profiles).toEqual([
      { id: 'sprites-1', name: 'Sprites profile 1', token_secret: 'SPRITES_TOKEN', workdir: '' },
    ])
    expect(JSON.stringify(writes[0])).not.toMatch(/"token"/)
    const check = await screen.findByRole('button', { name: 'Check Sprites connection' })
    await waitFor(() => expect(check).toBeEnabled())
    fireEvent.click(check)
    // A failed row is a notice, with the gateway's reason.
    expect(await screen.findByText(/the Sprites API token was refused/)).toBeVisible()
  })

  it('saves a Sprites token to the vault from a masked field and never shows it back', async () => {
    const good = { id: 'fly', name: 'Fly', token_secret: 'SPRITES_TOKEN', workdir: '/srv/work' }
    stored = { session_environments: { coder_url: '', profiles: [], sprites_profiles: [good] } }
    const vault: { name: string; value: string }[] = []
    server.use(
      http.get('*/api/secrets', () => HttpResponse.json({ names: vault.map(v => v.name), managed: [] })),
      http.post('*/api/secrets', async ({ request }) => {
        vault.push(await request.json() as { name: string; value: string })
        return HttpResponse.json({ ok: true })
      }),
      http.delete('*/api/secrets/:name', ({ params }) => {
        vault.splice(vault.findIndex(v => v.name === params.name), 1)
        return HttpResponse.json({ ok: true })
      }),
    )
    mount()
    await screen.findByLabelText('Coder URL')
    // Only Sprites is saved, so the card opens on it.
    expect(screen.getByRole('radio', { name: 'Fly.io Sprites' })).toBeChecked()
    expand('Fly')
    const field = screen.getByLabelText(TOKEN_LABEL)
    expect(field).toHaveAttribute('type', 'password')
    // One Save: there is no separate token button to choose between.
    expect(screen.queryByRole('button', { name: 'Save token' })).not.toBeInTheDocument()
    fireEvent.change(field, { target: { value: 'my-org/t1/secret-value' } })
    fireEvent.click(save())
    await waitFor(() => expect(vault).toEqual([{ name: 'SPRITES_TOKEN', value: 'my-org/t1/secret-value' }]))
    await waitFor(() => expect(field).toHaveValue(''))
    expect(await screen.findByText(/A token is saved as SPRITES_TOKEN\./)).toBeVisible()
    // The token never rides on the profile draft or a config write.
    expect(writes).toEqual([])
    expect(document.body.innerHTML).not.toContain('secret-value')
    expect(screen.queryByRole('button', { name: 'Remove token' })).not.toBeInTheDocument()
    expect(screen.getByText(/remove unused ones in Settings → Secrets/)).toBeVisible()
    expect(vault).toHaveLength(1)
  })

  it('freezes the token field during Save and clears only what was saved', async () => {
    const good = { id: 'fly', name: 'Fly', token_secret: 'SPRITES_TOKEN', workdir: '/w' }
    stored = { session_environments: { coder_url: '', profiles: [], sprites_profiles: [good] } }
    let release!: () => void
    const held = new Promise<void>(resolve => { release = resolve })
    const saved: string[] = []
    server.use(
      http.get('*/api/secrets', () => HttpResponse.json({ names: saved.length ? ['SPRITES_TOKEN'] : [], managed: [] })),
      http.post('*/api/secrets', async ({ request }) => {
        const body = await request.json() as { value: string }
        await held
        saved.push(body.value)
        return HttpResponse.json({ ok: true })
      }),
    )
    mount()
    await screen.findByLabelText('Coder URL')
    expand('Fly')
    const field = screen.getByLabelText(TOKEN_LABEL)
    fireEvent.change(field, { target: { value: 'first-value' } })
    fireEvent.click(save())
    await waitFor(() => expect(field).toBeDisabled())
    await act(async () => { release() })
    await waitFor(() => expect(saved).toEqual(['first-value']))
    await waitFor(() => expect(field).toBeEnabled())
    expect(field).toHaveValue('')
  })

  it('shows only the next step, never a transport code, when a token save cannot reach the server', async () => {
    const good = { id: 'fly', name: 'Fly', token_secret: 'SPRITES_TOKEN', workdir: '/w' }
    stored = { session_environments: { coder_url: '', profiles: [], sprites_profiles: [good] } }
    server.use(http.get('*/api/secrets', () => HttpResponse.json({ names: [], managed: [] })))
    vi.spyOn(api, 'secretsSave').mockRejectedValue(new TypeError('Failed to fetch'))
    mount()
    await screen.findByLabelText('Coder URL')
    expand('Fly')
    fireEvent.change(screen.getByLabelText(TOKEN_LABEL), { target: { value: 'new-token' } })
    fireEvent.click(save())
    expect(await screen.findByText(/Choose Save settings to try again/)).toBeVisible()
    expect(screen.queryByText(/change the name under Advanced: secret name/)).toBeNull()
    expect(screen.queryByText(/Failed to fetch/)).toBeNull()
    expect(writes).toEqual([])
  })

  it('brings a refused token into view when its row was collapsed on another tab', async () => {
    const good = { id: 'fly', name: 'Fly', token_secret: 'SPRITES_TOKEN', workdir: '/w' }
    stored = { session_environments: { coder_url: '', profiles: [], sprites_profiles: [good] } }
    server.use(http.get('*/api/secrets', () => HttpResponse.json({ names: [], managed: [] })))
    vi.spyOn(api, 'secretsSave').mockRejectedValue(new ApiError(400, 'the value is empty'))
    mount()
    await screen.findByLabelText('Coder URL')
    expand('Fly')
    fireEvent.change(screen.getByLabelText(TOKEN_LABEL), { target: { value: '   ' } })
    expand('Fly') // collapse it again
    fireEvent.click(screen.getByRole('radio', { name: 'boxd' }))
    // Rows and tabs stay mounted (drafts survive), only hidden.
    expect(screen.getByLabelText(TOKEN_LABEL)).not.toBeVisible()
    fireEvent.click(save())
    await waitFor(() => expect(screen.getByText('the value is empty')).toBeVisible())
    expect(screen.getByRole('radio', { name: 'Fly.io Sprites' })).toBeChecked()
    expect(screen.getByLabelText(TOKEN_LABEL)).toBeVisible()
    expect(writes).toEqual([])
  })

  it('names the reason a token save failed, keeps the token and writes no configuration', async () => {
    const good = { id: 'fly', name: 'Fly', token_secret: 'SPRITES_TOKEN', workdir: '/w' }
    stored = { session_environments: { coder_url: '', profiles: [], sprites_profiles: [good] } }
    server.use(http.get('*/api/secrets', () => HttpResponse.json({ names: ['SPRITES_TOKEN'], managed: [] })))
    vi.spyOn(api, 'secretsSave').mockRejectedValue(new ApiError(409, 'the vault is locked'))
    mount()
    await screen.findByLabelText('Coder URL')
    expand('Fly')
    expect(await screen.findByText(/A token is saved as SPRITES_TOKEN\./)).toBeVisible()
    const field = screen.getByLabelText(TOKEN_LABEL)
    fireEvent.change(field, { target: { value: 'new-token' } })
    fireEvent.change(screen.getByLabelText('Profile name'), { target: { value: 'Renamed' } })
    fireEvent.click(save())
    expect(await screen.findByText('the vault is locked')).toBeVisible()
    expect(screen.getByText(/any token already saved as SPRITES_TOKEN is unchanged/)).toBeVisible()
    // The server's own reason stands in for the retry advice: retrying would meet it again.
    expect(screen.queryByText(/Choose Save settings to try again/)).toBeNull()
    // ...and the fix that is on this card comes first: another secret name.
    expect(screen.getByText(
      'To save it under another name instead, change the name under Advanced: secret name below, then choose Save settings.',
    )).toBeVisible()
    expect(screen.getByTestId('sprites-token-reason')).toHaveTextContent('the vault is locked')
    // The collapsed header names no secret: it lives only under Advanced.
    expect(screen.getByRole('button', { name: /^Renamed/ })).not.toHaveTextContent('SPRITES_TOKEN')
    expect(screen.queryByText(/A token is saved as SPRITES_TOKEN\./)).toBeNull()
    expect(field).toHaveValue('new-token')
    // The configuration goes first, so the rename is stored; the refused token
    // stays typed for the next Save and "Settings saved." is not claimed.
    expect(writes).toHaveLength(1)
    expect(screen.queryByText('Settings saved.')).toBeNull()
  })

  it('writes no credential when the gateway refuses the configuration', async () => {
    // A token typed beside an invalid edit never overwrites the saved
    // credential: the configuration is refused before any vault write.
    const good = { id: 'fly', name: 'Fly', token_secret: 'SPRITES_TOKEN', workdir: '/w' }
    stored = { session_environments: { coder_url: '', profiles: [], sprites_profiles: [good] } }
    server.use(
      http.get('*/api/secrets', () => HttpResponse.json({ names: ['SPRITES_TOKEN'], managed: [] })),
      http.patch('*/api/config/kirocrew', () => HttpResponse.json({ error: 'sprites_profiles[0].id is invalid' }, { status: 400 })),
    )
    const saveToken = vi.spyOn(api, 'secretsSave')
    mount()
    await screen.findByLabelText('Coder URL')
    expand('Fly')
    fireEvent.change(screen.getByLabelText(TOKEN_LABEL), { target: { value: 'other-account-token' } })
    fireEvent.change(screen.getByLabelText('Profile ID'), { target: { value: 'bad id' } })
    fireEvent.click(save())
    expect(await screen.findByText(/sprites_profiles\[0\]\.id is invalid/)).toBeVisible()
    expect(saveToken).not.toHaveBeenCalled()
    expect(screen.getByLabelText(TOKEN_LABEL)).toHaveValue('other-account-token')
  })

  it("preserves all vault tokens when saved profiles are removed", async () => {
    const deleteSecret = vi.spyOn(api, 'secretsDelete')
    const a = { id: 'fly-a', name: 'Fly A', token_secret: 'TOKEN_A', workdir: '/w' }
    const b = { id: 'fly-b', name: 'Fly B', token_secret: 'SHARED', workdir: '/w' }
    const c = { id: 'fly-c', name: 'Fly C', token_secret: 'SHARED', workdir: '/w' }
    stored = { session_environments: { coder_url: '', profiles: [], sprites_profiles: [a, b, c] } }
    const deleted: string[] = []
    server.use(
      http.get('*/api/secrets', () => HttpResponse.json({ names: ['TOKEN_A', 'SHARED'], managed: [] })),
      http.delete('*/api/secrets/:name', ({ params }) => { deleted.push(String(params.name)); return HttpResponse.json({ ok: true }) }),
    )
    mount()
    await screen.findByLabelText('Coder URL')
    const removeProfile = (title: string) => {
      expand(title)
      const header = screen.getByRole('button', { name: new RegExp(`^${title}`) })
      const panel = header.closest('div')!.parentElement!
      fireEvent.click(within(panel).getByRole('button', { name: 'Remove profile' }))
    }
    removeProfile('Fly A')
    removeProfile('Fly B')
    expect(deleted).toEqual([])
    fireEvent.click(save())
    await screen.findByText('Settings saved.')
    expect(stored.session_environments?.sprites_profiles).toEqual([c])
    expect(deleteSecret).not.toHaveBeenCalled()
    expect(deleted).toEqual([])
  })

  it('picks a default environment across providers, and drops it with the profile', async () => {
    const fly = { id: 'fly', name: 'Fly', token_secret: 'SPRITES_TOKEN', workdir: '/w' }
    stored = { session_environments: { coder_url: 'https://coder.example.test', profiles: [{ ...profile }], sprites_profiles: [fly], default_profile: '' } }
    mount()
    await screen.findByLabelText('Coder URL')
    const defaults = screen.getByRole('region', { name: 'Global defaults' })
    fireEvent.click(within(defaults).getByRole('switch', { name: 'Start new chats on a profile' }))
    fireEvent.click(within(defaults).getByRole('combobox', { name: 'Default for new chats' }))
    fireEvent.click(await screen.findByRole('option', { name: 'Fly' }))
    fireEvent.click(save())
    await waitFor(() => expect(writes).toHaveLength(1))
    expect((writes[0] as { value: Section }).value.default_profile).toBe('fly')
    // Removing the default profile moves the default to the first remaining
    // profile of the same provider, then of the others: here none is left on
    // Sprites, so Coder's.
    fireEvent.click(screen.getByRole('radio', { name: 'Fly.io Sprites' }))
    expand('Fly')
    fireEvent.click(screen.getAllByRole('button', { name: 'Remove profile' }).find(b => !b.closest('[hidden]'))!)
    fireEvent.click(save())
    await waitFor(() => expect(writes).toHaveLength(2))
    expect((writes[1] as { value: Section }).value).toMatchObject({ default_profile: 'dev', sprites_profiles: [] })
  })

  it('moves a removed Sprites default to another Sprites profile before a Coder one', async () => {
    const a = { id: 'fly-a', name: 'Fly A', token_secret: 'SPRITES_TOKEN', workdir: '/w' }
    const b = { id: 'fly-b', name: 'Fly B', token_secret: 'SPRITES_TOKEN', workdir: '/w' }
    stored = { session_environments: { coder_url: 'https://coder.example.test', profiles: [{ ...profile }], sprites_profiles: [a, b], default_profile: 'fly-a' } }
    mount()
    await screen.findByLabelText('Coder URL')
    fireEvent.click(screen.getByRole('radio', { name: 'Fly.io Sprites' }))
    expand('Fly A')
    fireEvent.click(screen.getAllByRole('button', { name: 'Remove profile' }).find(btn => !btn.closest('[hidden]'))!)
    fireEvent.click(save())
    await waitFor(() => expect(writes).toHaveLength(1))
    expect((writes[0] as { value: Section }).value).toMatchObject({ default_profile: 'fly-b', sprites_profiles: [b] })
  })

  it('names each tab\'s own machine in the card intro, never a Coder workspace on a VM tab', async () => {
    stored = { session_environments: { coder_url: '', profiles: [], sprites_profiles: [], boxd_profiles: [] } }
    mount()
    await screen.findByLabelText('Coder URL')
    expect(screen.getByText(/Each chat on a profile gets its own Coder workspace/)).toBeVisible()
    fireEvent.click(screen.getByRole('radio', { name: 'Fly.io Sprites' }))
    expect(screen.getByText(/Each chat on a profile gets its own sprite, the machine/)).toBeVisible()
    expect(screen.queryByText(/its own Coder workspace, the machine/)).toBeNull()
    fireEvent.click(screen.getByRole('radio', { name: 'boxd', exact: true }))
    expect(screen.getByText(/Each chat on a profile gets its own boxd machine, the machine/)).toBeVisible()
  })

  it('carries the default along when a Sprites or boxd profile id is renamed', async () => {
    const fly = { id: 'fly', name: 'Fly', token_secret: 'SPRITES_TOKEN', workdir: '/w' }
    const box = { id: 'box', name: 'Box', snapshot: '', vcpu: 0, workdir: '/w' }
    for (const [tab, name, id, renamed] of [['Fly.io Sprites', 'Fly', 'fly', 'fly-2'], ['boxd', 'Box', 'box', 'box-2']]) {
      writes.length = 0
      stored = { session_environments: { coder_url: '', profiles: [], sprites_profiles: [fly], boxd_profiles: [box], default_profile: id } }
      const view = mount()
      await screen.findByLabelText('Coder URL')
      fireEvent.click(screen.getByRole('radio', { name: tab, exact: true }))
      expand(name)
      const field = within(screen.getByRole('group', { name })).getByLabelText('Profile ID')
      fireEvent.change(field, { target: { value: renamed } })
      fireEvent.click(save())
      await waitFor(() => expect(writes).toHaveLength(1))
      expect((writes[0] as { value: Section }).value.default_profile).toBe(renamed)
      view.unmount()
    }
  })

  it('adds a boxd profile, where a snapshot keeps its own size, and checks it', async () => {
    stored = { session_environments: { coder_url: '', profiles: [], sprites_profiles: [], boxd_profiles: [] } }
    server.use(http.post('*/api/session-environments/check', () => HttpResponse.json({
      coder: { ok: false }, profiles: [], git: {}, boxd: [{ id: 'boxd-1', ok: true }],
    })))
    mount()
    await screen.findByLabelText('Coder URL')
    const radio = screen.getByRole('radio', { name: 'boxd' })
    // The boxd mark is decorative; the text label stays the choice's name.
    expect(radio.closest('label')!.querySelector('span[aria-hidden="true"]')).not.toBeNull()
    fireEvent.click(radio)
    addVisible()
    fireEvent.click(screen.getByRole('combobox', { name: 'Machine size' }))
    fireEvent.click(await screen.findByRole('option', { name: '2 vCPU, 8 GiB' }))
    // Naming a snapshot returns the size to the default, which boxd requires.
    fireEvent.change(screen.getByLabelText('Start from a saved boxd snapshot (optional)'), { target: { value: 'my-workspace' } })
    expect(screen.getByRole('combobox', { name: 'Machine size' })).toBeDisabled()
    fireEvent.click(save())
    await waitFor(() => expect(writes).toHaveLength(1))
    expect((writes[0] as { value: Section }).value.boxd_profiles).toEqual([
      { id: 'boxd-1', name: 'boxd profile 1', snapshot: 'my-workspace', vcpu: 0, workdir: '' },
    ])
    const check = await screen.findByRole('button', { name: 'Check boxd connection' })
    await waitFor(() => expect(check).toBeEnabled())
    fireEvent.click(check)
    const results = await screen.findByRole('list', { name: 'Check results' })
    expect(results).toHaveTextContent('boxd profile 1: signed in to boxd on this computer')
  })

  it("lists only the open provider's workspaces", async () => {
    const box = { id: 'bx', name: 'Box', snapshot: '', vcpu: 0, workdir: '/w' }
    stored = { session_environments: { coder_url: 'https://coder.example.test', profiles: [{ ...profile }], boxd_profiles: [box] } }
    server.use(http.get('*/api/session-environments/workspaces', () => HttpResponse.json({ workspaces: [
      { workspace: 'crewenv-1', slot: 'a', state: 'running', template: 'python-dev', created_at: '', deletion_due_at: '' },
      { workspace: 'kc-0123456789abcdef', provider: 'boxd', slot: 'b', state: '', template: 'bx', created_at: '', deletion_due_at: '' },
    ] })))
    mount()
    const manage = await screen.findByRole('region', { name: /Manage/ })
    // The provider cards only switch the settings shown; the hint says so.
    expect(screen.getByText(/Profiles from every provider stay saved/)).toBeVisible()
    expect(await within(manage).findByText('crewenv-1')).toBeVisible()
    expect(within(manage).queryByText('kc-0123456789abcdef')).toBeNull()
    fireEvent.click(screen.getByRole('radio', { name: 'boxd' }))
    expect(await within(manage).findByText('kc-0123456789abcdef')).toBeVisible()
    expect(within(manage).getByText('In use by a chat')).toBeVisible()
    expect(within(manage).queryByText('crewenv-1')).toBeNull()
  })
})


describe('Sprites vault ownership and read failures', () => {
  const fly = { id: 'fly', name: 'Fly', token_secret: 'SHARED', workdir: '/w' }

  it('keeps a pre-existing secret when an unsaved profile is removed', async () => {
    const deleteSecret = vi.spyOn(api, 'secretsDelete')
    stored = { session_environments: { coder_url: '', profiles: [], sprites_profiles: [] } }
    server.use(http.get('*/api/secrets', () => HttpResponse.json({ names: ['SHARED'], managed: [] })))
    mount()
    await screen.findByLabelText('Coder URL')
    fireEvent.click(screen.getByRole('radio', { name: 'Fly.io Sprites' }))
    addVisible()
    fireEvent.change(screen.getByLabelText('Secret name for the token (usually SPRITES_TOKEN)'), { target: { value: 'SHARED' } })
    await screen.findByText(/A token is saved as SHARED/)
    fireEvent.click(screen.getByRole('button', { name: 'Remove profile' }))
    expect(screen.queryByLabelText(TOKEN_LABEL)).not.toBeInTheDocument()
    expect(deleteSecret).not.toHaveBeenCalled()
    expect(writes).toEqual([])
  })

  it('keeps the old vault entry when a profile secret name is changed and saved', async () => {
    const deleteSecret = vi.spyOn(api, 'secretsDelete')
    stored = { session_environments: { coder_url: '', profiles: [], sprites_profiles: [fly] } }
    server.use(http.get('*/api/secrets', () => HttpResponse.json({ names: ['SHARED'], managed: [] })))
    mount()
    await screen.findByLabelText('Coder URL')
    expand('Fly')
    await screen.findByText(/A token is saved as SHARED/)
    fireEvent.change(screen.getByLabelText('Secret name for the token (usually SPRITES_TOKEN)'), { target: { value: 'OTHER' } })
    fireEvent.click(save())
    await screen.findByText('Settings saved.')
    expect(stored.session_environments?.sprites_profiles?.[0].token_secret).toBe('OTHER')
    expect(deleteSecret).not.toHaveBeenCalled()
  })

  it.each([false, true])('surfaces a vault listing failure and retries without losing drafts (stale data: %s)', async stale => {
    stored = { session_environments: { coder_url: '', profiles: [], sprites_profiles: [fly] } }
    server.use(http.get('*/api/secrets', () => HttpResponse.json({ error: 'offline' }, { status: 500 })))
    mount()
    await screen.findByLabelText('Coder URL')
    if (stale) await act(async () => {
      qc.setQueryData(['secrets'], { names: ['SHARED'], managed: [] })
      // The mounted field must observe a failed refetch with cached names.
      await qc.invalidateQueries({ queryKey: ['secrets'] })
    })
    expand('Fly')
    const error = await screen.findByText("Couldn't check whether a token is saved. Retry to check again.")
    // With nothing cached, the field stays neutral: the notice below says it could not look.
    if (!stale) expect(screen.getByLabelText(TOKEN_LABEL)).toHaveAttribute('placeholder', 'Paste a Sprites API token')
    expect(error.closest('[role="alert"]')).not.toBeNull()
    expect(screen.queryByText(/No token is saved as SHARED yet/)).not.toBeInTheDocument()
    expect(screen.queryByText(/A token is saved as SHARED/)).not.toBeInTheDocument()
    fireEvent.change(screen.getByLabelText(TOKEN_LABEL), { target: { value: 'unsaved-token' } })
    fireEvent.change(screen.getByLabelText('Profile name'), { target: { value: 'Draft name' } })
    server.use(http.get('*/api/secrets', () => HttpResponse.json({ names: ['SHARED'], managed: [] })))
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await screen.findByText(/A token is saved as SHARED/)
    expect(screen.getByLabelText(TOKEN_LABEL)).toHaveValue('unsaved-token')
    expect(screen.getByLabelText('Profile name')).toHaveValue('Draft name')
    expect(screen.queryByText("Couldn't check whether a token is saved. Retry to check again.")).not.toBeInTheDocument()
    expect(writes).toEqual([])
  })
})

describe('Profile draft identity', () => {
  it.each([false, true])('saves a typed token with the profile, never into config, and keeps it when the vault refuses (new profile: %s)', async newProfile => {
    const fly = { id: 'fly', name: 'Fly', token_secret: 'TOKEN', workdir: '/workspace/sprite' }
    stored = { session_environments: { coder_url: '', profiles: [], sprites_profiles: newProfile ? [] : [fly] } }
    const vault: { name: string; value: string }[] = []
    server.use(
      http.get('*/api/secrets', () => HttpResponse.json({ names: vault.map(row => row.name), managed: [] })),
      http.post('*/api/secrets', () => HttpResponse.json({ error: 'vault unavailable' }, { status: 500 })),
    )
    mount()
    await screen.findByLabelText('Coder URL')
    fireEvent.click(screen.getByRole('radio', { name: 'Fly.io Sprites' }))
    if (newProfile) addVisible()
    else expand('Fly')
    const token = screen.getByLabelText(TOKEN_LABEL)
    fireEvent.change(token, { target: { value: 'unsaved-token' } })
    fireEvent.change(screen.getByLabelText('Profile ID'), { target: { value: 'renamed-fly' } })
    fireEvent.change(screen.getByLabelText('Profile name'), { target: { value: 'Renamed Fly' } })
    // The configuration is stored first, so a refused token keeps it saved and
    // leaves only the token typed for the next Save.
    fireEvent.click(save())
    await screen.findByText(/^The token was not saved, so any token already saved as/)
    expect(writes).toHaveLength(1)
    expect(JSON.stringify(writes)).not.toContain('unsaved-token')
    expect(screen.queryByText('Settings saved.')).toBeNull()
    expect(screen.getByLabelText(TOKEN_LABEL)).toBe(token)
    expect(token).toHaveValue('unsaved-token')
    server.use(http.post('*/api/secrets', async ({ request }) => {
      vault.push(await request.json() as { name: string; value: string })
      return HttpResponse.json({ ok: true })
    }))
    fireEvent.click(save())
    await screen.findByText('Settings saved.')
    expect(vault).toEqual([{ name: newProfile ? 'SPRITES_TOKEN' : 'TOKEN', value: 'unsaved-token' }])
    expect(writes).toHaveLength(1)
    expect(JSON.stringify(writes)).not.toContain('unsaved-token')
    expect(JSON.stringify(qc.getQueryData(['kirocrewConfig']))).not.toContain('unsaved-token')
    expand('Renamed Fly')
    // The same editor row survives the save, now empty.
    expect(screen.getByLabelText(TOKEN_LABEL)).toBe(token)
    expect(token).toHaveValue('')
    expect(screen.queryByTestId('sprites-profile-unsaved')).not.toBeInTheDocument()
    expect(save()).toBeDisabled()
  })

  it('keeps each token on its own row when a config-save response reorders profiles', async () => {
    const a = { id: 'fly-a', name: 'Fly A', token_secret: 'TOKEN_A', workdir: '/workspace/a' }
    const b = { id: 'fly-b', name: 'Fly B', token_secret: 'TOKEN_B', workdir: '/workspace/b' }
    stored = { session_environments: { coder_url: '', profiles: [], sprites_profiles: [a, b] } }
    const vault = new Map<string, string>()
    server.use(
      http.get('*/api/secrets', () => HttpResponse.json({ names: [...vault.keys()], managed: [] })),
      http.post('*/api/secrets', async ({ request }) => {
        const { name, value } = await request.json() as { name: string; value: string }
        vault.set(name, value)
        return HttpResponse.json({ ok: true })
      }),
      http.patch('*/api/config/kirocrew', async ({ request }) => {
        const body = await request.json() as { path: string; value: Section }
        writes.push(body)
        stored = { session_environments: { ...body.value, sprites_profiles: [...body.value.sprites_profiles!].reverse() } }
        return HttpResponse.json(stored)
      }),
    )
    mount()
    await screen.findByLabelText('Coder URL')
    expandAll()
    const [tokenA, tokenB] = screen.getAllByLabelText(TOKEN_LABEL)
    fireEvent.change(tokenA, { target: { value: 'draft-for-a' } })
    fireEvent.change(tokenB, { target: { value: 'draft-for-b' } })
    fireEvent.change(screen.getAllByLabelText('Profile name')[0], { target: { value: 'Renamed A' } })
    fireEvent.click(save())
    await screen.findByText('Settings saved.')
    // Each token went to its own profile's secret, before the reorder came back.
    expect(Object.fromEntries(vault)).toEqual({ TOKEN_A: 'draft-for-a', TOKEN_B: 'draft-for-b' })
    expandAll()
    expect(screen.getAllByLabelText(TOKEN_LABEL)).toEqual([tokenB, tokenA])
    expect(within(screen.getByRole('group', { name: 'Renamed A' })).getByLabelText(TOKEN_LABEL)).toBe(tokenA)
    expect(within(screen.getByRole('group', { name: 'Fly B' })).getByLabelText(TOKEN_LABEL)).toBe(tokenB)
  })

  it('keeps a token draft mounted across a settings refetch without configuration edits', async () => {
    const fly = { id: 'fly', name: 'Fly', token_secret: 'TOKEN', workdir: '/workspace/sprite' }
    stored = { session_environments: { coder_url: '', profiles: [], sprites_profiles: [fly] } }
    server.use(http.get('*/api/secrets', () => HttpResponse.json({ names: [], managed: [] })))
    mount()
    await screen.findByLabelText('Coder URL')
    expand('Fly')
    const token = screen.getByLabelText(TOKEN_LABEL)
    fireEvent.change(token, { target: { value: 'unsaved-token' } })
    stored.session_environments!.sprites_profiles = [{ ...fly, name: 'Updated Fly' }]
    await act(async () => { await qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }) })
    await waitFor(() => expect(screen.getByLabelText('Profile name')).toHaveValue('Updated Fly'))
    expect(screen.getByLabelText(TOKEN_LABEL)).toBe(token)
    expect(token).toHaveValue('unsaved-token')
    // A typed token is an unsaved edit: Save is offered, nothing was written.
    expect(save()).toBeEnabled()
    expect(writes).toEqual([])
  })
  it('never saves a typed token under a secret name a refresh moved it to', async () => {
    const fly = { id: 'fly', name: 'Fly', token_secret: 'TOKEN_A', workdir: '/workspace/sprite' }
    stored = { session_environments: { coder_url: '', profiles: [], sprites_profiles: [fly] } }
    const vault = new Map([['TOKEN_B', 'other-account']])
    server.use(
      http.get('*/api/secrets', () => HttpResponse.json({ names: [...vault.keys()], managed: [] })),
      http.post('*/api/secrets', async ({ request }) => {
        const { name, value } = await request.json() as { name: string; value: string }
        vault.set(name, value)
        return HttpResponse.json({ ok: true })
      }),
    )
    mount()
    await screen.findByLabelText('Coder URL')
    expand('Fly')
    fireEvent.change(screen.getByLabelText(TOKEN_LABEL), { target: { value: 'typed-for-a' } })
    // Another window rebinds the profile to TOKEN_B while the token is typed.
    stored.session_environments!.sprites_profiles = [{ ...fly, token_secret: 'TOKEN_B' }]
    await act(async () => { await qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }) })
    await waitFor(() => expect(screen.getByLabelText(/Secret name for the token/)).toHaveValue('TOKEN_B'))
    fireEvent.click(save())
    expect(await screen.findByText(/The secret name changed to TOKEN_B after you typed the token/)).toBeVisible()
    expect(vault.get('TOKEN_B')).toBe('other-account')
    expect(vault.has('TOKEN_A')).toBe(false)
    expect(screen.getByLabelText(TOKEN_LABEL)).toHaveValue('typed-for-a')
    // Saving again, after the notice, stores it under the name now shown.
    fireEvent.click(save())
    await screen.findByText('Settings saved.')
    expect(vault.get('TOKEN_B')).toBe('typed-for-a')
  })
  it.each(['', 'draft-for-b'])('removing a profile never transfers its token to the next row (%s)', async draftForB => {
    const a = { id: 'fly-a', name: 'Fly A', token_secret: 'TOKEN_A', workdir: '/w' }
    const b = { id: 'fly-b', name: 'Fly B', token_secret: 'TOKEN_B', workdir: '/w' }
    stored = { session_environments: { coder_url: '', profiles: [], sprites_profiles: [a, b] } }
    const vault = new Map([['TOKEN_A', 'stored-a'], ['TOKEN_B', 'stored-b']])
    server.use(
      http.get('*/api/secrets', () => HttpResponse.json({ names: [...vault.keys()], managed: [] })),
      http.post('*/api/secrets', async ({ request }) => {
        const { name, value } = await request.json() as { name: string; value: string }
        vault.set(name, value)
        return HttpResponse.json({ ok: true })
      }),
    )
    mount()
    await screen.findByLabelText('Coder URL')
    expand('Fly A')
    expand('Fly B')
    const rowA = within(screen.getByRole('group', { name: 'Fly A' }))
    const rowB = within(screen.getByRole('group', { name: 'Fly B' }))
    const inputA = rowA.getByLabelText(TOKEN_LABEL)
    const inputB = rowB.getByLabelText(TOKEN_LABEL)
    fireEvent.change(inputA, { target: { value: 'draft-for-a' } })
    fireEvent.change(inputB, { target: { value: draftForB } })
    // Editing the public id must not remount the token field or disclosure.
    fireEvent.change(rowB.getByLabelText('Profile ID'), { target: { value: 'renamed-b' } })
    expect(rowB.getByLabelText(TOKEN_LABEL)).toBe(inputB)
    fireEvent.click(rowA.getByRole('button', { name: 'Remove profile' }))
    expect(inputA).not.toBeInTheDocument()
    expect(screen.getByLabelText(TOKEN_LABEL)).toBe(inputB)
    expect(inputB).toHaveValue(draftForB)
    expect(screen.getByRole('button', { name: /^Fly B/ })).toHaveAttribute('aria-expanded', 'true')
    fireEvent.click(save())
    await screen.findByText('Settings saved.')
    // The removed row's token was never saved; B's only when one was typed.
    expect(vault.get('TOKEN_A')).toBe('stored-a')
    expect(vault.get('TOKEN_B')).toBe(draftForB || 'stored-b')
    expect(stored.session_environments?.sprites_profiles).toEqual([{ ...b, id: 'renamed-b' }])
    expect(JSON.stringify(writes)).not.toContain('draft-for-a')
  })

  it('does not give a newly added profile the removed row\'s token', async () => {
    stored = { session_environments: { coder_url: '', profiles: [], sprites_profiles: [] } }
    server.use(http.get('*/api/secrets', () => HttpResponse.json({ names: [], managed: [] })))
    mount()
    await screen.findByLabelText('Coder URL')
    fireEvent.click(screen.getByRole('radio', { name: 'Fly.io Sprites' }))
    addVisible()
    const field = screen.getByLabelText(TOKEN_LABEL)
    fireEvent.change(field, { target: { value: 'removed-token' } })
    fireEvent.click(screen.getByRole('button', { name: 'Remove profile' }))
    addVisible()
    expect(screen.getByLabelText('Profile ID')).toHaveValue('sprites-1')
    expect(screen.getByLabelText(TOKEN_LABEL)).not.toBe(field)
    expect(screen.getByLabelText(TOKEN_LABEL)).toHaveValue('')
    expect(screen.queryByRole('button', { name: 'Save token' })).not.toBeInTheDocument()
    expect(writes).toEqual([])
  })
})
