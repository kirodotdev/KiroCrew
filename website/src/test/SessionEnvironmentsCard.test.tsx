import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
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
  archived_retention_days?: number
  kiro_cloud_profiles?: { id: string; name: string; repositories: string[] }[]
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
afterEach(() => { cleanup(); qc?.clear(); localStorage.clear() })

const url = () => screen.getByLabelText('Coder URL')
const save = () => screen.getByRole('button', { name: 'Save configuration' })
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
    // One provider, so there is no provider choice to make.
    expect(screen.queryByRole('radio')).not.toBeInTheDocument()
    expect(screen.getByText(/A session runs on a profile once you choose it in the chat composer/)).toBeVisible()
    expect(writes).toEqual([])
  })

  it('collapses saved profiles, opens new ones, and keeps edits across collapse', async () => {
    mount()
    await screen.findByLabelText('Coder URL')
    const header = screen.getByRole('button', { name: /^Development/ })
    expect(header).toHaveAttribute('aria-expanded', 'false')
    expect(header).toHaveTextContent('python-dev')
    expect(screen.queryByLabelText('Coder template')).not.toBeInTheDocument()
    fireEvent.click(header)
    expect(header).toHaveAttribute('aria-expanded', 'true')
    fireEvent.change(screen.getByLabelText('Coder template'), { target: { value: 'node-dev' } })
    fireEvent.click(header)
    await waitFor(() => expect(screen.queryByLabelText('Coder template')).not.toBeInTheDocument())
    expect(header).toHaveTextContent('node-dev')
    fireEvent.click(header)
    expect(screen.getByLabelText('Coder template')).toHaveValue('node-dev')
    fireEvent.click(screen.getByRole('button', { name: 'Add profile' }))
    expect(screen.getByRole('button', { name: /^profile-1/ })).toHaveAttribute('aria-expanded', 'true')
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

describe('Kiro Cloud Sessions profiles', () => {
  it('adds a cloud profile with its repositories, and offers it as the default', async () => {
    stored = { session_environments: { coder_url: '', profiles: [], kiro_cloud_profiles: [], default_profile: '' } }
    mount()
    const add = await screen.findAllByRole('button', { name: 'Add profile' })
    // The Kiro Cloud list is the second one; it needs no Coder URL.
    fireEvent.click(add[1])
    const field = screen.getByRole('group', { name: 'kiro-cloud-1' })
    fireEvent.change(within(field).getByLabelText('Profile name'), { target: { value: 'Cloud' } })
    // A trailing comma while typing is kept, then dropped on save.
    fireEvent.change(within(field).getByLabelText('Repositories'), { target: { value: 'acme/web, ' } })
    expect(within(field).getByLabelText('Repositories')).toHaveValue('acme/web, ')
    fireEvent.click(screen.getByRole('switch', { name: 'Start new chats in a session environment' }))
    fireEvent.click(save())
    expect(await screen.findByText('Configuration saved. No compute was started.')).toBeVisible()
    expect(writes).toEqual([{ path: 'session_environments', value: {
      coder_url: '', profiles: [], default_profile: 'kiro-cloud-1',
      kiro_cloud_profiles: [{ id: 'kiro-cloud-1', name: 'Cloud', repositories: ['acme/web'] }],
    } }])
  })

  it('says Verify readiness does not cover Kiro Cloud profiles', async () => {
    stored = { session_environments: { coder_url: '', profiles: [], kiro_cloud_profiles: [], default_profile: '' } }
    mount()
    // The readiness check probes Coder and git hosts only, so the cloud list says so.
    expect(await screen.findByText(/Verify readiness doesn't check these profiles/)).toBeVisible()
    expect(screen.getByText(/reports why instead of running on the gateway/)).toBeVisible()
  })

  it('hides the Kiro Cloud list for a gateway that does not report it', async () => {
    mount()
    await screen.findByLabelText('Coder URL')
    expect(screen.queryByText('Kiro Cloud Sessions')).not.toBeInTheDocument()
    expect(screen.queryByText(/Verify readiness doesn't check these profiles/)).not.toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: 'Add profile' })).toHaveLength(1)
  })
})
