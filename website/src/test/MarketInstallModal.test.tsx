/**
 * Marketplace install consent: nothing is installed until the user has seen the
 * whole plan, and Install sends back exactly the digest of the plan that was shown.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import planFixture from '../../../test/fixtures/market_install_plan.json'

const capabilityMarketStatus = vi.fn()
const capabilityMarketPlan = vi.fn()
const capabilityMarketApply = vi.fn()
const capabilityMarketConfirm = vi.fn()

vi.mock('../api/client', () => ({
  api: {
    capabilityMarketStatus: (...a: unknown[]) => capabilityMarketStatus(...a),
    capabilityMarketPlan: (...a: unknown[]) => capabilityMarketPlan(...a),
    capabilityMarketApply: (...a: unknown[]) => capabilityMarketApply(...a),
    capabilityMarketConfirm: (...a: unknown[]) => capabilityMarketConfirm(...a),
  },
}))

// Render catalog KEYS (with interpolated values appended), not English.
vi.mock('../i18n/t', () => ({
  i18nT: (key: string, vars?: Record<string, unknown>) =>
    vars && Object.keys(vars).length ? `${key} ${Object.values(vars).join(' ')}` : key,
}))

import { ApiError } from '../api/apiError'
/** What /market/confirm answers when it refused before anything was installed. */
const refusedBeforeInstall = () =>
  new ApiError(409, 'the plan changed', JSON.stringify({ error: 'the plan changed', code: 'market_refused' }))
import MarketInstallModal, { MarketInstallEntry, marketInstalledMessage, marketInstalledNotice } from '../components/appstore/MarketInstallModal'

const K = 'components.appstore.marketInstall'

let lastClient: QueryClient | null = null

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  lastClient = qc
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>)
}

async function review(ref: string, ready: () => Promise<unknown> = () => screen.findByTestId('market-plan')) {
  fireEvent.change(screen.getByRole('textbox'), { target: { value: ref } })
  fireEvent.click(screen.getByRole('button', { name: `${K}.review` }))
  await ready()
}

beforeEach(() => {
  capabilityMarketStatus.mockReset()
  capabilityMarketPlan.mockReset()
  capabilityMarketApply.mockReset()
  capabilityMarketConfirm.mockReset()
})

