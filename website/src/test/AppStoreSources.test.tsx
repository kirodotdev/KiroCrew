import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import { Route, Routes } from 'react-router-dom'
import { renderWithProviders } from './helpers'
import DiscoverPage from '../pages/apps/DiscoverPage'
import AppDetailPage from '../pages/AppDetailPage'
import AppSource from '../components/appstore/AppSource'
import { sourceKey } from '../components/appstore/types'

const { listApps, listRegistry, listRegistries, getApp } = vi.hoisted(() => ({
  listApps: vi.fn(), listRegistry: vi.fn(), listRegistries: vi.fn(), getApp: vi.fn(),
}))
vi.mock('../api/client', () => ({
  api: {
    listApps, listRegistry, listRegistries, getApp,
    system: vi.fn().mockResolvedValue({ hostname: '' }),
  },
}))

const catalog = [
  { name: 'alpha', displayName: 'Alpha Tool', description: 'Build code', tags: ['git'], _registry: 'team-feed', provenance: 'external' },
  { name: 'beta', displayName: 'Beta Notes', description: 'Write notes', tags: ['writing'], _registry: 'team-feed', provenance: 'external' },
  { name: 'gamma', displayName: 'Gamma Tool', description: 'Build code', tags: ['git'], _registry: 'all', provenance: 'external' },
  { name: 'delta', displayName: 'Delta Tool', description: 'Build code', tags: ['git'], provenance: 'official' },
  { name: 'epsilon', displayName: 'Epsilon Builtin', description: 'Builtin', tags: [], origin: 'builtin', provenance: 'builtin' },
].map(a => ({ ...a, version: '1.0.0', author: 'Example author', installed: false }))
const sources = {
  pinned: [{ name: 'team-feed', label: 'Team catalog', repo: 'https://example.com/team.git', branch: 'main', review: 'community' }],
  registries: [
    { name: 'all', label: 'Other catalog', repo: 'https://example.com/other.git', branch: 'main' },
    { name: 'empty', label: 'Empty catalog', repo: 'https://example.com/empty.git', branch: 'main' },
  ],
}

beforeEach(() => {
  listApps.mockReset().mockResolvedValue([])
  listRegistry.mockReset().mockResolvedValue({ apps: catalog })
  listRegistries.mockReset().mockResolvedValue(sources)
  getApp.mockReset().mockRejectedValue(Object.assign(new Error('Not installed'), { status: 404 }))
  sessionStorage.clear()
})

function mount(route = '/apps') {
  return renderWithProviders(
    <Routes>
      <Route path="/apps" element={<DiscoverPage />} />
      <Route path="/apps/detail/:name" element={<AppDetailPage />} />
    </Routes>, { route },
  )
}

const sourceButton = (label: string) => screen.findByRole('button', { name: new RegExp(label) })
const appRows = () => screen.queryAllByRole('button', { name: /^View details for/ })

