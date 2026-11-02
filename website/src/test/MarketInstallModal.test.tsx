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

vi.mock('../api/client', () => ({
  api: {
    capabilityMarketStatus: (...a: unknown[]) => capabilityMarketStatus(...a),
    capabilityMarketPlan: (...a: unknown[]) => capabilityMarketPlan(...a),
    capabilityMarketApply: (...a: unknown[]) => capabilityMarketApply(...a),
  },
}))

// Render catalog KEYS (with interpolated values appended), not English.
vi.mock('../i18n/t', () => ({
  i18nT: (key: string, vars?: Record<string, unknown>) =>
    vars && Object.keys(vars).length ? `${key} ${Object.values(vars).join(' ')}` : key,
}))

import MarketInstallModal, { MarketInstallEntry } from '../components/appstore/MarketInstallModal'

const K = 'components.appstore.marketInstall'

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
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
    await review('team-a/pdf-tools', () => screen.findByText('team-a/docs-mcp 1.0.0 was withdrawn'))
    expect(screen.queryByRole('button', { name: `${K}.install` })).toBeNull()
    // No primary action leads anywhere from a refused plan: only Cancel, plus a hint.
    expect(screen.queryByRole('button', { name: `${K}.review` })).toBeNull()
    expect(screen.getByText(`${K}.refused_hint`)).toBeTruthy()
    // The refused plan is not offered, so its item list is not shown as if it were.
    expect(screen.queryByTestId('market-plan')).toBeNull()
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
