/**
 * The Agents page usage card's harness + account rows.
 *
 * These rows answer one question before work starts: which harness am I on, and
 * which account will be billed. Two properties are load-bearing and are pinned
 * here because neither is visible from reading the markup alone.
 *
 * The email is MASKED. This card is a routine screen-share surface, so the full
 * address must not reach the DOM even though the API returns it — asserting only
 * that the masked form appears would still pass if the raw address were rendered
 * alongside it, so the raw address is asserted absent too.
 *
 * An absent account is a STEADY STATE, not a failure. The backend attaches
 * identity only when the signed-in account provably matches the one the credits
 * were billed to, and individual / Builder ID accounts carry no profile ARN to
 * match on, so they never resolve. The card must say so rather than rendering an
 * empty row that reads as a loading or broken state.
 */
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const mockApi = vi.hoisted(() => ({
  spawnList: vi.fn(),
  sessionsContext: vi.fn(),
  sessionsUsage: vi.fn(),
  agentsInstalled: vi.fn(),
  mcpProbeCache: vi.fn(),
  defaultAgent: vi.fn(),
  agentDetail: vi.fn(),
  agentMetadata: vi.fn(),
  kirocrewAgents: vi.fn(),
  skills: vi.fn(),
  agentPatch: vi.fn(),
  spawnClear: vi.fn(),
  spawnDelete: vi.fn(),
  setDefaultAgent: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))
vi.mock('../store', () => ({ useAppSelector: (fn: (s: unknown) => unknown) => fn({ dashboard: { status: { sessions: 1, subagents: 0 }, refreshTrigger: 0 } }) }))
vi.mock('../providers', () => ({
  useProvider: () => ({
    id: 'acp',
    displayName: 'ACP',
    capabilities: { agentTemplates: true },
    labels: { sessionProcess: 'ACP subprocess', configFile: 'agent.json' },
    fetchAvailableModels: () => Promise.resolve([]),
  }),
}))

import AgentsPage, { maskAccountEmail, accountDisplayValue, addressSafeLabel, authTypeLabel } from '../pages/AgentsPage'

const mkAgent = (name: string) => ({
  name,
  description: `${name} agent`,
  source: 'builtin',
  model: 'claude-opus-4.8',
  skills: [],
  mcp_servers: [],
  filename: `${name}.json`,
})

function renderPage() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <AgentsPage embedded />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  Object.values(mockApi).forEach(fn => fn.mockReset())
  mockApi.spawnList.mockResolvedValue({ agents: [] })
  mockApi.sessionsContext.mockResolvedValue({ sessions: [] })
  mockApi.sessionsUsage.mockResolvedValue({ usage: null })
  mockApi.agentsInstalled.mockResolvedValue([mkAgent('kirocrew')])
  mockApi.mcpProbeCache.mockResolvedValue([])
  mockApi.agentMetadata.mockResolvedValue({ content: '' })
  mockApi.kirocrewAgents.mockResolvedValue({ agents: [], default_agent: '' })
  mockApi.skills.mockResolvedValue([])
  mockApi.agentDetail.mockResolvedValue({ ...mkAgent('kirocrew'), unmanaged_skills: [] })
})

describe('maskAccountEmail', () => {
  it('keeps the first character and the domain, dropping the rest of the local part', () => {
    expect(maskAccountEmail('alice@example.com')).toBe('a•••@example.com')
  })

  it('masks a bare handle that carries no domain to anchor on', () => {
    expect(maskAccountEmail('kseam')).toBe('k•••')
  })

  it('masks on the LAST @ so a local part containing one cannot leak', () => {
    expect(maskAccountEmail('a@b@example.com')).toBe('a•••@example.com')
  })

  it('returns null for input with nothing to mask, so callers omit the row', () => {
    expect(maskAccountEmail(undefined)).toBeNull()
    expect(maskAccountEmail('')).toBeNull()
    expect(maskAccountEmail('   ')).toBeNull()
    // A leading '@' has no local-part character to keep.
    expect(maskAccountEmail('@example.com')).toBeNull()
  })
})

describe('addressSafeLabel', () => {
  it('passes a non-address label through unchanged', () => {
    expect(addressSafeLabel('IamIdentityCenter')).toBe('IamIdentityCenter')
    expect(addressSafeLabel('BuilderId')).toBe('BuilderId')
  })

  it('masks a label that is address-shaped', () => {
    expect(addressSafeLabel('alice@example.com')).toBe('a•••@example.com')
  })

  it('returns null for an empty or whitespace-only label', () => {
    expect(addressSafeLabel(undefined)).toBeNull()
    expect(addressSafeLabel('   ')).toBeNull()
  })
})