describe('App Store source filtering', () => {
  it('filters by registry id, shows readable source names, and resets independently', async () => {
    mount()
    fireEvent.click(await sourceButton('Team catalog'))
    expect(await screen.findByRole('status')).toHaveTextContent('2 apps')
    expect(appRows()).toHaveLength(2)
    expect(screen.queryByRole('button', { name: 'View details for Gamma Tool' })).toBeNull()
    const alpha = screen.getByRole('button', { name: 'View details for Alpha Tool' })
    expect(within(alpha).getByText('Team catalog')).toBeVisible()
    expect(await sourceButton('Team catalog')).toHaveAttribute('aria-pressed', 'true')

    fireEvent.click(await sourceButton('Other catalog'))
    expect(appRows()).toHaveLength(1)
    expect(screen.getByRole('status')).toHaveTextContent('1 app')
    expect(screen.getByRole('button', { name: 'View details for Gamma Tool' })).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'All sources' }))
    expect(screen.getByRole('status')).toHaveTextContent('5 apps')
  })

  it('composes source with search and category and keeps them when source resets', async () => {
    mount()
    fireEvent.click(await sourceButton('Team catalog'))
    fireEvent.click(screen.getByRole('button', { name: /Developer Tools/ }))
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Build' } })
    expect(appRows()).toHaveLength(1)
    expect(screen.getByRole('status')).toHaveTextContent('1 app')
    fireEvent.click(screen.getByRole('button', { name: 'All sources' }))
    expect(appRows()).toHaveLength(3)
    expect(screen.getByRole('status')).toHaveTextContent('3 apps')
  })

  it('supports keyboard selection, empty sources, builtin and core buckets', async () => {
    mount()
    const empty = await sourceButton('Empty catalog')
    fireEvent.keyDown(empty, { key: 'Enter' })
    expect(empty).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByText('No matching apps')).toBeVisible()
    expect(screen.getByText('Try a different search, category, or source.')).toBeVisible()
    expect(screen.getByRole('status')).toHaveTextContent('0 apps')
    fireEvent.keyDown(await sourceButton('Built-in'), { key: ' ' })
    expect(appRows()).toHaveLength(1)
    expect(screen.getByRole('button', { name: 'View details for Epsilon Builtin' })).toBeVisible()
    fireEvent.click(await sourceButton('Kiro Crew registry'))
    expect(appRows()).toHaveLength(1)
    expect(screen.getByRole('button', { name: 'View details for Delta Tool' })).toBeVisible()
  })

  it('preserves source attribution from browse to an installed app detail', async () => {
    getApp.mockResolvedValue({
      name: 'alpha', displayName: 'Alpha Tool', version: '1.0.0', origin: 'registry',
      source: 'registry:alpha', enabled: false, manifest: { description: 'Build code' },
    })
    mount()
    fireEvent.click(await sourceButton('Team catalog'))
    fireEvent.click(screen.getByRole('button', { name: 'View details for Alpha Tool' }))
    expect(await screen.findByText('Team catalog')).toBeVisible()
    expect(screen.getByText('Source')).toBeVisible()
    expect(screen.queryByText('Kiro Crew registry')).toBeNull()
  })

  it.each([
    ['alpha', 'Team catalog'], ['delta', 'Kiro Crew registry'], ['epsilon', 'Built-in · kirocrew'],
  ])('shows source on the uninstalled %s detail page', async (name, label) => {
    mount(`/apps/detail/${name}`)
    expect(await screen.findByText(label)).toBeVisible()
  })

  it('does not call an unlisted local install an official catalog app', async () => {
    getApp.mockResolvedValue({
      name: 'local-tool', displayName: 'Local Tool', version: '1.0.0', origin: 'local',
      enabled: false, manifest: {},
    })
    mount('/apps/detail/local-tool')
    expect(await screen.findByText('Local install')).toBeVisible()
    expect(screen.queryByText('Kiro Crew registry')).toBeNull()
  })
})

describe('source identity and display fallbacks', () => {
  it('keeps filter buckets independent from labels and preserves exact case', () => {
    expect(sourceKey({ origin: 'builtin', _registry: 'stale' })).toBe('registry:stale')
    expect(sourceKey({ _registry: 'Team' })).toBe('registry:Team')
    expect(sourceKey({})).toBe('__core__')
    expect(sourceKey({ _registry: 'all' })).toBe('registry:all')
  })

  it('keeps a stale registry id readable without a registry metadata response', () => {
    renderWithProviders(<AppSource app={{ _registry: 'retired-feed' }} />)
    expect(screen.getByText('retired-feed')).toBeVisible()
  })

  it('does not borrow a label from a case-variant shadowed registry', () => {
    renderWithProviders(<AppSource app={{ _registry: 'TEAM' }} sources={[
      { name: 'team', label: 'Pinned catalog' }, { name: 'TEAM', label: 'Shadow catalog' },
    ]} />)
    expect(screen.getByText('TEAM')).toBeVisible()
    expect(screen.queryByText('Shadow catalog')).toBeNull()
  })
})

it.each(['alpha', 'delta'])('keeps local install provenance for a same-name catalog app: %s', async name => {
  getApp.mockResolvedValue({
    name, displayName: 'Local Fork', version: '1.0.0', origin: 'local',
    source: '/example/local-fork', enabled: false, manifest: {},
  })
  mount(`/apps/detail/${name}`)
  expect(await screen.findByText('Local install')).toBeVisible()
  expect(screen.queryByText('Kiro Crew registry')).toBeNull()
  expect(screen.queryByText('Team catalog')).toBeNull()
})

