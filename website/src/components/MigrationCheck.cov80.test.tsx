import { screen, waitFor } from '@testing-library/react'
import { renderWithProviders } from '../test/helpers'
import MigrationCheck, { registerNonAppPrefix } from './MigrationCheck'
import { api } from '../api/client'

vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return { ...mod, api: { ...mod.api, listApps: vi.fn() } }
})
vi.mock('./MigrationBanner', () => ({
  default: ({ appName, migratedTo }: { appName: string; migratedTo: string }) => (
    <div data-testid="banner">{appName}|{migratedTo}</div>
  ),
}))

const listApps = vi.mocked(api.listApps)

function app(over: Record<string, unknown> = {}) {
  return {
    name: 'zzq',
    displayName: 'Zzq Display',
    enabled: true,
    origin: 'builtin',
    migratedTo: 'registry:zzq-pkg',
    manifest: { ui: { pages: [{ route: '/zzq', label: 'Zzq' }] } },
    ...over,
  }
}

describe('MigrationCheck', () => {
  beforeEach(() => {
    listApps.mockReset()
    listApps.mockResolvedValue([app()] as never)
  })

  it('renders the banner on a route owned by a migrated builtin app', async () => {
    renderWithProviders(<MigrationCheck />, { route: '/zzq' })
    expect(await screen.findByTestId('banner')).toHaveTextContent('Zzq Display|registry:zzq-pkg')
  })

  it('matches nested routes under the app page', async () => {
    renderWithProviders(<MigrationCheck />, { route: '/zzq/sub' })
    expect(await screen.findByTestId('banner')).toBeInTheDocument()
  })

  it('never queries on a known non-app prefix', async () => {
    renderWithProviders(<MigrationCheck />, { route: '/settings' })
    await waitFor(() => expect(listApps).not.toHaveBeenCalled())
    expect(screen.queryByTestId('banner')).not.toBeInTheDocument()
  })

  it('never queries on a nested non-app route', async () => {
    renderWithProviders(<MigrationCheck />, { route: '/chat/zzq' })
    await waitFor(() => expect(listApps).not.toHaveBeenCalled())
  })

  it('renders nothing when no app claims the route', async () => {
    renderWithProviders(<MigrationCheck />, { route: '/zzq-unclaimed' })
    await waitFor(() => expect(listApps).toHaveBeenCalled())
    expect(screen.queryByTestId('banner')).not.toBeInTheDocument()
  })

  it('ignores disabled, non-builtin and non-migrated apps', async () => {
    listApps.mockResolvedValue([
      app({ enabled: false }),
      app({ origin: 'registry' }),
      app({ migratedTo: undefined }),
    ] as never)
    renderWithProviders(<MigrationCheck />, { route: '/zzq' })
    await waitFor(() => expect(listApps).toHaveBeenCalled())
    expect(screen.queryByTestId('banner')).not.toBeInTheDocument()
  })

  it('falls back to the raw name when displayName is empty', async () => {
    listApps.mockResolvedValue([app({ displayName: '' })] as never)
    renderWithProviders(<MigrationCheck />, { route: '/zzq' })
    expect(await screen.findByTestId('banner')).toHaveTextContent('zzq|registry:zzq-pkg')
  })

  it('registerNonAppPrefix suppresses the probe for an edition route, idempotently', async () => {
    registerNonAppPrefix('/zzq-edition')
    registerNonAppPrefix('/zzq-edition')
    renderWithProviders(<MigrationCheck />, { route: '/zzq-edition/panel' })
    await waitFor(() => expect(listApps).not.toHaveBeenCalled())
  })
})