describe('MarketInstallModal', () => {
  it('shows every item, its sha256 and publisher, and the union of permissions before Install', async () => {
    capabilityMarketPlan.mockResolvedValue(planFixture)
    wrap(<MarketInstallModal open onClose={() => {}} onInstalled={() => {}} />)
    expect(screen.queryByRole('button', { name: `${K}.install` })).toBeNull()
    await review('team-a/pdf-tools')

    expect(capabilityMarketPlan).toHaveBeenCalledWith('team-a/pdf-tools')
    expect(screen.getAllByTestId('market-plan-item')).toHaveLength(2)
    expect(screen.getByText(`${K}.kind_skill`)).toBeTruthy()
    expect(screen.getByText(`${K}.kind_mcp`)).toBeTruthy()
    // An MCP server the user did not type says in plain words what it is.
    expect(screen.getByText(`${K}.kind_mcp_meaning`)).toBeTruthy()
    // Each non-empty permission row says what it means in plain words.
    expect(screen.getByText(`${K}.commands_meaning`)).toBeTruthy()
    expect(screen.getByText(`${K}.secrets_meaning`)).toBeTruthy()
    for (const item of planFixture.items) {
      expect(screen.getByText(`${item.ref}@${item.semver}`)).toBeTruthy()
      expect(screen.getByText(`${K}.sha256 ${item.sha256}`)).toBeTruthy()
    }
    expect(screen.getAllByText(`${K}.publisher alice (internal)`)).toHaveLength(2)
    expect(screen.getByText(`${K}.required_by team-a/pdf-tools`)).toBeTruthy()
    expect(screen.getByText('uvx docs-mcp-server==1.2.3')).toBeTruthy()
    expect(screen.getByText('docs.example')).toBeTruthy()
    expect(screen.getByText('DOCS_TOKEN')).toBeTruthy()
    expect(capabilityMarketApply).not.toHaveBeenCalled()
  })

  it('shows two identical command lines without a duplicate-key warning', async () => {
    const cmd = ['uvx', 'docs-mcp-server==1.2.3']
    capabilityMarketPlan.mockResolvedValue({
      ...planFixture,
      permissions: { ...planFixture.permissions, commands: [cmd, cmd] },
    })
    const error = vi.spyOn(console, 'error').mockImplementation(() => {})
    try {
      wrap(<MarketInstallModal open onClose={() => {}} onInstalled={() => {}} />)
      await review('team-a/pdf-tools')
      expect(screen.getAllByText('uvx docs-mcp-server==1.2.3')).toHaveLength(2)
      const keyWarnings = error.mock.calls.filter(c => String(c[0]).includes('same key'))
      expect(keyWarnings).toEqual([])
    } finally {
      error.mockRestore()
    }
  })

  it('sends the digest of the plan that was shown', async () => {
    capabilityMarketPlan.mockResolvedValue(planFixture)
    capabilityMarketApply.mockResolvedValue({ ok: true })
    const onInstalled = vi.fn()
    wrap(<MarketInstallModal open onClose={() => {}} onInstalled={onInstalled} />)
    await review('team-a/pdf-tools')
    fireEvent.click(screen.getByRole('button', { name: `${K}.install` }))
    await waitFor(() => expect(onInstalled).toHaveBeenCalled())
    expect(capabilityMarketApply).toHaveBeenCalledWith('team-a/pdf-tools', planFixture.planDigest)
    // The page learns what kinds landed (to link to them) and that the agent spec followed.
    expect(onInstalled).toHaveBeenCalledWith({ ref: 'team-a/pdf-tools', kinds: ['skill', 'mcp'], specRewritten: true })
  })

  it('reports an install whose agent spec was not rewritten', async () => {
    capabilityMarketPlan.mockResolvedValue(planFixture)
    capabilityMarketApply.mockResolvedValue({ ok: true, agent_spec_rewritten: false })
    const onInstalled = vi.fn()
    wrap(<MarketInstallModal open onClose={() => {}} onInstalled={onInstalled} />)
    await review('team-a/pdf-tools')
    fireEvent.click(screen.getByRole('button', { name: `${K}.install` }))
    await waitFor(() => expect(onInstalled).toHaveBeenCalledWith(expect.objectContaining({ specRewritten: false })))
  })

  it('offers only Cancel and a next step for a plan the installer refused', async () => {
    capabilityMarketPlan.mockResolvedValue({
      ...planFixture,
      installable: false,
      refusal: { code: 'YANKED', message: 'team-a/docs-mcp 1.0.0 was withdrawn' },
    })
    wrap(<MarketInstallModal open onClose={() => {}} onInstalled={() => {}} />)
    await review('team-a/pdf-tools@1.0.0', () => screen.findByText('team-a/docs-mcp 1.0.0 was withdrawn'))
    expect(screen.queryByRole('button', { name: `${K}.install` })).toBeNull()
    // No primary action leads anywhere from a refused plan: only Cancel, plus a hint.
    expect(screen.queryByRole('button', { name: `${K}.review` })).toBeNull()
    // The hint names the listing the user typed (without its version), not the refused dependency.
    expect(screen.getByText(`${K}.refused_hint team-a/pdf-tools`)).toBeTruthy()
    // The refused plan is not offered, so its item list is not shown as if it were.
    expect(screen.queryByTestId('market-plan')).toBeNull()
  })

  it('refreshes the skill, MCP and theme catalogs after an install', async () => {
    capabilityMarketPlan.mockResolvedValue(planFixture)
    capabilityMarketApply.mockResolvedValue({ ok: true, agent_spec_rewritten: true })
    const onInstalled = vi.fn()
    wrap(<MarketInstallModal open onClose={() => {}} onInstalled={onInstalled} />)
    const invalidate = vi.spyOn(lastClient!, 'invalidateQueries')
    await review('team-a/pdf-tools')
    fireEvent.click(screen.getByRole('button', { name: `${K}.install` }))
    await waitFor(() => expect(onInstalled).toHaveBeenCalled())
    const keys = invalidate.mock.calls.map(([f]) => JSON.stringify((f as { queryKey: unknown }).queryKey))
    for (const key of [['skills'], ['mcp-servers'], ['custom-themes-catalog']]) {
      expect(keys).toContain(JSON.stringify(key))
    }
  })

  it('shows no version hint for a conflict, which names its own fix', async () => {
    capabilityMarketPlan.mockResolvedValue({ ...planFixture, conflicts: ['skills/pdf-tools was placed by another source'] })
    wrap(<MarketInstallModal open onClose={() => {}} onInstalled={() => {}} />)
    await review('team-a/pdf-tools', () => screen.findByText('skills/pdf-tools was placed by another source'))
    expect(screen.queryByText(`${K}.refused_hint`)).toBeNull()
    expect(screen.queryByRole('button', { name: `${K}.install` })).toBeNull()
  })

  it('shows a refused install and keeps the dialog open', async () => {
    capabilityMarketPlan.mockResolvedValue(planFixture)
    capabilityMarketApply.mockRejectedValue(new Error('the plan changed'))
    const onInstalled = vi.fn()
    wrap(<MarketInstallModal open onClose={() => {}} onInstalled={onInstalled} />)
    await review('team-a/pdf-tools')
    fireEvent.click(screen.getByRole('button', { name: `${K}.install` }))
    expect(await screen.findByText('the plan changed')).toBeTruthy()
    expect(onInstalled).not.toHaveBeenCalled()
  })
})

