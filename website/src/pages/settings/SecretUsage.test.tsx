import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { SecretsUsageResponse } from '../../api/client'
import en from '../../i18n/locales/en.json'
import { SecretInUseNote, SecretUsageCard, useReferencedNames } from './SecretUsage'

const usage = vi.fn<() => Promise<SecretsUsageResponse>>()
vi.mock('../../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../../api/client')>()
  return { ...mod, api: { ...mod.api, secretsUsage: () => usage() } }
})

const s = en.settings.secrets

function mount(name: string) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <SecretUsageCard name={name} allowAgentHandoff={false} />
      <SecretInUseNote name={name} />
    </QueryClientProvider>,
  )
}

describe('SecretUsageCard', () => {
  beforeEach(() => usage.mockReset())

  it('lists each place that uses the item and warns on delete', async () => {
    usage.mockResolvedValue({
      usage: { GH: [{ kind: 'mcp_server', name: 'github', key: 'GITHUB_TOKEN' }, { kind: 'cron_job', name: 'nightly', key: 'TOKEN' }] },
      incomplete: [],
    })
    mount('GH')
    expect(await screen.findByText('github')).toBeTruthy()
    expect(screen.getByText(s.usage_kind_mcp_server)).toBeTruthy()
    expect(screen.getByText('nightly')).toBeTruthy()
    expect(screen.getByText(s.usage_kind_cron_job)).toBeTruthy()
    expect(screen.getByText(s.delete_in_use)).toBeTruthy()
    expect(screen.queryByText(s.usage_none)).toBeNull()
  })

  it('says no use was found when the scan lists no place', async () => {
    usage.mockResolvedValue({ usage: {}, incomplete: [] })
    mount('OLD')
    expect(await screen.findByText(s.usage_none)).toBeTruthy()
    expect(screen.queryByText(s.delete_in_use)).toBeNull()
  })

  it('names only the items some place references', async () => {
    usage.mockResolvedValue({ usage: { GH: [{ kind: 'env_file', name: '.env', key: 'JIRA_API_TOKEN' }], OLD: [] }, incomplete: [] })
    function Probe() {
      const names = useReferencedNames()
      return <span>{[...names].join(',') || 'none'}</span>
    }
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<QueryClientProvider client={qc}><Probe /></QueryClientProvider>)
    expect(await screen.findByText('GH')).toBeTruthy()
  })

  it('shows no "no use found" when a kind of place could not be checked', async () => {
    usage.mockResolvedValue({ usage: {}, incomplete: ['env_file', 'cron_job'] })
    mount('OLD')
    // The notice names each skipped kind of place by its own label.
    const notice = await screen.findByText(new RegExp(s.usage_kind_env_file))
    expect(notice.textContent).toContain(s.usage_kind_cron_job)
    expect(screen.queryByText(s.usage_none)).toBeNull()
  })

  it('reads an item named after an Object prototype property as its own entry', async () => {
    usage.mockResolvedValue({ usage: {}, incomplete: [] })
    mount('constructor')
    expect(await screen.findByText(s.usage_none)).toBeTruthy()
    expect(screen.queryByText(s.delete_in_use)).toBeNull()
  })
})
