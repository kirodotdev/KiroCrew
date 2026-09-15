import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'

/* ── api client mock ───────────────────────────────────────────────────────
 * The panel reads and writes only through these three methods, so mocking them
 * keeps every case network-free and lets the save/remove cases assert on the
 * REQUEST the panel sends (slug + body), not just on the re-render it causes. */
vi.mock('../../api/client', () => ({
  api: {
    connectionsOAuthClients: vi.fn(),
    connectionsOAuthClientSave: vi.fn(),
    connectionsOAuthClientDelete: vi.fn(),
  },
}))

// The redirect URI copy button is the one clipboard write on the panel; stub
// the helper so the test can assert WHAT was copied without a real clipboard.
vi.mock('../../utils/clipboard', () => ({
  copyToClipboard: vi.fn(() => Promise.resolve(true)),
}))

import { api, type ConnectionOAuthClient } from '../../api/client'
import { copyToClipboard } from '../../utils/clipboard'
import { ConnectionsPanel, oauthClientSettingId, OAUTH_CLIENTS_QUERY_KEY } from './ConnectionsPanel'

type Mock = ReturnType<typeof vi.fn>
const listMock = api.connectionsOAuthClients as unknown as Mock
const saveMock = api.connectionsOAuthClientSave as unknown as Mock
const deleteMock = api.connectionsOAuthClientDelete as unknown as Mock
const copyMock = copyToClipboard as unknown as Mock

const GITHUB_REDIRECT = 'http://127.0.0.1:48101/callback'
const ASANA_REDIRECT = 'http://127.0.0.1:48102/callback'

/** GET /api/connections/oauth-clients row for GitHub: confidential, nothing entered yet. */
function github(over: Partial<ConnectionOAuthClient> = {}): ConnectionOAuthClient {
  return {
    slug: 'github',
    confidential: true,
    redirect_uri: GITHUB_REDIRECT,
    registration_guide: 'oauth-app-registration/github.md',
    client_id: null,
    client_id_source: null,
    client_secret_set: false,
    client_secret_source: null,
    configured: false,
    ...over,
  }
}

/** Asana with a complete client record saved through the dashboard. */
function asana(over: Partial<ConnectionOAuthClient> = {}): ConnectionOAuthClient {
  return {
    slug: 'asana',
    confidential: true,
    redirect_uri: ASANA_REDIRECT,
    registration_guide: 'oauth-app-registration/asana.md',
    client_id: 'asana-client-id',
    client_id_source: 'config',
    client_secret_set: true,
    client_secret_source: 'vault',
    configured: true,
    ...over,
  }
}

function listResponse(clients: ConnectionOAuthClient[]) {
  return { schema_version: 1, clients }
}

/** The card root for one provider, addressed the way the settings deep link finds it. */
function cardFor(slug: string): HTMLElement {
  const el = document.querySelector<HTMLElement>(`[data-setting-id="${oauthClientSettingId(slug)}"]`)
  if (!el) throw new Error(`no card rendered for ${slug}`)
  return el
}

async function mount(clients: ConnectionOAuthClient[] = [github(), asana()], readOnly = false) {
  listMock.mockResolvedValue(listResponse(clients))
  const utils = renderWithProviders(<ConnectionsPanel readOnly={readOnly} />)
  // Wait for the list to land: the section title renders during loading too.
  await waitFor(() => expect(screen.queryByText('Loading…')).not.toBeInTheDocument())
  return utils
}

beforeEach(() => {
  listMock.mockReset()
  saveMock.mockReset().mockResolvedValue({ ok: true, client: github({ client_id: 'abc', client_id_source: 'config' }) })
  deleteMock.mockReset().mockResolvedValue({ ok: true, client: github() })
  copyMock.mockClear()
})