describe('MarketInstallModal, an app', () => {
  const appPlan = {
    ...planFixture,
    items: [{ ...planFixture.items[0], ref: 'team-a/demo-app', kind: 'app', target: '$HOME/.kiro/crew/apps/demo-app' }],
    planDigest: 'a'.repeat(64),
  }
  const pending = {
    needsConfirm: true,
    ref: 'team-a/demo-app',
    token: 'tok-1',
    confirm: {
      inFolder: { crons: ['daily-digest'], mcpServers: ['tickets'], agents: ['triager'] },
      app: { ref: 'team-a/demo-app', version: 3, appVersion: '1.2.0', publisher: 'alice (internal)', sha256Prefix: 'abcdef012345' },
      components: [{ kind: 'skill', name: 'triage', sha256Prefix: '0123456789ab' }],
      permissions: { notice: 'declared by the publisher, not enforced', commands: ['node'], network: ['api.example.internal'], files: [], background: ['cron: daily-digest'], mcpServers: ['tickets'], agents: [] },
      planPermissions: planFixture.permissions,
      trustLabel: 'Community, not reviewed by Kiro Space',
    },
  }

  async function prepare() {
    capabilityMarketPlan.mockResolvedValue(appPlan)
    capabilityMarketApply.mockResolvedValue(pending)
    await review('team-a/demo-app')
    // For an app the first step says what it does: step 1 of 2, and it only reviews.
    expect(screen.getByText(`${K}.app_plan_intro`)).toBeTruthy()
    expect(screen.getByText(`${K}.permissions_heading_app`)).toBeTruthy()
    expect(screen.queryByRole('button', { name: `${K}.install` })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: `${K}.review_app` }))
    await screen.findByTestId('market-app-confirm')
  }

  it('shows what is in the folder and the not-enforced notice before the yes', async () => {
    const onInstalled = vi.fn()
    wrap(<MarketInstallModal open onClose={() => {}} onInstalled={onInstalled} />)
    await prepare()
    for (const v of ['daily-digest', 'tickets', 'triager']) {
      expect(screen.getAllByText(v).length).toBeGreaterThan(0)
    }
    expect(screen.getByText(`${K}.not_enforced`)).toBeTruthy()
    // The settings the app reads are shown at the final yes too, and the box speaks of the app.
    expect(screen.getByText(`${K}.secrets_app`)).toBeTruthy()
    expect(screen.getByText('DOCS_TOKEN')).toBeTruthy()
    expect(screen.getByText(`${K}.commands_app`)).toBeTruthy()
    expect(screen.getByText(`${K}.app_trust`)).toBeTruthy()
    // The full command, not only the program name.
    expect(screen.getByText('uvx docs-mcp-server==1.2.3')).toBeTruthy()
    // Preparing installed nothing: the page is not told of an install, and no yes was sent.
    expect(onInstalled).not.toHaveBeenCalled()
    expect(capabilityMarketConfirm).not.toHaveBeenCalled()
  })

  it('step 1 says it downloads and checks while busy, never that it installs, and lists what step 2 fills in', async () => {
    capabilityMarketPlan.mockResolvedValue(appPlan)
    capabilityMarketApply.mockReturnValue(new Promise(() => {}))
    wrap(<MarketInstallModal open onClose={() => {}} onInstalled={() => {}} />)
    await review('team-a/demo-app')
    expect(screen.getAllByText(`${K}.in_folder_next_step`)).toHaveLength(3)
    fireEvent.click(screen.getByRole('button', { name: `${K}.review_app` }))
    expect(await screen.findByText(`${K}.downloading_checking`)).toBeTruthy()
    expect(screen.queryByText(`${K}.installing`)).toBeNull()
  })

  it('shows a refused yes at the top of step 2 and offers Start again, which checks the listing again', async () => {
    capabilityMarketConfirm.mockRejectedValue(refusedBeforeInstall())
    wrap(<MarketInstallModal open onClose={() => {}} onInstalled={() => {}} />)
    await prepare()
    fireEvent.click(screen.getByRole('button', { name: `${K}.install_app` }))
    const box = await screen.findByTestId('market-app-confirm-error')
    expect(box.textContent).toContain('the plan changed')
    expect(screen.queryByRole('button', { name: `${K}.install_app` })).toBeNull()
    capabilityMarketPlan.mockClear()
    fireEvent.click(screen.getByRole('button', { name: `${K}.start_again` }))
    await waitFor(() => expect(capabilityMarketPlan).toHaveBeenCalledWith('team-a/demo-app'))
    expect(capabilityMarketConfirm).toHaveBeenCalledTimes(1)
  })

  it('a refused yes says nothing was installed', async () => {
    capabilityMarketConfirm.mockRejectedValue(refusedBeforeInstall())
    wrap(<MarketInstallModal open onClose={() => {}} onInstalled={() => {}} />)
    await prepare()
    fireEvent.click(screen.getByRole('button', { name: `${K}.install_app` }))
    const box = await screen.findByTestId('market-app-confirm-error')
    expect(box.textContent).toContain(`${K}.confirm_failed_intro`)
    expect(box.textContent).not.toContain(`${K}.confirm_outcome_unknown`)
  })

  for (const [what, failure] of [
    ['a confirm 500', () => new ApiError(500, 'the installer failed', JSON.stringify({ error: 'the installer failed', code: 'market_error' }))],
    ['an install that failed after the yes', () => new ApiError(500, 'secret write failed', JSON.stringify({ error: 'secret write failed', code: 'market_install_failed' }))],
    ['a dropped connection', () => new Error('Failed to fetch')],
  ] as const) {
    it(`${what} never says nothing was installed: the outcome is unknown, check Library`, async () => {
      const changed = vi.fn()
      window.addEventListener('mc:apps-changed', changed)
      try {
        capabilityMarketConfirm.mockRejectedValue(failure())
        wrap(<MarketInstallModal open onClose={() => {}} onInstalled={() => {}} />)
        await prepare()
        fireEvent.click(screen.getByRole('button', { name: `${K}.install_app` }))
        const box = await screen.findByTestId('market-app-confirm-error')
        expect(box.textContent).toContain(`${K}.confirm_outcome_unknown`)
        expect(screen.queryByText(`${K}.confirm_failed_intro`)).toBeNull()
        expect(screen.queryByText(`${K}.confirm_intro`)).toBeNull()
        // No Start again (it could lead to a second install) and no second yes.
        expect(screen.queryByRole('button', { name: `${K}.start_again` })).toBeNull()
        expect(screen.queryByRole('button', { name: `${K}.install_app` })).toBeNull()
        // The app list is read again, since the app may have landed.
        expect(changed).toHaveBeenCalledTimes(1)
      } finally {
        window.removeEventListener('mc:apps-changed', changed)
      }
    })
  }

  it('Cancel never sends the yes', async () => {
    const onClose = vi.fn()
    wrap(<MarketInstallModal open onClose={onClose} onInstalled={() => {}} />)
    await prepare()
    fireEvent.click(screen.getByRole('button', { name: `${K}.cancel` }))
    expect(onClose).toHaveBeenCalled()
    expect(capabilityMarketConfirm).not.toHaveBeenCalled()
  })

  it('tells the app list a new app landed after the yes, and not after a refused yes', async () => {
    const changed = vi.fn()
    window.addEventListener('mc:apps-changed', changed)
    try {
      capabilityMarketConfirm.mockRejectedValueOnce(refusedBeforeInstall())
      const first = wrap(<MarketInstallModal open onClose={() => {}} onInstalled={() => {}} />)
      await prepare()
      fireEvent.click(screen.getByRole('button', { name: `${K}.install_app` }))
      await screen.findByTestId('market-app-confirm-error')
      expect(changed).not.toHaveBeenCalled()
      first.unmount()

      capabilityMarketConfirm.mockResolvedValue({ ok: true, ref: 'team-a/demo-app', enabled: false })
      const onInstalled = vi.fn()
      wrap(<MarketInstallModal open onClose={() => {}} onInstalled={onInstalled} />)
      await prepare()
      fireEvent.click(screen.getByRole('button', { name: `${K}.install_app` }))
      await waitFor(() => expect(onInstalled).toHaveBeenCalled())
      expect(changed).toHaveBeenCalledTimes(1)
    } finally {
      window.removeEventListener('mc:apps-changed', changed)
    }
  })

  it('installs only on an explicit click, with the token and digest', async () => {
    capabilityMarketConfirm.mockResolvedValue({ ok: true, ref: 'team-a/demo-app', enabled: false })
    const onInstalled = vi.fn()
    wrap(<MarketInstallModal open onClose={() => {}} onInstalled={onInstalled} />)
    await prepare()
    fireEvent.click(screen.getByRole('button', { name: `${K}.install_app` }))
    await waitFor(() => expect(onInstalled).toHaveBeenCalled())
    expect(capabilityMarketConfirm).toHaveBeenCalledWith('team-a/demo-app', 'a'.repeat(64), 'tok-1')
    expect(onInstalled).toHaveBeenCalledWith({ ref: 'team-a/demo-app', kinds: ['app'], specRewritten: true })
  })
})

