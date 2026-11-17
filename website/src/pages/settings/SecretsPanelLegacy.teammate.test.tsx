/**
 * Settings > Secrets for someone other than the machine owner: the vault holds
 * credentials that would outlive a teammate's access, so the page is one notice,
 * not a failed list, an Add button that cannot save and a command for a shell
 * they cannot reach.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { SecretsPanelLegacy } from './SecretsPanelLegacy'
import { SECRETS_AUTHORIZE_COMMAND } from './secretsAuthorizeCommand'

function answer(status: number, body: unknown): Response {
  const text = JSON.stringify(body)
  return {
    ok: status < 400, status, url: '', headers: { get: () => null },
    json: () => Promise.resolve(body), text: () => Promise.resolve(text),
  } as unknown as Response
}

function serve({ ownerOk, secrets }: { ownerOk: boolean | undefined; secrets: Response }) {
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url === '/api/auth/me') return answer(200, { owner_ok: ownerOk })
    if (url === '/api/secrets') return secrets
    return answer(200, {})
  }))
}

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return render(<QueryClientProvider client={qc}><SecretsPanelLegacy /></QueryClientProvider>)
}

afterEach(() => { vi.unstubAllGlobals() })

describe('SecretsPanelLegacy — someone other than the machine owner', () => {
  it('gives a teammate one owner-only notice: no list error, no Add secret, no authorize command', async () => {
    serve({ ownerOk: false, secrets: answer(200, { names: ['GITHUB_TOKEN'], managed: [] }) })
    mount()
    expect(await screen.findByTestId('secrets-owner-only')).toHaveTextContent("Only the Crew's owner can see or change secrets.")
    expect(screen.queryByRole('button', { name: /Add secret/ })).toBeNull()
    expect(screen.queryByText(SECRETS_AUTHORIZE_COMMAND)).toBeNull()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  const refused = { error: "Crew's local tokens and secrets aren't available through the relay.", code: 'relay_local_credential' }

  it("gives a teammate the owner-only notice for the relay's local-credential refusal too", async () => {
    serve({ ownerOk: false, secrets: answer(403, refused) })
    mount()
    expect(await screen.findByTestId('secrets-owner-only')).toBeInTheDocument()
    expect(screen.queryByText(/Could not load secrets/)).toBeNull()
    expect(screen.queryByRole('button', { name: /Add secret/ })).toBeNull()
  })

  it('tells the owner on Remote Control the relay does not carry secrets, not that only the owner can', async () => {
    serve({ ownerOk: true, secrets: answer(403, refused) })
    mount()
    expect(await screen.findByTestId('secrets-relay-unavailable')).toHaveTextContent("Secrets can be seen and changed only on the Crew's computer, not through Remote Control.")
    expect(screen.queryByTestId('secrets-owner-only')).toBeNull()
    expect(screen.queryByRole('button', { name: /Add secret/ })).toBeNull()
  })

  it('keeps the vault for the owner', async () => {
    serve({ ownerOk: true, secrets: answer(200, { names: [], managed: [] }) })
    mount()
    expect(await screen.findByRole('button', { name: /Add secret/ })).toBeInTheDocument()
    expect(screen.queryByTestId('secrets-owner-only')).toBeNull()
  })
})