describe('ConnectionsPanel — one card per pre-registered provider', () => {
  it('renders a card per client with its configured state and settings anchor', async () => {
    await mount()

    expect(screen.getByText('OAuth apps for Connections')).toBeInTheDocument()
    expect(listMock).toHaveBeenCalledTimes(1)

    const gh = cardFor('github')
    expect(gh.getAttribute('data-setting-id')).toBe('connections-oauth-client-github')
    expect(within(gh).getByRole('heading', { name: 'GitHub' })).toBeInTheDocument()
    expect(within(gh).getByText('Needs configuration')).toBeInTheDocument()
    expect(within(gh).queryByText('Configured')).not.toBeInTheDocument()

    const as = cardFor('asana')
    expect(as.getAttribute('data-setting-id')).toBe('connections-oauth-client-asana')
    expect(within(as).getByRole('heading', { name: 'Asana' })).toBeInTheDocument()
    expect(within(as).getByText('Configured')).toBeInTheDocument()
    expect(within(as).queryByText('Needs configuration')).not.toBeInTheDocument()

    // Exactly the two cards the API listed -- nothing synthesised from the registry.
    expect(document.querySelectorAll('[data-setting-id^="connections-oauth-client-"]')).toHaveLength(2)
  })

  it('shows the exact redirect URI to register and copies it on request', async () => {
    await mount()

    const gh = cardFor('github')
    expect(within(gh).getByText(GITHUB_REDIRECT)).toBeInTheDocument()
    expect(within(cardFor('asana')).getByText(ASANA_REDIRECT)).toBeInTheDocument()

    fireEvent.click(within(gh).getByRole('button', { name: 'Copy redirect URI' }))
    expect(copyMock).toHaveBeenCalledWith(GITHUB_REDIRECT)
    expect(await within(gh).findByText('Copied')).toBeInTheDocument()
  })

  it('links each card to its setup guide', async () => {
    await mount()

    const link = within(cardFor('github')).getByRole('link', { name: /Open the GitHub setup guide/ })
    expect(link).toHaveAttribute('href', 'https://github.com/kirodotdev/KiroCrew/blob/main/docs/guides/oauth-app-registration/github.md')
    expect(link).toHaveAttribute('target', '_blank')
  })

  it('explains the empty and failed list states', async () => {
    await mount([])
    expect(screen.getByText(/nothing to configure here/)).toBeInTheDocument()

    listMock.mockRejectedValue(new Error('gateway down'))
    renderWithProviders(<ConnectionsPanel />)
    expect(await screen.findByText('Could not load OAuth apps')).toBeInTheDocument()
    expect(screen.getByText('gateway down')).toBeInTheDocument()
  })
})

describe('ConnectionsPanel — saving a client', () => {
  it('keeps Save disabled until the Client ID changes, then sends only the id', async () => {
    await mount()
    const gh = cardFor('github')
    const save = within(gh).getByRole('button', { name: 'Save' })
    expect(save).toBeDisabled()

    const idInput = within(gh).getByLabelText('Client ID')
    expect(idInput).toBeEnabled()
    fireEvent.change(idInput, { target: { value: 'abc' } })
    expect(save).toBeEnabled()

    fireEvent.click(save)
    await waitFor(() => expect(saveMock).toHaveBeenCalledTimes(1))
    expect(saveMock).toHaveBeenCalledWith('github', { client_id: 'abc' })
    // A successful save re-reads the list so the Configured badge can flip.
    await waitFor(() => expect(listMock).toHaveBeenCalledTimes(2))
  })

  it('trims the id and ignores a change back to the stored value', async () => {
    await mount([github({ client_id: 'stored', client_id_source: 'config' })])
    const gh = cardFor('github')
    const save = within(gh).getByRole('button', { name: 'Save' })
    const idInput = within(gh).getByLabelText('Client ID')
    expect(idInput).toHaveValue('stored')

    fireEvent.change(idInput, { target: { value: '  stored ' } })
    expect(save).toBeDisabled()

    fireEvent.change(idInput, { target: { value: '  new-id ' } })
    expect(save).toBeEnabled()
    fireEvent.click(save)
    await waitFor(() => expect(saveMock).toHaveBeenCalledWith('github', { client_id: 'new-id' }))
  })

  it('sends a typed secret without touching the id', async () => {
    await mount()
    const gh = cardFor('github')
    const save = within(gh).getByRole('button', { name: 'Save' })
    expect(save).toBeDisabled()

    // No secret is stored, so SecretField renders its editor straight away.
    const secretInput = within(gh).getByLabelText('Client secret')
    expect(secretInput).toHaveAttribute('type', 'password')
    fireEvent.change(secretInput, { target: { value: 's3cret' } })
    expect(save).toBeEnabled()

    fireEvent.click(save)
    await waitFor(() => expect(saveMock).toHaveBeenCalledTimes(1))
    expect(saveMock).toHaveBeenCalledWith('github', { client_secret: 's3cret' })
  })

  it('sends id and secret together when both changed', async () => {
    await mount()
    const gh = cardFor('github')
    fireEvent.change(within(gh).getByLabelText('Client ID'), { target: { value: 'abc' } })
    fireEvent.change(within(gh).getByLabelText('Client secret'), { target: { value: 's3cret' } })
    fireEvent.click(within(gh).getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(saveMock).toHaveBeenCalledWith('github', { client_id: 'abc', client_secret: 's3cret' }))
  })

  it('surfaces a rejected save on the card', async () => {
    saveMock.mockRejectedValue(new Error('invalid_client_id'))
    await mount()
    const gh = cardFor('github')
    fireEvent.change(within(gh).getByLabelText('Client ID'), { target: { value: 'abc' } })
    fireEvent.click(within(gh).getByRole('button', { name: 'Save' }))

    expect(await within(gh).findByText('Could not save the OAuth app')).toBeInTheDocument()
    expect(within(gh).getByText('invalid_client_id')).toBeInTheDocument()
  })

  it('names the confidential requirement per provider', async () => {
    await mount([github(), asana({ confidential: false, client_secret_set: false, client_secret_source: null })])
    expect(within(cardFor('github')).getByText(/GitHub requires a client secret at the token endpoint/)).toBeInTheDocument()
    expect(within(cardFor('asana')).getByText(/Asana accepts a public client, so the secret is optional/)).toBeInTheDocument()
  })
})