describe('MarketInstallEntry', () => {
  it('renders nothing when the build installs no marketplace listings', async () => {
    capabilityMarketStatus.mockResolvedValue({ available: false })
    const { container } = wrap(<MarketInstallEntry onOpen={() => {}} />)
    await waitFor(() => expect(capabilityMarketStatus).toHaveBeenCalled())
    expect(container.querySelector('button')).toBeNull()
  })

  it('offers the install when the build supports it', async () => {
    capabilityMarketStatus.mockResolvedValue({ available: true })
    const onOpen = vi.fn()
    wrap(<MarketInstallEntry onOpen={onOpen} />)
    fireEvent.click(await screen.findByRole('button', { name: `${K}.open` }))
    expect(onOpen).toHaveBeenCalled()
  })

  it('shows a failed availability check instead of hiding the control', async () => {
    capabilityMarketStatus.mockRejectedValue(new Error('gateway unreachable'))
    wrap(<MarketInstallEntry onOpen={() => {}} />)
    expect(await screen.findByText('gateway unreachable')).toBeTruthy()
  })
})

describe('marketInstalledNotice', () => {
  it('sends an installed app to Library, turned off, like an install from a path', () => {
    expect(marketInstalledNotice({ ref: 'team-a/demo-app', kinds: ['app'], specRewritten: true }))
      .toEqual({ message: 'pages.appsPage.installed_app_find_in_library_and_enable demo-app', libraryLink: true })
  })

  it('names the installed app without the @version the user typed', () => {
    expect(marketInstalledNotice({ ref: 'team-a/demo-app@^1.0.0', kinds: ['app'], specRewritten: true }))
      .toEqual({ message: 'pages.appsPage.installed_app_find_in_library_and_enable demo-app', libraryLink: true })
  })

  it('keeps the plain notice and no Library link for skills and MCP servers', () => {
    expect(marketInstalledNotice({ ref: 'team-a/pdf-tools', kinds: ['skill', 'mcp'], specRewritten: true }))
      .toEqual({ message: `${K}.installed team-a/pdf-tools ${K}.kind_skill, ${K}.kind_mcp`, libraryLink: false })
  })
})

describe('marketInstalledMessage', () => {
  it('names what the install placed', () => {
    expect(marketInstalledMessage({ ref: 'team-a/pdf-tools', kinds: ['skill', 'mcp'], specRewritten: true }))
      .toBe(`${K}.installed team-a/pdf-tools ${K}.kind_skill, ${K}.kind_mcp`)
  })
})