it('clears a selected source when that registry and its cached apps disappear', async () => {
  const { queryClient } = mount()
  fireEvent.click(await sourceButton('Team catalog'))
  expect(screen.getByRole('status')).toHaveTextContent('2 apps')
  listRegistry.mockResolvedValue({ apps: catalog.filter(app => app._registry !== 'team-feed') })
  listRegistries.mockResolvedValue({ ...sources, pinned: [] })
  await act(async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ['registry'] }),
      queryClient.invalidateQueries({ queryKey: ['registries'] }),
    ])
  })
  await waitFor(() => expect(screen.getByRole('button', { name: 'All sources' })).toHaveAttribute('aria-pressed', 'true'))
  expect(screen.getByRole('status')).toHaveTextContent('3 apps')
})

it('scopes category counts to source and search while keeping empty categories selectable', async () => {
  mount()
  fireEvent.click(await sourceButton('Team catalog'))
  expect(screen.getByRole('button', { name: 'All apps 2' })).toBeVisible()
  expect(screen.getByRole('button', { name: 'Developer Tools 1' })).toBeVisible()
  fireEvent.click(screen.getByRole('button', { name: 'Research & Writing 1' }))
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Build' } })
  expect(screen.getByRole('button', { name: 'All apps 1' })).toBeVisible()
  expect(screen.getByRole('button', { name: 'Research & Writing 0' })).toHaveAttribute('aria-pressed', 'true')
  fireEvent.click(await sourceButton('Empty catalog'))
  expect(screen.getByRole('button', { name: 'All apps 0' })).toBeVisible()
  expect(screen.getByRole('status')).toHaveTextContent('0 apps')
})

it('shows the community warning on a direct detail visit', async () => {
  mount('/apps/detail/alpha')
  expect(await screen.findByText('Team catalog')).toBeVisible()
  expect(screen.getByText('Not vetted')).toBeVisible()
  expect(screen.queryByText('Team reviewed')).toBeNull()
})

it('reports a failed source metadata read while retaining the source id', async () => {
  listRegistries.mockRejectedValue(new Error('Source metadata unavailable'))
  mount('/apps/detail/alpha')
  expect(await screen.findByText('Source metadata unavailable')).toBeVisible()
  expect(screen.getByText('team-feed')).toBeVisible()
  expect(screen.queryByText('Team reviewed')).toBeNull()
})

it('does not assign a review tier to a local install from a same-name listing', async () => {
  getApp.mockResolvedValue({
    name: 'alpha', version: '1.0.0', displayName: 'Local fork', origin: 'local',
    enabled: false, manifest: {},
  })
  mount('/apps/detail/alpha')
  expect(await screen.findByText('Local install')).toBeVisible()
  expect(screen.queryByText('Not vetted')).toBeNull()
  expect(listRegistries).not.toHaveBeenCalled()
})

it.each(['__builtin__', '__core__', 'registry:team-feed'])('isolates an external source named %s from host buckets', async id => {
  listRegistry.mockResolvedValue({ apps: [
    ...catalog,
    { name: 'reserved-name-app', displayName: 'External App', description: '', version: '1.0.0',
      author: 'Example author', _registry: id, provenance: 'external', origin: 'builtin' },
  ] })
  listRegistries.mockResolvedValue({ ...sources, registries: [
    ...sources.registries, { name: id, label: 'Reserved-name registry', repo: 'https://example.com/registry.git', branch: 'main' },
  ] })
  mount()
  const external = await sourceButton('Reserved-name registry')
  expect(external).toHaveTextContent('1 app')
  expect(within(external).queryByLabelText('First-party')).toBeNull()
  fireEvent.click(external)
  expect(appRows()).toHaveLength(1)
  const appRow = screen.getByRole('button', { name: 'View details for External App' })
  expect(appRow).toBeVisible()
  expect(within(appRow).getByText('Reserved-name registry')).toBeVisible()
  expect(within(appRow).queryByText('Built-in · kirocrew')).toBeNull()
  fireEvent.click(await sourceButton('Built-in · kirocrew'))
  expect(appRows()).toHaveLength(1)
  expect(screen.queryByRole('button', { name: 'View details for External App' })).toBeNull()
  fireEvent.click(await sourceButton('Kiro Crew registry'))
  expect(appRows()).toHaveLength(1)
  expect(screen.queryByRole('button', { name: 'View details for External App' })).toBeNull()
})
