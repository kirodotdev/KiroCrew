import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { http, HttpResponse } from 'msw'
import { server } from '../../integration/mocks/server'
import { api } from '../api/client'
import { SessionEnvironmentsCard } from '../pages/settings/SessionEnvironmentsCard'
import { FeaturePreviewsSection } from '../pages/settings/FeaturePreviewsSection'
import { PREVIEW_SESSION_ENVIRONMENTS } from '../utils/previewFlags'
import { settingEntryOffered } from '../components/commandPalette/settingsSearchCore'
import { SETTINGS_REGISTRY } from '../components/commandPalette/settingsRegistry.gen'

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
  archived_retention_days?: number
}
let stored: { session_environments?: Section }
let reads: number
let writes: unknown[]
let qc: QueryClient

function mount(enabled = true) {
  if (enabled) localStorage.setItem(PREVIEW_SESSION_ENVIRONMENTS, '1')
  qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(<QueryClientProvider client={qc}><MemoryRouter><SessionEnvironmentsCard enabled={enabled} /></MemoryRouter></QueryClientProvider>)
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
  )
})
afterEach(() => { cleanup(); qc?.clear(); localStorage.clear(); vi.restoreAllMocks() })

const url = () => screen.getByLabelText('Coder URL')
/** The section's own Save (an unsaved Sprites profile adds a second, same action). */
const save = () => screen.getAllByRole('button', { name: 'Save configuration' }).at(-1)!
/** The visible tab's Add button (each provider section has one). */
const addVisible = () => fireEvent.click(screen.getAllByRole('button', { name: 'Add profile' }).find(b => !b.closest('[hidden]'))!)
const toggle = () => screen.getByRole('switch', { name: 'Session Environments' })
/** Saved profiles start collapsed; open one by its header (name, then subtitle). */
const expand = (title: string) => fireEvent.click(screen.getByRole('button', { name: new RegExp(`^${title}`) }))
const expandAll = () => screen.getAllByRole('button', { expanded: false }).forEach(header => fireEvent.click(header))

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

  it('does not load the configuration when disabled', () => {
    mount(false)
    expect(reads).toBe(0)
    expect(screen.queryByLabelText('Coder URL')).not.toBeInTheDocument()
  })

  it('writes the complete section only on Save and reloads saved profiles', async () => {
    const first = mount()
    await screen.findByLabelText('Coder URL')
    fireEvent.change(url(), { target: { value: 'https://new.example.test' } })
    expand('Development')
    fireEvent.change(screen.getByLabelText('Coder template'), { target: { value: 'node-dev' } })
    expect(writes).toEqual([])
    fireEvent.click(save())
    expect(await screen.findByText('Configuration saved. No compute was started.')).toBeVisible()
    expect(writes).toEqual([{ path: 'session_environments', value: {
      coder_url: 'https://new.example.test', profiles: [{ ...profile, template: 'node-dev' }],
    } }])
    expect(save()).toBeDisabled()
    first.unmount(); qc.clear()
    mount()
    expect(await screen.findByLabelText('Coder URL')).toHaveValue('https://new.example.test')
    expand('Development')
    expect(screen.getByLabelText('Coder template')).toHaveValue('node-dev')
  })

  it('round-trips a per-profile "files survive a stop" switch, on by default', async () => {
    const first = mount()
    await screen.findByLabelText('Coder URL')
    expand('Development')
    const survives = () => screen.getByRole('switch', { name: 'Files survive a workspace stop' })
    expect(survives()).toBeChecked()
    expect(screen.getByText(/such as MicroVM/)).toBeInTheDocument()
    fireEvent.click(survives())
    expect(survives()).not.toBeChecked()
    fireEvent.click(save())
    expect(await screen.findByText('Configuration saved. No compute was started.')).toBeVisible()
    expect(writes).toEqual([{ path: 'session_environments', value: {
      coder_url: 'https://coder.example.test', profiles: [{ ...profile, compute_survives_stop: false }],
    } }])
    first.unmount(); qc.clear()
    mount()
    await screen.findByLabelText('Coder URL')
    expand('Development')
    expect(survives()).not.toBeChecked()
  })

  it('keeps archived session environments for 0-7 days and saves it with the section', async () => {
    stored.session_environments = { ...stored.session_environments!, archived_retention_days: 6 }
    mount()
    await screen.findByLabelText('Coder URL')
    // Section level, so it is visible without opening a profile.
    const group = screen.getByText('Days to keep archived session environments').closest('div')!.parentElement!
    expect(group).toHaveTextContent('6')
    const increase = screen.getByRole('button', { name: 'Increase' })
    for (let i = 0; i < 4; i++) fireEvent.click(increase)
    fireEvent.click(save())
    await screen.findByText('Configuration saved. No compute was started.')
    expect(writes).toEqual([{ path: 'session_environments', value: {
      coder_url: 'https://coder.example.test', profiles: [{ ...profile }], archived_retention_days: 7,
    } }])
  })

  it('hides the retention control for a gateway that does not report it', async () => {
    mount()
    await screen.findByLabelText('Coder URL')
    expect(screen.queryByText('Days to keep archived session environments')).not.toBeInTheDocument()
  })

  it('keeps edits across refetch and discards explicitly', async () => {
    mount()
    await screen.findByLabelText('Coder URL')
    fireEvent.change(url(), { target: { value: 'https://draft.example.test' } })
    act(() => qc.setQueryData(['kirocrewConfig'], { session_environments: { coder_url: 'https://other.example.test', profiles: [] } }))
    expect(url()).toHaveValue('https://draft.example.test')
    fireEvent.click(screen.getByRole('button', { name: 'Discard changes' }))
    expect(url()).toHaveValue('https://other.example.test')
    expect(writes).toEqual([])
  })

  it('adds unique profile drafts and removes them without provisioning', async () => {
    stored.session_environments!.profiles.push({ ...profile, id: 'profile-1' })
    mount()
    await screen.findByLabelText('Coder URL')
    fireEvent.click(screen.getByRole('button', { name: 'Add profile' }))
    expandAll()
    expect(screen.getAllByLabelText('Profile ID').map(input => (input as HTMLInputElement).value)).toEqual(['dev', 'profile-1', 'profile-2'])
    fireEvent.click(screen.getAllByRole('button', { name: 'Remove profile' })[2])
    expect(screen.getAllByLabelText('Profile ID')).toHaveLength(2)
    expect(writes).toEqual([])
  })

  it('retains a rejected draft and shows the backend validation detail', async () => {
    server.use(http.patch('*/api/config/kirocrew', () => HttpResponse.json({ error: 'profiles[0].id is a duplicate' }, { status: 400 })))
    mount()
    await screen.findByLabelText('Coder URL')
    fireEvent.change(url(), { target: { value: 'https://draft.example.test' } })
    fireEvent.click(save())
    expect(await screen.findByText('profiles[0].id is a duplicate')).toBeVisible()
    expect(url()).toHaveValue('https://draft.example.test')
    expect(save()).toBeEnabled()
    expect(screen.queryByText('Configuration saved. No compute was started.')).not.toBeInTheDocument()
  })

  it('does not fabricate settings support on an older gateway', async () => {
    stored = {}
    mount()
    expect(await screen.findByText(/This gateway does not support Session Environments configuration/)).toBeVisible()
    expect(screen.queryByLabelText('Coder URL')).not.toBeInTheDocument()
    expect(writes).toEqual([])
  })

  it('retries loading after a failed configuration read', async () => {
    server.use(http.get('*/api/config/kirocrew', () => HttpResponse.json({ error: 'offline' }, { status: 500 })))
    mount()
    expect(await screen.findByText('Could not load environment settings.')).toBeVisible()
    server.use(http.get('*/api/config/kirocrew', () => HttpResponse.json(stored)))
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await screen.findByLabelText('Coder URL')
    expect(save()).toBeDisabled()
  })

  it.each([false, true])('reports failed refreshes without losing cached settings or drafts (draft=%s)', async draft => {
    mount()
    await screen.findByLabelText('Coder URL')
    const value = draft ? 'https://draft.example.test' : stored.session_environments!.coder_url
    if (draft) fireEvent.change(url(), { target: { value } })
    server.use(http.get('*/api/config/kirocrew', () => HttpResponse.json({ error: 'offline' }, { status: 500 })))
    await act(async () => { await qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }) })
    const notice = await screen.findByText('Could not load environment settings.')
    expect(notice).toBeVisible()
    expect(url()).toHaveValue(value)
    expect(save().hasAttribute('disabled')).toBe(!draft)
    expect(within(notice.closest('[role="alert"]') as HTMLElement).queryByRole('button', { name: /Ask the agent/i })).not.toBeInTheDocument()
    server.use(http.get('*/api/config/kirocrew', () => HttpResponse.json(stored)))
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(screen.queryByText('Could not load environment settings.')).not.toBeInTheDocument())
    expect(url()).toHaveValue(value)
    expect(writes).toEqual([])
    if (draft) {
      fireEvent.click(save())
      await screen.findByText('Configuration saved. No compute was started.')
      expect(writes).toEqual([{ path: 'session_environments', value: { ...stored.session_environments, coder_url: value } }])
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
    mount()
    await screen.findByLabelText('Coder URL')
    fireEvent.change(url(), { target: { value: 'https://draft.example.test' } })
    fireEvent.click(save())
    await waitFor(() => expect(url()).toBeDisabled())
    expect(save()).toBeDisabled()
    await act(async () => finish())
    await screen.findByText('Configuration saved. No compute was started.')
    expect(writes).toHaveLength(1)
  })
})

