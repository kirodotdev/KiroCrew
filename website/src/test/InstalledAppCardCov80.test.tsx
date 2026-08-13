/**
 * InstalledAppCard — the Library row's badge matrix, action set and details
 * drawer. Which affordances appear is driven entirely by the app's origin /
 * resources / lifecycle triple plus `updateAvailable`, and getting that wrong
 * is how a locked app gets an Uninstall button or a gateway app loses its Sync.
 * The Open button also has a second outcome worth pinning: a headless gateway
 * answers with a command to run locally, which the card surfaces inline.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import type { InstalledApp } from '../components/appstore/types'
import { i18nT } from '../i18n/t'

const mocks = vi.hoisted(() => ({ openApp: vi.fn() }))
vi.mock('../api/client', () => ({ api: mocks }))
vi.mock('../hooks/useTheme', () => ({ useTheme: () => ({ theme: 'dark' }) }))
vi.mock('../components/AppIcon', () => ({
  default: () => <div data-testid="zzq-app-icon" />,
}))

import InstalledAppCard from '../components/appstore/InstalledAppCard'

const T = (k: string, vars?: Record<string, unknown>) => i18nT(`components.appstore.installedAppCard.${k}`, vars)

function app(over: Partial<InstalledApp> = {}, manifest: Partial<InstalledApp['manifest']> = {}): InstalledApp {
  return {
    name: 'zzq-app',
    version: '1.2.3',
    displayName: 'Zzq App',
    enabled: true,
    installedAt: '2026-08-02T00:00:00Z',
    origin: 'registry',
    lifecycle: 'gateway',
    manifest: {
      name: 'zzq-app',
      version: '1.2.3',
      displayName: 'Zzq App',
      description: 'zzq description',
      author: 'zzq-author',
      ...manifest,
    },
    ...over,
  } as InstalledApp
}

function renderCard(a: InstalledApp, over: Partial<{ actionLoading: string | null }> = {}) {
  const onAction = vi.fn()
  const onOpen = vi.fn()
  const onDetail = vi.fn()
  const r = render(
    <InstalledAppCard
      app={a}
      actionLoading={over.actionLoading ?? null}
      onAction={onAction}
      onOpen={onOpen}
      onDetail={onDetail}
    />,
  )
  return { ...r, onAction, onOpen, onDetail }
}

beforeEach(() => { mocks.openApp.mockResolvedValue(null) })
afterEach(() => vi.clearAllMocks())

describe('InstalledAppCard badges', () => {
  it('shows Registry plus the enabled state for a registry app', () => {
    renderCard(app())
    expect(screen.getByText(T('registry'))).toBeInTheDocument()
    expect(screen.getByText(T('enabled'))).toBeInTheDocument()
    expect(screen.getByText(`${T('v')}1.2.3`)).toBeInTheDocument()
  })

  it('shows Built-in plus the disabled state for a disabled builtin', () => {
    renderCard(app({ origin: 'builtin', enabled: false }))
    expect(screen.getByText(T('built_in'))).toBeInTheDocument()
    expect(screen.getByText(T('disabled'))).toBeInTheDocument()
    expect(screen.queryByText(T('registry'))).not.toBeInTheDocument()
  })

  it('shows Self-managed instead of an enabled/disabled state', () => {
    renderCard(app({ resources: 'app', origin: 'external' }))
    expect(screen.getByText(T('self_managed'))).toBeInTheDocument()
    expect(screen.queryByText(T('enabled'))).not.toBeInTheDocument()
    expect(screen.queryByText(T('external'))).not.toBeInTheDocument()
  })

  it('shows External for a gateway-managed external app, and Local for a local one', () => {
    const { unmount } = renderCard(app({ origin: 'external' }))
    expect(screen.getByText(T('external'))).toBeInTheDocument()
    unmount()

    renderCard(app({ origin: 'local' }))
    expect(screen.getByText(T('local'))).toBeInTheDocument()
  })

  it('flags a migrating app and an available update', () => {
    renderCard(app({ migratedTo: 'zzq-successor', updateAvailable: true, _newVersion: '2.0.0' } as Partial<InstalledApp>))
    expect(screen.getByText(T('migrating'))).toBeInTheDocument()
    expect(screen.getByText(/\(v2\.0\.0 available\)/)).toBeInTheDocument()
  })

  it('summarises the manifest resource counts', () => {
    renderCard(app({}, {
      agents: ['a1', 'a2'],
      skills: ['s1'],
      crons: [{ name: 'c1' }, { name: 'c2' }, { name: 'c3' }],
      ui: { pages: [{ route: '/zzq', label: 'Zzq', icon: 'Box' }] },
    }))
    expect(screen.getByText(T('agent', { count: 2 }))).toBeInTheDocument()
    expect(screen.getByText(T('skill', { count: 1 }))).toBeInTheDocument()
    expect(screen.getByText(T('cron', { count: 3 }))).toBeInTheDocument()
    expect(screen.getByText(T('page', { count: 1 }))).toBeInTheDocument()
    expect(screen.getByText('zzq-author')).toBeInTheDocument()
  })
})

describe('InstalledAppCard actions', () => {
  it('offers Disable for an enabled app and Enable for a disabled one', () => {
    const enabled = renderCard(app())
    fireEvent.click(screen.getByRole('button', { name: new RegExp(T('disable')) }))
    expect(enabled.onAction).toHaveBeenCalledWith('zzq-app', 'disable')
    enabled.unmount()

    const disabled = renderCard(app({ enabled: false }))
    fireEvent.click(screen.getByRole('button', { name: new RegExp(T('enable')) }))
    expect(disabled.onAction).toHaveBeenCalledWith('zzq-app', 'enable')
  })

  it('disables the in-flight action button', () => {
    renderCard(app(), { actionLoading: 'zzq-app:disable' })
    expect(screen.getByRole('button', { name: new RegExp(T('disable')) })).toBeDisabled()
  })

  it('offers Update when a new version exists, and Sync otherwise', () => {
    const upd = renderCard(app({ updateAvailable: true, _newVersion: '2.0.0' } as Partial<InstalledApp>))
    expect(screen.queryByRole('button', { name: new RegExp(T('sync')) })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: new RegExp(T('update')) }))
    expect(upd.onAction).toHaveBeenCalledWith('zzq-app', 'update')
    upd.unmount()

    const sync = renderCard(app())
    fireEvent.click(screen.getByRole('button', { name: new RegExp(T('sync')) }))
    expect(sync.onAction).toHaveBeenCalledWith('zzq-app', 'update')
  })

  it('offers Uninstall unless the lifecycle is locked', () => {
    const open = renderCard(app())
    fireEvent.click(screen.getByRole('button', { name: new RegExp(T('uninstall')) }))
    expect(open.onAction).toHaveBeenCalledWith('zzq-app', 'uninstall')
    open.unmount()

    renderCard(app({ lifecycle: 'locked' }))
    expect(screen.queryByRole('button', { name: new RegExp(T('uninstall')) })).not.toBeInTheDocument()
    // A locked app is also not a gateway app, so it has no Sync either.
    expect(screen.queryByRole('button', { name: new RegExp(T('sync')) })).not.toBeInTheDocument()
  })

  it('opens a UI app through the host callback', () => {
    const { onOpen } = renderCard(app({}, { ui: { pages: [{ route: '/zzq', label: 'Zzq', icon: 'Box' }] } }))
    fireEvent.click(screen.getByRole('button', { name: new RegExp(T('open')) }))
    expect(onOpen).toHaveBeenCalledTimes(1)
    expect(mocks.openApp).not.toHaveBeenCalled()
  })

  it('routes the title through onDetail', () => {
    const { onDetail } = renderCard(app())
    fireEvent.click(screen.getByText('Zzq App'))
    expect(onDetail).toHaveBeenCalledTimes(1)
  })
})

describe('InstalledAppCard openCommand app', () => {
  it('goes through api.openApp and stays quiet on a local open', async () => {
    renderCard(app({}, { openCommand: 'zzq --open' }))
    fireEvent.click(screen.getByRole('button', { name: new RegExp(T('open')) }))
    await waitFor(() => expect(mocks.openApp).toHaveBeenCalledWith('zzq-app'))
    expect(screen.queryByText(T('remote_environment_detected'))).not.toBeInTheDocument()
  })

  it('surfaces the remote command banner and lets it be dismissed', async () => {
    mocks.openApp.mockResolvedValue({ remote: true, command: 'zzq open --host local' })
    renderCard(app({}, { openCommand: 'zzq --open' }))
    fireEvent.click(screen.getByRole('button', { name: new RegExp(T('open')) }))

    await screen.findByText(T('remote_environment_detected'))
    expect(screen.getByText('zzq open --host local')).toBeInTheDocument()

    fireEvent.click(screen.getByLabelText(T('dismiss')))
    expect(screen.queryByText(T('remote_environment_detected'))).not.toBeInTheDocument()
  })

  it('falls back to the message, then to the headless explainer', async () => {
    mocks.openApp.mockResolvedValue({ remote: true, message: 'zzq run it over there' })
    const first = renderCard(app({}, { openCommand: 'zzq --open' }))
    fireEvent.click(screen.getByRole('button', { name: new RegExp(T('open')) }))
    expect(await screen.findByText('zzq run it over there')).toBeInTheDocument()
    first.unmount()

    mocks.openApp.mockResolvedValue({ remote: true })
    renderCard(app({}, { openCommand: 'zzq --open' }))
    fireEvent.click(screen.getByRole('button', { name: new RegExp(T('open')) }))
    expect(await screen.findByText(T('app_cannot_be_opened_kirocrew_is_running_in_a_he'))).toBeInTheDocument()
  })
})

describe('InstalledAppCard details drawer', () => {
  it('is closed until the chevron is pressed and then lists the manifest detail', () => {
    renderCard(app({ source: '/zzq/source/dir', resources: 'gateway' }, {
      tags: ['zzq-tag-a', 'zzq-tag-b'],
      permissions: { mcpTools: ['zzq_tool_1', 'zzq_tool_2'] },
      sops: ['sop-1', 'sop-2'],
      ui: { pages: [{ route: '/zzq', label: 'Zzq Page', icon: 'Box' }] },
      minKiroCrewVersion: '9.9.9',
    }))
    expect(screen.queryByText(T('mcp_tools'))).not.toBeInTheDocument()

    fireEvent.click(screen.getByLabelText(T('expand_details')))

    expect(screen.getByText('zzq-tag-a')).toBeInTheDocument()
    expect(screen.getByText('zzq_tool_1, zzq_tool_2')).toBeInTheDocument()
    expect(screen.getByText('Zzq Page (/zzq)')).toBeInTheDocument()
    expect(screen.getByText(T('standard_operating_procedure', { count: 2 }))).toBeInTheDocument()
    expect(screen.getByText(/9\.9\.9/)).toBeInTheDocument()
    expect(screen.getByText(new RegExp('/zzq/source/dir'))).toBeInTheDocument()

    // And it closes again.
    fireEvent.click(screen.getByLabelText(T('collapse_details')))
    expect(screen.queryByText(T('mcp_tools'))).not.toBeInTheDocument()
  })

  it('explains a self-managed app in the drawer', () => {
    renderCard(app({ resources: 'app' }))
    fireEvent.click(screen.getByLabelText(T('expand_details')))
    expect(screen.getByText(T('management_app_handles_its_own_agent_skill_mcp_r'))).toBeInTheDocument()
  })

  it('explains a builtin app in the drawer and omits its source line', () => {
    renderCard(app({ origin: 'builtin', source: '/zzq/builtin/dir' }))
    fireEvent.click(screen.getByLabelText(T('expand_details')))
    expect(screen.getByText(T('built_in_this_feature_is_part_of_the_kirocrew_da'))).toBeInTheDocument()
    expect(screen.queryByText(new RegExp('/zzq/builtin/dir'))).not.toBeInTheDocument()
  })
})