describe('authTypeLabel', () => {
  // The raw enum reaching the UI is the defect this guards: users would read
  // "IamIdentityCenter" here while the credit modal says "IAM Identity Center".
  it('maps the known auth-type enum to the same localized prose the credit modal uses', () => {
    expect(authTypeLabel('IamIdentityCenter')).toBe('IAM Identity Center')
    expect(authTypeLabel('BuilderId')).toBe('Builder ID')
    expect(authTypeLabel('Social')).toBe('Social login')
  })

  it('address-guards a value outside the known enum', () => {
    expect(authTypeLabel('alice@example.com')).toBe('a•••@example.com')
    expect(authTypeLabel('SomethingNew')).toBe('SomethingNew')
    expect(authTypeLabel(undefined)).toBeNull()
  })
})

describe('accountDisplayValue', () => {
  it('prefers the email over the profile name', () => {
    expect(accountDisplayValue({ email: 'alice@example.com', account: 'eng-profile' }))
      .toBe('a•••@example.com')
  })

  it('renders a profile name that is not address-shaped verbatim', () => {
    expect(accountDisplayValue({ account: 'kiro-eng-profile' })).toBe('kiro-eng-profile')
  })

  // The profile name is an arbitrary provider-supplied label, so it must not be
  // a way around the masking the email path applies.
  it('masks a profile name that IS address-shaped', () => {
    expect(accountDisplayValue({ account: 'alice@example.com' })).toBe('a•••@example.com')
  })

  it('reports nothing when an address-shaped profile name cannot be masked', () => {
    expect(accountDisplayValue({ account: '@example.com' })).toBeNull()
  })

  it('reports nothing when neither field is present', () => {
    expect(accountDisplayValue({})).toBeNull()
    expect(accountDisplayValue({ account: '  ' })).toBeNull()
  })
})

describe('AgentsPage harness and account rows', () => {
  it('renders the harness with its auth type and the MASKED account, never the raw address', async () => {
    mockApi.sessionsUsage.mockResolvedValue({
      usage: {
        plan: 'KIRO PRO',
        credits_used: 120,
        credits_plan: 1000,
        email: 'alice@example.com',
        // The REAL payload shape: whoami emits the code identifier, not prose.
        account_type: 'IamIdentityCenter',
      },
    })
    renderPage()

    await waitFor(() => expect(screen.getByText('a•••@example.com')).toBeInTheDocument())
    expect(screen.getByText('Harness')).toBeInTheDocument()
    expect(screen.getByText('Account')).toBeInTheDocument()
    // Localized prose, not the camelCase enum.
    expect(screen.getByText(/IAM Identity Center/)).toBeInTheDocument()
    expect(document.body.textContent).not.toContain('IamIdentityCenter')
    // The raw address must not reach the DOM anywhere on the page.
    expect(screen.queryByText(/alice@example\.com/)).not.toBeInTheDocument()
    expect(document.body.textContent).not.toContain('alice@example.com')
  })

  it('says the account is not reported when the backend could not prove one', async () => {
    mockApi.sessionsUsage.mockResolvedValue({
      usage: { plan: 'KIRO PRO', credits_used: 120, credits_plan: 1000 },
    })
    renderPage()

    await waitFor(() => expect(screen.getByText('Not reported')).toBeInTheDocument())
    expect(screen.getByText('Account')).toBeInTheDocument()
  })

  it('falls back to the org profile name when no email is exposed', async () => {
    mockApi.sessionsUsage.mockResolvedValue({
      usage: { plan: 'KIRO PRO', credits_used: 120, credits_plan: 1000, account: 'kiro-eng-profile' },
    })
    renderPage()

    await waitFor(() => expect(screen.getByText('kiro-eng-profile')).toBeInTheDocument())
    expect(screen.queryByText('Not reported')).not.toBeInTheDocument()
  })

  // The profile-name fallback bypassed masking entirely at one point. Asserting
  // on the masked form alone would not have caught it, since the raw value can
  // be rendered alongside; the raw address is asserted absent from the whole DOM.
  it('masks an address-shaped profile name, keeping the raw address out of the DOM', async () => {
    mockApi.sessionsUsage.mockResolvedValue({
      usage: {
        plan: 'KIRO PRO', credits_used: 120, credits_plan: 1000,
        account: 'alice@example.com',
      },
    })
    renderPage()

    await waitFor(() => expect(screen.getByText('a•••@example.com')).toBeInTheDocument())
    expect(document.body.textContent).not.toContain('alice@example.com')
  })

  // The Harness row renders a provider-supplied label too, so it is held to the
  // same invariant: no field on this card puts a full address on screen.
  it('masks an address-shaped account_type on the Harness row', async () => {
    mockApi.sessionsUsage.mockResolvedValue({
      usage: {
        plan: 'KIRO PRO', credits_used: 120, credits_plan: 1000,
        account_type: 'alice@example.com',
      },
    })
    renderPage()

    await waitFor(() => expect(screen.getByText(/a•••@example\.com/)).toBeInTheDocument())
    expect(document.body.textContent).not.toContain('alice@example.com')
  })
})