describe('Coder setup and saved profile management', () => {
  it('shows Coder setup without claiming a live connection', async () => {
    mount()
    await screen.findByLabelText('Coder URL')
    // jsdom keeps closed <details> content in the tree, so assert the
    // disclosure state itself: instructions start collapsed.
    const instructions = screen.getByText('Setup instructions', { exact: true }).closest('details')!
    expect(instructions.open).toBe(false)
    instructions.open = true
    expect(screen.getByRole('link', { name: 'Open provider setup documentation' })).toHaveAttribute('href', 'https://coder.com/docs/admin/setup')
    // A saved Coder setup opens on the Coder tab.
    expect(screen.getByRole('radio', { name: 'Coder', exact: true })).toBeChecked()
    expect(screen.getByRole('radio', { name: 'Fly.io Sprites' })).not.toBeChecked()
    expect(screen.getByText(/A session runs on a profile once you choose it in the chat composer/)).toBeVisible()
    expect(writes).toEqual([])
  })

  it('collapses saved profiles, opens new ones, and keeps edits across collapse', async () => {
    mount()
    await screen.findByLabelText('Coder URL')
    const header = screen.getByRole('button', { name: /^Development/ })
    expect(header).toHaveAttribute('aria-expanded', 'false')
    expect(header).toHaveTextContent('python-dev')
    expect(screen.getByLabelText('Coder template')).not.toBeVisible()
    fireEvent.click(header)
    expect(header).toHaveAttribute('aria-expanded', 'true')
    fireEvent.change(screen.getByLabelText('Coder template'), { target: { value: 'node-dev' } })
    fireEvent.click(header)
    expect(screen.getByLabelText('Coder template')).not.toBeVisible()
    expect(header).toHaveTextContent('node-dev')
    fireEvent.click(header)
    expect(screen.getByLabelText('Coder template')).toHaveValue('node-dev')
    fireEvent.click(screen.getByRole('button', { name: 'Add profile' }))
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
    const field = screen.getByLabelText('API token')
    fireEvent.change(field, { target: { value: 'unsaved-token' } })
    fireEvent.click(header)
    const body = document.getElementById(header.getAttribute('aria-controls')!)!
    expect(body).toHaveAttribute('hidden')
    expect(body).toHaveAttribute('inert')
    expect(field).toBeInTheDocument()
    expect(field).not.toBeVisible()
    expect(screen.queryByRole('button', { name: 'Save token' })).toBeNull()
    fireEvent.click(header)
    expect(screen.getByLabelText('API token')).toBe(field)
    await waitFor(() => expect(field).toBeVisible())
    expect(field).toHaveValue('unsaved-token')
    expect(body).not.toHaveAttribute('inert')
    expect(saveToken).not.toHaveBeenCalled()
    expect(writes).toEqual([])
  })

  it('keeps configure, verify and manage on one page without treating a save as readiness', async () => {
    mount()
    await screen.findByLabelText('Coder URL')
    expect(screen.queryByRole('tablist')).not.toBeInTheDocument()
    for (const name of ['1. Connect and configure', '2. Verify readiness', '3. Manage']) {
      expect(screen.getByRole('region', { name })).toBeVisible()
    }
    fireEvent.change(url(), { target: { value: 'https://changed.example.test' } })
    // An unsaved draft cannot be checked: the check reads what the gateway saved.
    expect(screen.getByRole('button', { name: 'Check configuration' })).toBeDisabled()
    expect(screen.getByText('Save your changes to check them.')).toBeVisible()
    fireEvent.click(save())
    await screen.findByText('Configuration saved. No compute was started.')
    expect(screen.getByRole('button', { name: 'Check configuration' })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Check configuration' })).toHaveAttribute('aria-describedby')
    // The controls that did nothing are gone.
    for (const name of ['Start session', 'Manage resources']) {
      expect(screen.queryByRole('button', { name })).not.toBeInTheDocument()
    }
    expect(writes).toHaveLength(1)
  })

  it('cannot check before a Coder URL is saved', async () => {
    stored = { session_environments: { coder_url: '', profiles: [] } }
    mount()
    await screen.findByLabelText('Coder URL')
    expect(screen.getByRole('button', { name: 'Check configuration' })).toBeDisabled()
    expect(screen.queryByRole('link', { name: 'Open Coder' })).toBeNull()
  })

  it('picks a default environment, and drops it with the profile', async () => {
    const second = { ...profile, id: 'gpu', name: 'GPU box' }
    stored = { session_environments: { coder_url: 'https://coder.example.test', profiles: [{ ...profile }, second], default_profile: '' } }
    mount()
    await screen.findByLabelText('Coder URL')
    // Global, above the profiles: off by default, no picker until it is on.
    const defaults = screen.getByRole('region', { name: 'Global defaults' })
    const toggle = within(defaults).getByRole('switch', { name: 'Start new chats in a session environment' })
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
    // Removing the default profile falls back to this machine in the same draft.
    expand('GPU box')
    const header = screen.getByRole('button', { name: /^GPU box/ })
    const panel = header.closest('div')!.parentElement!
    fireEvent.click(within(panel).getByRole('button', { name: 'Remove profile' }))
    fireEvent.click(save())
    await waitFor(() => expect(writes).toHaveLength(2))
    expect((writes[1] as { value: Section }).value).toMatchObject({ default_profile: '', profiles: [profile] })
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
    mount()
    await screen.findByLabelText('Coder URL')
    fireEvent.click(screen.getByRole('button', { name: 'Check configuration' }))
    const results = await screen.findByRole('list', { name: 'Check results' })
    expect(checks).toBe(1)
    expect(results).toHaveTextContent('Signed in to Coder as devuser')
    expect(results).toHaveTextContent('Development: Coder template is unavailable')
    const failure = within(results).getByRole('alert')
    expect(failure).toHaveTextContent('Development: Coder template is unavailable')
    expect(within(failure).getByRole('button', { name: /Ask the agent/ })).toBeVisible()
    expect(results).toHaveTextContent('GitHub: signed in on the gateway')
    expect(results).toHaveTextContent('Azure DevOps: not signed in on the gateway')
    expect(results).toHaveTextContent("bitbucket.org: uses the gateway's git credential helper, not checked")
    expect(writes).toEqual([])
  })

  it('surfaces failed Coder checks with a safe hand-off and drops them while editing', async () => {
    server.use(http.post('*/api/session-environments/check', () => HttpResponse.json({
      coder: { ok: false, error: 'Coder login expired' }, profiles: [], git: {},
    })))
    mount()
    await screen.findByLabelText('Coder URL')
    fireEvent.click(screen.getByRole('button', { name: 'Check configuration' }))
    const results = await screen.findByRole('list', { name: 'Check results' })
    const failure = within(results).getByRole('alert')
    expect(failure).toHaveTextContent('Coder login expired')
    expect(within(failure).getByRole('button', { name: /Ask the agent/ })).toBeVisible()
    fireEvent.change(url(), { target: { value: 'https://draft.example.test' } })
    expect(screen.queryByRole('list', { name: 'Check results' })).toBeNull()
  })

  it('reports a check that could not run through the error notice', async () => {
    server.use(http.post('*/api/session-environments/check', () => HttpResponse.json({ error: 'x' }, { status: 500 })))
    mount()
    await screen.findByLabelText('Coder URL')
    fireEvent.click(screen.getByRole('button', { name: 'Check configuration' }))
    expect(await screen.findByText('The check could not run.')).toBeVisible()
  })

  it('lists the workspaces Kiro Crew owns, with when an archived one is deleted', async () => {
    server.use(http.get('*/api/session-environments/workspaces', () => HttpResponse.json({
      workspaces: [
        { workspace: 'crewenv-devuser-aaaa', slot: 'chat-1', state: 'running', template: 't', created_at: '2026-10-09T10:00:00Z', deletion_due_at: '' },
        { workspace: 'crewenv-devuser-bbbb', slot: 'chat-2', state: 'running', template: 't', created_at: '2026-10-09T11:00:00Z', deletion_due_at: '2026-10-16T11:00:00Z' },
        { workspace: 'crewenv-devuser-cccc', slot: 'chat-3', state: 'deleting', template: 't', created_at: '2026-10-09T12:00:00Z', deletion_due_at: '' },
      ],
    })))
    mount()
    const list = await screen.findByRole('list', { name: 'Session environments' })
    const rows = list.querySelectorAll('li')
    expect(rows).toHaveLength(3)
    expect(rows[0]).toHaveTextContent('crewenv-devuser-aaaa')
    expect(rows[0]).toHaveTextContent('In use by a session')
    expect(rows[0]).not.toHaveTextContent('Will be deleted')
    expect(rows[1]).toHaveTextContent(/Will be deleted/)
    expect(rows[2]).toHaveTextContent('Being deleted')
    // The Coder deployment is one click away, opened safely in a new tab.
    const link = screen.getByRole('link', { name: 'Open Coder' })
    expect(link).toHaveAttribute('href', 'https://coder.example.test')
    expect(link).toHaveAttribute('rel', 'noopener noreferrer')
  })
})

describe('Sprites and boxd providers', () => {
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
    fireEvent.click(screen.getByRole('button', { name: 'Check configuration' }))
    const results = await screen.findByRole('list', { name: 'Check results' })
    expect(results).toHaveTextContent('Fly.io Sprites: Sprites API token accepted')
    expect(results).toHaveTextContent('GitHub: signed in on the gateway')
    expect(results).not.toHaveTextContent('Coder')
    // Switching back to Coder shows the Coder rows from the same result.
    fireEvent.click(screen.getByRole('radio', { name: 'Coder', exact: true }))
    expect(results).toHaveTextContent('Signed in to Coder as devuser')
    expect(results).not.toHaveTextContent('Sprites API token')
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
    expect(screen.getByLabelText('API token secret name')).toHaveValue('SPRITES_TOKEN')
    // Until the configuration is saved, the profile says it is not in the composer yet.
    expect(screen.getByTestId('sprites-profile-unsaved')).toHaveTextContent('This profile is not saved yet')
    fireEvent.click(save())
    await waitFor(() => expect(writes).toHaveLength(1))
    await waitFor(() => expect(screen.queryByTestId('sprites-profile-unsaved')).toBeNull())
    expect((writes[0] as { value: Section }).value.sprites_profiles).toEqual([
      { id: 'sprites-1', name: 'Fly.io Sprites', token_secret: 'SPRITES_TOKEN', workdir: '/home/sprite/workspace' },
    ])
    expect(JSON.stringify(writes[0])).not.toMatch(/"token"/)
    const check = await screen.findByRole('button', { name: 'Check configuration' })
    await waitFor(() => expect(check).toBeEnabled())
    fireEvent.click(check)
    // A failed row is a notice, with the gateway's reason.
    expect(await screen.findByText(/the Sprites API token was refused/)).toBeVisible()
  })

  it('saves a Sprites token to the vault from a masked field and never shows it back', async () => {
    const good = { id: 'fly', name: 'Fly', token_secret: 'SPRITES_TOKEN', workdir: '/home/sprite/workspace' }
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
    const field = screen.getByLabelText('API token')
    expect(field).toHaveAttribute('type', 'password')
    fireEvent.change(field, { target: { value: 'my-org/t1/secret-value' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save token' }))
    await waitFor(() => expect(vault).toEqual([{ name: 'SPRITES_TOKEN', value: 'my-org/t1/secret-value' }]))
    await waitFor(() => expect(field).toHaveValue(''))
    expect(await screen.findByText(/A token is saved in the gateway vault as SPRITES_TOKEN\./)).toBeVisible()
    // The token never rides on the profile draft or a config write.
    expect(writes).toEqual([])
    expect(document.body.innerHTML).not.toContain('secret-value')
    expect(screen.queryByRole('button', { name: 'Remove token' })).not.toBeInTheDocument()
    expect(screen.getByText(/Remove unused secrets manually in Settings → Secrets/)).toBeVisible()
    expect(vault).toHaveLength(1)
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
    await screen.findByText('Configuration saved. No compute was started.')
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
    fireEvent.click(within(defaults).getByRole('switch', { name: 'Start new chats in a session environment' }))
    fireEvent.click(within(defaults).getByRole('combobox', { name: 'Default for new chats' }))
    fireEvent.click(await screen.findByRole('option', { name: 'Fly' }))
    fireEvent.click(save())
    await waitFor(() => expect(writes).toHaveLength(1))
    expect((writes[0] as { value: Section }).value.default_profile).toBe('fly')
    // Removing the default profile falls back to this machine in the same draft.
    fireEvent.click(screen.getByRole('radio', { name: 'Fly.io Sprites' }))
    expand('Fly')
    fireEvent.click(screen.getAllByRole('button', { name: 'Remove profile' }).find(b => !b.closest('[hidden]'))!)
    fireEvent.click(save())
    await waitFor(() => expect(writes).toHaveLength(2))
    expect((writes[1] as { value: Section }).value).toMatchObject({ default_profile: '', sprites_profiles: [] })
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
    fireEvent.change(screen.getByLabelText('Snapshot to start from (optional)'), { target: { value: 'my-workspace' } })
    expect(screen.getByRole('combobox', { name: 'Machine size' })).toBeDisabled()
    fireEvent.click(save())
    await waitFor(() => expect(writes).toHaveLength(1))
    expect((writes[0] as { value: Section }).value.boxd_profiles).toEqual([
      { id: 'boxd-1', name: 'boxd', snapshot: 'my-workspace', vcpu: 0, workdir: '/home/boxd/workspace' },
    ])
    const check = await screen.findByRole('button', { name: 'Check configuration' })
    await waitFor(() => expect(check).toBeEnabled())
    fireEvent.click(check)
    const results = await screen.findByRole('list', { name: 'Check results' })
    expect(results).toHaveTextContent('boxd: signed in to boxd on the gateway')
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
    expect(await within(manage).findByText('crewenv-1')).toBeVisible()
    expect(within(manage).queryByText('kc-0123456789abcdef')).toBeNull()
    fireEvent.click(screen.getByRole('radio', { name: 'boxd' }))
    expect(await within(manage).findByText('kc-0123456789abcdef')).toBeVisible()
    expect(within(manage).getByText('Session machine')).toBeVisible()
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
    fireEvent.change(screen.getByLabelText('API token secret name'), { target: { value: 'SHARED' } })
    await screen.findByText(/A token is saved in the gateway vault as SHARED/)
    fireEvent.click(screen.getByRole('button', { name: 'Remove profile' }))
    expect(screen.queryByLabelText('API token')).not.toBeInTheDocument()
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
    await screen.findByText(/A token is saved in the gateway vault as SHARED/)
    fireEvent.change(screen.getByLabelText('API token secret name'), { target: { value: 'OTHER' } })
    fireEvent.click(save())
    await screen.findByText('Configuration saved. No compute was started.')
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
    const error = await screen.findByText('Could not read saved token names. Retry to check the vault.')
    expect(error.closest('[role="alert"]')).not.toBeNull()
    expect(screen.queryByText(/No token is saved as SHARED yet/)).not.toBeInTheDocument()
    expect(screen.queryByText(/A token is saved in the gateway vault as SHARED/)).not.toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('API token'), { target: { value: 'unsaved-token' } })
    fireEvent.change(screen.getByLabelText('Profile name'), { target: { value: 'Draft name' } })
    server.use(http.get('*/api/secrets', () => HttpResponse.json({ names: ['SHARED'], managed: [] })))
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await screen.findByText(/A token is saved in the gateway vault as SHARED/)
    expect(screen.getByLabelText('API token')).toHaveValue('unsaved-token')
    expect(screen.getByLabelText('Profile name')).toHaveValue('Draft name')
    expect(screen.queryByText('Could not read saved token names. Retry to check the vault.')).not.toBeInTheDocument()
    expect(writes).toEqual([])
  })
})

describe('Profile draft identity', () => {
  it.each([false, true])('keeps a token draft across configuration saves until its vault save succeeds (new profile: %s)', async newProfile => {
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
    const token = screen.getByLabelText('API token')
    fireEvent.change(token, { target: { value: 'unsaved-token' } })
    fireEvent.change(screen.getByLabelText('Profile ID'), { target: { value: 'renamed-fly' } })
    fireEvent.change(screen.getByLabelText('Profile name'), { target: { value: 'Renamed Fly' } })
    fireEvent.click(save())
    await screen.findByText('Configuration saved. No compute was started.')
    expand('Renamed Fly')
    expect(screen.getByLabelText('API token')).toBe(token)
    expect(token).toHaveValue('unsaved-token')
    expect(vault).toEqual([])
    expect(JSON.stringify(writes)).not.toContain('unsaved-token')
    expect(JSON.stringify(qc.getQueryData(['kirocrewConfig']))).not.toContain('unsaved-token')
    expect(screen.queryByTestId('sprites-profile-unsaved')).not.toBeInTheDocument()
    expect(save()).toBeDisabled()
    // An unchanged response still has to preserve the submitted editor row.
    fireEvent.change(screen.getByLabelText('Profile name'), { target: { value: 'Temporary name' } })
    fireEvent.change(screen.getByLabelText('Profile name'), { target: { value: 'Renamed Fly' } })
    fireEvent.click(save())
    await screen.findByText('Configuration saved. No compute was started.')
    expand('Renamed Fly')
    expect(screen.getByLabelText('API token')).toBe(token)
    expect(token).toHaveValue('unsaved-token')
    fireEvent.click(screen.getByRole('button', { name: 'Save token' }))
    await screen.findByText('The token could not be saved.')
    expect(token).toHaveValue('unsaved-token')
    server.use(http.post('*/api/secrets', async ({ request }) => {
      vault.push(await request.json() as { name: string; value: string })
      return HttpResponse.json({ ok: true })
    }))
    fireEvent.click(screen.getByRole('button', { name: 'Save token' }))
    await waitFor(() => expect(token).toHaveValue(''))
    expect(vault).toEqual([{ name: newProfile ? 'SPRITES_TOKEN' : 'TOKEN', value: 'unsaved-token' }])
    expect(writes).toHaveLength(2)
  })

  it('keeps each token on its own row when a config-save response reorders profiles', async () => {
    const a = { id: 'fly-a', name: 'Fly A', token_secret: 'TOKEN_A', workdir: '/workspace/a' }
    const b = { id: 'fly-b', name: 'Fly B', token_secret: 'TOKEN_B', workdir: '/workspace/b' }
    stored = { session_environments: { coder_url: '', profiles: [], sprites_profiles: [a, b] } }
    server.use(
      http.get('*/api/secrets', () => HttpResponse.json({ names: [], managed: [] })),
      http.patch('*/api/config/kirocrew', async ({ request }) => {
        const body = await request.json() as { path: string; value: Section }
        writes.push(body)
        stored = { session_environments: { ...body.value, sprites_profiles: [...body.value.sprites_profiles!].reverse() } }
        return HttpResponse.json(stored)
      }),
    )
    const saveToken = vi.spyOn(api, 'secretsSave')
    mount()
    await screen.findByLabelText('Coder URL')
    expandAll()
    const [tokenA, tokenB] = screen.getAllByLabelText('API token')
    fireEvent.change(tokenA, { target: { value: 'draft-for-a' } })
    fireEvent.change(tokenB, { target: { value: 'draft-for-b' } })
    fireEvent.change(screen.getAllByLabelText('Profile name')[0], { target: { value: 'Renamed A' } })
    fireEvent.click(save())
    await screen.findByText('Configuration saved. No compute was started.')
    expandAll()
    expect(screen.getAllByLabelText('API token')).toEqual([tokenB, tokenA])
    expect(within(screen.getByRole('group', { name: 'Renamed A' })).getByLabelText('API token')).toHaveValue('draft-for-a')
    expect(within(screen.getByRole('group', { name: 'Fly B' })).getByLabelText('API token')).toHaveValue('draft-for-b')
    expect(saveToken).not.toHaveBeenCalled()
  })

  it('keeps a token draft mounted across a settings refetch without configuration edits', async () => {
    const fly = { id: 'fly', name: 'Fly', token_secret: 'TOKEN', workdir: '/workspace/sprite' }
    stored = { session_environments: { coder_url: '', profiles: [], sprites_profiles: [fly] } }
    server.use(http.get('*/api/secrets', () => HttpResponse.json({ names: [], managed: [] })))
    mount()
    await screen.findByLabelText('Coder URL')
    expand('Fly')
    const token = screen.getByLabelText('API token')
    fireEvent.change(token, { target: { value: 'unsaved-token' } })
    stored.session_environments!.sprites_profiles = [{ ...fly, name: 'Updated Fly' }]
    await act(async () => { await qc.invalidateQueries({ queryKey: ['kirocrewConfig'] }) })
    await waitFor(() => expect(screen.getByLabelText('Profile name')).toHaveValue('Updated Fly'))
    expect(screen.getByLabelText('API token')).toBe(token)
    expect(token).toHaveValue('unsaved-token')
    expect(save()).toBeDisabled()
    expect(writes).toEqual([])
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
    const inputA = rowA.getByLabelText('API token')
    const inputB = rowB.getByLabelText('API token')
    fireEvent.change(inputA, { target: { value: 'draft-for-a' } })
    fireEvent.change(inputB, { target: { value: draftForB } })
    // Editing the public id must not remount the token field or disclosure.
    fireEvent.change(rowB.getByLabelText('Profile ID'), { target: { value: 'renamed-b' } })
    expect(rowB.getByLabelText('API token')).toBe(inputB)
    fireEvent.click(rowA.getByRole('button', { name: 'Remove profile' }))
    expect(inputA).not.toBeInTheDocument()
    expect(screen.getByLabelText('API token')).toBe(inputB)
    expect(inputB).toHaveValue(draftForB)
    expect(screen.getByRole('button', { name: /^Fly B/ })).toHaveAttribute('aria-expanded', 'true')
    const saveToken = screen.getByRole('button', { name: 'Save token' })
    if (draftForB) {
      fireEvent.click(saveToken)
      await waitFor(() => expect(vault.get('TOKEN_B')).toBe(draftForB))
    } else {
      expect(saveToken).toBeDisabled()
      expect(vault.get('TOKEN_B')).toBe('stored-b')
    }
    expect(vault.get('TOKEN_A')).toBe('stored-a')
    fireEvent.click(save())
    await screen.findByText('Configuration saved. No compute was started.')
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
    const field = screen.getByLabelText('API token')
    fireEvent.change(field, { target: { value: 'removed-token' } })
    fireEvent.click(screen.getByRole('button', { name: 'Remove profile' }))
    addVisible()
    expect(screen.getByLabelText('Profile ID')).toHaveValue('sprites-1')
    expect(screen.getByLabelText('API token')).not.toBe(field)
    expect(screen.getByLabelText('API token')).toHaveValue('')
    expect(screen.getByRole('button', { name: 'Save token' })).toBeDisabled()
    expect(writes).toEqual([])
  })
})