describe('ConnectionsPanel — environment-sourced halves', () => {
  it('disables the Client ID input and names the overriding variable', async () => {
    await mount([github({ client_id: 'from-env', client_id_source: 'env' })])
    const gh = cardFor('github')

    const idInput = within(gh).getByLabelText('Client ID')
    expect(idInput).toBeDisabled()
    expect(idInput).toHaveValue('from-env')
    expect(within(gh).getByText(
      'Set by the environment variable KIROCREW_CONNECTIONS_GITHUB_CLIENT_ID, which overrides any value saved here.',
    )).toBeInTheDocument()
    // The dashboard-stored help copy is replaced, not appended.
    expect(within(gh).queryByText(/Public identifier of the registered app/)).not.toBeInTheDocument()
  })

  it('names the secret variable when the secret is environment-sourced', async () => {
    await mount([github({ client_secret_set: true, client_secret_source: 'env' })])
    const gh = cardFor('github')
    expect(within(gh).getByText(
      'Set by the environment variable KIROCREW_CONNECTIONS_GITHUB_CLIENT_SECRET, which overrides any value saved here.',
    )).toBeInTheDocument()
    // Not a SecretField (whose read-only mode talks about a remote session): a
    // disabled masked input, because the ENVIRONMENT owns this half.
    const secretInput = within(gh).getByLabelText('Client secret')
    expect(secretInput).toBeDisabled()
    expect(secretInput).toHaveValue('••••••••')
  })
})

describe('ConnectionsPanel — removing a client', () => {
  it('arms on the first press and only deletes on the danger confirm', async () => {
    await mount([github({ client_id: 'abc', client_id_source: 'config', client_secret_set: true, client_secret_source: 'vault', configured: true })])
    const gh = cardFor('github')
    const remove = within(gh).getByRole('button', { name: 'Remove the GitHub OAuth app' })
    expect(remove).toBeEnabled()

    fireEvent.click(remove)
    // Armed, not fired: the consequence is spelled out and focus sits on Cancel.
    expect(deleteMock).not.toHaveBeenCalled()
    expect(within(gh).getByText(/Remove the GitHub OAuth app\? The client ID and the stored secret are deleted/)).toBeInTheDocument()
    expect(within(gh).getByRole('button', { name: 'Cancel' })).toHaveFocus()
    expect(within(gh).queryByRole('button', { name: 'Save' })).toBeNull()

    fireEvent.click(within(gh).getByRole('button', { name: 'Remove OAuth app' }))
    await waitFor(() => expect(deleteMock).toHaveBeenCalledTimes(1))
    expect(deleteMock).toHaveBeenCalledWith('github')
    await waitFor(() => expect(listMock).toHaveBeenCalledTimes(2))
  })

  it('backs out of an armed remove without deleting', async () => {
    await mount([github({ client_id: 'abc', client_id_source: 'config', client_secret_set: true, client_secret_source: 'vault', configured: true })])
    const gh = cardFor('github')
    fireEvent.click(within(gh).getByRole('button', { name: 'Remove the GitHub OAuth app' }))
    fireEvent.click(within(gh).getByRole('button', { name: 'Cancel' }))
    expect(deleteMock).not.toHaveBeenCalled()
    expect(within(gh).getByRole('button', { name: 'Remove the GitHub OAuth app' })).toBeEnabled()
  })

  it('offers nothing to remove when neither half is stored', async () => {
    await mount()
    expect(within(cardFor('github')).getByRole('button', { name: 'Remove the GitHub OAuth app' })).toBeDisabled()
    expect(within(cardFor('asana')).getByRole('button', { name: 'Remove the Asana OAuth app' })).toBeEnabled()
  })

  it('cannot remove a record the environment fully owns', async () => {
    await mount([github({
      client_id: 'from-env', client_id_source: 'env',
      client_secret_set: true, client_secret_source: 'env', configured: true,
    })])
    expect(within(cardFor('github')).getByRole('button', { name: 'Remove the GitHub OAuth app' })).toBeDisabled()
  })
})

describe('ConnectionsPanel — read-only mount', () => {
  it('hides Save and Remove and locks both inputs', async () => {
    await mount([github()], true)
    const gh = cardFor('github')
    expect(within(gh).queryByRole('button', { name: 'Save' })).not.toBeInTheDocument()
    expect(within(gh).queryByRole('button', { name: /Remove/ })).not.toBeInTheDocument()
    expect(within(gh).getByLabelText('Client ID')).toBeDisabled()
    expect(within(gh).queryByLabelText('Client secret')).not.toBeInTheDocument()
  })
})

describe('ConnectionsPanel — exported handles', () => {
  it('derives the deep-link anchor id the gallery card routes to', () => {
    expect(oauthClientSettingId('github')).toBe('connections-oauth-client-github')
    expect(OAUTH_CLIENTS_QUERY_KEY).toEqual(['connections', 'oauth-clients'])
  })
})
