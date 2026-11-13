import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'

/* ── api client mock ───────────────────────────────────────────────────────
 * The panel reads and writes only through these three methods, so mocking them
 * keeps every case network-free and lets the save/remove cases assert on the
 * REQUEST the panel sends (slug + body), not just on the re-render it causes. */
vi.mock('../../api/client', async () => ({
  ApiError: (await vi.importActual<typeof import('../../api/apiError')>('../../api/apiError')).ApiError,
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

import { api, ApiError, type ConnectionOAuthClient } from '../../api/client'
import { copyToClipboard } from '../../utils/clipboard'
import { OAuthAppPanel, OAuthClientWrittenContext, OAuthDraftValuesContext, oauthClientSettingId, OAUTH_CLIENTS_QUERY_KEY, type OAuthDraftValues } from './OAuthAppPanel'
import { GOOGLE_ENTRY_SLUG, isGoogleSlug } from './connectionsLinks'

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

/** One part per listed provider, as the Connections cards open them; the
 *  Google products share one part, as the Google entry opens it. */
async function mount(clients: ConnectionOAuthClient[] = [github(), asana()], readOnly = false) {
  listMock.mockResolvedValue(listResponse(clients))
  const google = clients.map(c => c.slug).filter(isGoogleSlug)
  const utils = renderWithProviders(
    <>
      {clients.filter(c => !isGoogleSlug(c.slug)).map(c => (
        <OAuthAppPanel key={c.slug} slugs={[c.slug]} settingSlug={c.slug} readOnly={readOnly} />
      ))}
      {google.length > 0 && <OAuthAppPanel slugs={google} settingSlug={GOOGLE_ENTRY_SLUG} readOnly={readOnly} />}
    </>,
  )
  // Wait for the list to land.
  await waitFor(() => expect(screen.queryByText('Loading…')).not.toBeInTheDocument())
  return utils
}

/** One part per Google product too: the page shows one Google entry, but a
 *  second tab or an older link can still hold a draft on a single product. */
async function mountEach(clients: ConnectionOAuthClient[]) {
  listMock.mockResolvedValue(listResponse(clients))
  const google = clients.map(c => c.slug).filter(isGoogleSlug)
  const utils = renderWithProviders(
    <>{clients.map(c => (
      <OAuthAppPanel
        key={c.slug}
        slugs={isGoogleSlug(c.slug) ? [c.slug, ...google.filter(s => s !== c.slug)] : [c.slug]}
        settingSlug={c.slug}
      />
    ))}</>,
  )
  await waitFor(() => expect(screen.queryByText('Loading…')).not.toBeInTheDocument())
  return utils
}

beforeEach(() => {
  listMock.mockReset()
  saveMock.mockReset().mockResolvedValue({ ok: true, client: github({ client_id: 'abc', client_id_source: 'config' }) })
  deleteMock.mockReset().mockResolvedValue({ ok: true, client: github() })
  copyMock.mockClear()
})

describe('OAuthAppPanel — one part per pre-registered provider', () => {
  it('renders each part with its configured state and settings anchor, from one list read', async () => {
    await mount()

    expect(listMock).toHaveBeenCalledTimes(1)

    const gh = cardFor('github')
    expect(gh.getAttribute('data-setting-id')).toBe('connections-oauth-client-github')
    expect(within(gh).getByRole('heading', { name: 'OAuth app' })).toBeInTheDocument()
    expect(within(gh).getByText('Needs configuration')).toBeInTheDocument()
    expect(within(gh).queryByText('Configured')).not.toBeInTheDocument()

    const as = cardFor('asana')
    expect(as.getAttribute('data-setting-id')).toBe('connections-oauth-client-asana')
    expect(within(as).getByText(/Register an app in the Asana developer console/)).toBeInTheDocument()
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

  it('explains a provider the gateway does not list, and a failed list read', async () => {
    listMock.mockResolvedValue(listResponse([]))
    renderWithProviders(<OAuthAppPanel slugs={['github']} settingSlug="github" />)
    expect(await screen.findByText(/nothing to configure here/)).toBeInTheDocument()

    listMock.mockRejectedValue(new Error('gateway down'))
    renderWithProviders(<OAuthAppPanel slugs={['asana']} settingSlug="asana" />)
    expect(await screen.findByText('Could not load OAuth apps')).toBeInTheDocument()
    expect(screen.getByText('gateway down')).toBeInTheDocument()
  })
})

describe('OAuthAppPanel — saving a client', () => {
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

  it('says the saved app did not reach the agent spec when the rebuild declined', async () => {
    saveMock.mockResolvedValue({
      ok: true,
      client: github({ client_id: 'abc', client_id_source: 'config' }),
      agent_spec_rewritten: false,
    })
    await mount()
    const gh = cardFor('github')
    fireEvent.change(within(gh).getByLabelText('Client ID'), { target: { value: 'abc' } })
    fireEvent.click(within(gh).getByRole('button', { name: 'Save' }))

    const notice = await within(gh).findByTestId('agent-spec-not-rewritten')
    // No hand-off: it would unmount the card's unsaved drafts.
    expect(within(notice).queryByRole('button')).not.toBeInTheDocument()
  })

  it('names the confidential requirement per provider', async () => {
    await mount([github(), asana({ confidential: false, client_secret_set: false, client_secret_source: null })])
    expect(within(cardFor('github')).getByText(/GitHub requires a client secret at the token endpoint/)).toBeInTheDocument()
    expect(within(cardFor('asana')).getByText(/Asana accepts a public client, so the secret is optional/)).toBeInTheDocument()
  })
})

describe('OAuthAppPanel — environment-sourced halves', () => {
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

describe('OAuthAppPanel — removing a client', () => {
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

  it('keeps an environment-set Client ID on screen after Remove', async () => {
    await mount([github({ client_id: 'env-id', client_id_source: 'env', client_secret_set: true, client_secret_source: 'vault', configured: true })])
    const gh = cardFor('github')
    fireEvent.click(within(gh).getByRole('button', { name: 'Remove the GitHub OAuth app' }))
    fireEvent.click(within(gh).getByRole('button', { name: 'Remove OAuth app' }))
    await waitFor(() => expect(deleteMock).toHaveBeenCalledWith('github'))
    await waitFor(() => expect(within(gh).getByLabelText('Client ID')).toHaveValue('env-id'))
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

describe('OAuthAppPanel — a failed refetch', () => {
  it('keeps another card\'s unsaved draft and shows the error beside the form', async () => {
    await mount([github({ client_id: null, client_id_source: null, client_secret_set: false, client_secret_source: null, configured: false }), asana()])
    fireEvent.change(within(cardFor('asana')).getByLabelText('Client ID'), { target: { value: 'draft-id' } })
    listMock.mockRejectedValue(new Error('list down'))
    const gh = cardFor('github')
    fireEvent.change(within(gh).getByLabelText('Client ID'), { target: { value: 'gh-id' } })
    fireEvent.click(within(gh).getByRole('button', { name: 'Save' }))

    expect((await screen.findAllByText('list down')).length).toBeGreaterThan(0)
    expect(within(cardFor('asana')).getByLabelText('Client ID')).toHaveValue('draft-id')
  })
})

describe('OAuthAppPanel — read-only mount', () => {
  it('hides Save and Remove and locks both inputs', async () => {
    await mount([github()], true)
    const gh = cardFor('github')
    expect(within(gh).queryByRole('button', { name: 'Save' })).not.toBeInTheDocument()
    expect(within(gh).queryByRole('button', { name: /Remove/ })).not.toBeInTheDocument()
    expect(within(gh).getByLabelText('Client ID')).toBeDisabled()
    expect(within(gh).queryByLabelText('Client secret')).not.toBeInTheDocument()
  })
})

describe('OAuthAppPanel — exported handles', () => {
  it('derives the deep-link anchor id the gallery card routes to', () => {
    expect(oauthClientSettingId('github')).toBe('connections-oauth-client-github')
    expect(OAUTH_CLIENTS_QUERY_KEY).toEqual(['connections', 'oauth-clients'])
  })
})

describe('OAuthAppPanel — one Google app for every Google card', () => {
  function googleCard(slug: string, port: number): ConnectionOAuthClient {
    return github({ slug, redirect_uri: `http://127.0.0.1:${port}/callback`, registration_guide: 'oauth-app-registration/google.md' })
  }
  const googleCards = () => [googleCard('gmail', 48104), googleCard('google-drive', 48103), googleCard('google-docs', 48110), github()]

  it('is one part for every Google product, listing each redirect URI and both setup links', async () => {
    await mount(googleCards())
    const google = cardFor(GOOGLE_ENTRY_SLUG)
    for (const port of [48104, 48103, 48110]) expect(within(google).getByText(`http://127.0.0.1:${port}/callback`)).toBeInTheDocument()
    expect(within(google).getByText('Gmail')).toBeInTheDocument()
    expect(within(google).getByRole('link', { name: /Private Google app guide/ })).toHaveAttribute(
      'href', 'https://github.com/kirodotdev/KiroCrew/blob/main/docs/guides/oauth-app-registration/google.md')
    expect(within(google).getByRole('link', { name: /Setup script/ }).getAttribute('href')).toMatch(/scripts\/google-oauth-setup\.(sh|ps1)$/)
    expect(document.querySelectorAll('[data-setting-id^="connections-oauth-client-"]')).toHaveLength(2)
  })

  it('saves one Google card and fills every other Google card, not GitHub', async () => {
    await mount(googleCards())
    const gmail = cardFor(GOOGLE_ENTRY_SLUG)
    expect(within(gmail).getByRole('switch', { name: 'Also save on Google Drive, Google Docs' })).toBeChecked()
    expect(within(cardFor('github')).queryByRole('switch', { name: /Also save on/ })).not.toBeInTheDocument()

    fireEvent.change(within(gmail).getByLabelText('Client ID'), { target: { value: 'g-id' } })
    fireEvent.change(within(gmail).getByLabelText('Client secret'), { target: { value: 'g-secret' } })
    fireEvent.click(within(gmail).getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(saveMock).toHaveBeenCalledTimes(3))
    const body = { client_id: 'g-id', client_secret: 'g-secret' }
    expect(saveMock.mock.calls).toEqual([['gmail', body], ['google-drive', body], ['google-docs', body]])
  })

  it('shows the shared Client ID on an untouched card once the list refreshes', async () => {
    await mountEach(googleCards())
    listMock.mockResolvedValue(listResponse([
      { ...googleCard('gmail', 48104), client_id: 'g-id', client_id_source: 'config' },
      { ...googleCard('google-drive', 48103), client_id: 'g-id', client_id_source: 'config' },
      googleCard('google-docs', 48110), github(),
    ]))
    const gmail = cardFor('gmail')
    fireEvent.change(within(gmail).getByLabelText('Client ID'), { target: { value: 'g-id' } })
    fireEvent.change(within(gmail).getByLabelText('Client secret'), { target: { value: 'g-secret' } })
    fireEvent.click(within(gmail).getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(within(cardFor('google-drive')).getByLabelText('Client ID')).toHaveValue('g-id'))
  })

  it('names the Google cards that did not save and keeps Save available to retry', async () => {
    saveMock.mockImplementation((slug: string) =>
      slug === 'google-docs' ? Promise.reject(new Error('boom')) : Promise.resolve({ ok: true, client: null }))
    await mount(googleCards())
    // A list refetch now would report Gmail's saved id and clear the draft.
    listMock.mockResolvedValue(listResponse([
      { ...googleCard('gmail', 48104), client_id: 'g-id', client_id_source: 'config', client_secret_set: true, client_secret_source: 'vault', configured: true },
      googleCard('google-drive', 48103), googleCard('google-docs', 48110), github(),
    ]))
    const gmail = cardFor(GOOGLE_ENTRY_SLUG)
    fireEvent.change(within(gmail).getByLabelText('Client ID'), { target: { value: 'g-id' } })
    fireEvent.change(within(gmail).getByLabelText('Client secret'), { target: { value: 'g-secret' } })
    fireEvent.click(within(gmail).getByRole('button', { name: 'Save' }))

    expect(await within(gmail).findByText('Not saved on Google Docs')).toBeInTheDocument()
    expect(within(gmail).getByText('Saved on Gmail. Press Save to try again.')).toBeInTheDocument()
    expect(within(gmail).queryByText('Could not save the OAuth app')).not.toBeInTheDocument()
    expect(saveMock).toHaveBeenCalledTimes(3)
    // Gmail took the save, so its badge refreshes from the list...
    expect(await within(gmail).findByText('Configured')).toBeInTheDocument()
    expect(within(gmail).getByLabelText('Client ID')).toHaveValue('g-id')
    // ...and Save stays armed even though the draft now matches the stored id.
    expect(within(gmail).getByRole('button', { name: 'Save' })).toBeEnabled()
    fireEvent.click(within(gmail).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(saveMock).toHaveBeenCalledTimes(6))
    expect(saveMock.mock.calls[5]).toEqual(['google-docs', { client_id: 'g-id', client_secret: 'g-secret' }])
  })

  it('tells the page every product a save or Remove wrote, even when another product failed', async () => {
    // A write cancels that product's sign-in on the backend, so the page must
    // hear about each accepted one to drop its wait (including partial fan-outs).
    saveMock.mockImplementation((slug: string) =>
      slug === 'google-docs' ? Promise.reject(new Error('boom')) : Promise.resolve({ ok: true, client: null }))
    const written = vi.fn()
    const render = (clients: ConnectionOAuthClient[]) => {
      listMock.mockResolvedValue(listResponse(clients))
      return renderWithProviders(
        <OAuthClientWrittenContext.Provider value={written}>
          <OAuthAppPanel slugs={clients.map(c => c.slug).filter(isGoogleSlug)} settingSlug={GOOGLE_ENTRY_SLUG} />
        </OAuthClientWrittenContext.Provider>,
      )
    }
    const first = render(googleCards())
    await waitFor(() => expect(screen.queryByText('Loading…')).not.toBeInTheDocument())
    let google = cardFor(GOOGLE_ENTRY_SLUG)
    fireEvent.change(within(google).getByLabelText('Client ID'), { target: { value: 'g-id' } })
    fireEvent.change(within(google).getByLabelText('Client secret'), { target: { value: 'g-secret' } })
    fireEvent.click(within(google).getByRole('button', { name: 'Save' }))
    expect(await within(google).findByText('Not saved on Google Docs')).toBeInTheDocument()
    expect(written.mock.calls.flat(2).sort()).toEqual(['gmail', 'google-drive'])

    first.unmount()
    written.mockClear()
    const stored = { client_id: 'g-id', client_id_source: 'config' as const, client_secret_set: true, client_secret_source: 'vault' as const, configured: true }
    render(['gmail', 'google-drive', 'google-docs'].map((slug, i) => ({ ...googleCard(slug, 48104 + i), ...stored })))
    await waitFor(() => expect(screen.queryByText('Loading…')).not.toBeInTheDocument())
    google = cardFor(GOOGLE_ENTRY_SLUG)
    fireEvent.click(within(google).getByRole('button', { name: /Remove the Google OAuth app/ }))
    fireEvent.click(within(google).getByRole('button', { name: 'Remove OAuth app' }))
    await waitFor(() => expect(written.mock.calls.flat(2).sort()).toEqual(['gmail', 'google-docs', 'google-drive']))
  })

  it('gives a remounted form the draft its page kept, so a layout swap loses no typed input', async () => {
    const kept = new Map<string, OAuthDraftValues>()
    const store = { get: (k: string) => kept.get(k), set: (k: string, v: OAuthDraftValues) => { kept.set(k, v) } }
    listMock.mockResolvedValue(listResponse([github()]))
    const form = () => (
      <OAuthDraftValuesContext.Provider value={store}>
        <OAuthAppPanel slugs={['github']} settingSlug="github" />
      </OAuthDraftValuesContext.Provider>
    )
    const first = renderWithProviders(form())
    await waitFor(() => expect(screen.queryByText('Loading…')).not.toBeInTheDocument())
    fireEvent.change(within(cardFor('github')).getByLabelText('Client ID'), { target: { value: 'typed-id' } })
    fireEvent.change(within(cardFor('github')).getByLabelText('Client secret'), { target: { value: 'typed-secret' } })
    first.unmount()
    renderWithProviders(form())
    await waitFor(() => expect(screen.queryByText('Loading…')).not.toBeInTheDocument())
    expect(within(cardFor('github')).getByLabelText('Client ID')).toHaveValue('typed-id')
    expect(within(cardFor('github')).getByLabelText('Client secret')).toHaveValue('typed-secret')
    expect(within(cardFor('github')).getByRole('button', { name: 'Save' })).toBeEnabled()
  })

  it('tells the page about a save the backend committed even when its agent-spec rebuild failed', async () => {
    saveMock.mockRejectedValue(new ApiError(500, 'the client was saved but the agent spec could not be rebuilt',
      JSON.stringify({ ok: false, code: 'projection_refresh_failed', slug: 'github' })))
    const written = vi.fn()
    listMock.mockResolvedValue(listResponse([github()]))
    renderWithProviders(
      <OAuthClientWrittenContext.Provider value={written}>
        <OAuthAppPanel slugs={['github']} settingSlug="github" />
      </OAuthClientWrittenContext.Provider>,
    )
    await waitFor(() => expect(screen.queryByText('Loading…')).not.toBeInTheDocument())
    const card = cardFor('github')
    fireEvent.change(within(card).getByLabelText('Client ID'), { target: { value: 'abc' } })
    fireEvent.click(within(card).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(written).toHaveBeenCalledWith(['github']))
    // A plain refusal wrote nothing, so it reports nothing.
    written.mockClear()
    saveMock.mockRejectedValue(new ApiError(400, 'bad id', JSON.stringify({ ok: false, error: 'bad id' })))
    fireEvent.click(within(card).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(saveMock).toHaveBeenCalledTimes(2))
    expect(written).not.toHaveBeenCalled()
  })

  it('skips a Google card whose client is set by the environment', async () => {
    await mount([
      googleCard('gmail', 48104),
      { ...googleCard('google-drive', 48103), client_id: 'env-id', client_id_source: 'env' },
      googleCard('google-docs', 48110),
    ])
    const gmail = cardFor(GOOGLE_ENTRY_SLUG)
    fireEvent.change(within(gmail).getByLabelText('Client ID'), { target: { value: 'g-id' } })
    fireEvent.change(within(gmail).getByLabelText('Client secret'), { target: { value: 'g-secret' } })
    fireEvent.click(within(gmail).getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(saveMock).toHaveBeenCalledTimes(2))
    expect(saveMock.mock.calls.map(c => c[0])).toEqual(['gmail', 'google-docs'])
  })

  it('keeps a Client ID change alone on its own card, so no other card gets a mismatched pair', async () => {
    await mount(googleCards())
    const gmail = cardFor(GOOGLE_ENTRY_SLUG)
    fireEvent.change(within(gmail).getByLabelText('Client ID'), { target: { value: 'g-id' } })
    fireEvent.click(within(gmail).getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(saveMock).toHaveBeenCalledTimes(1))
    expect(saveMock).toHaveBeenCalledWith('gmail', { client_id: 'g-id' })
  })

  it('keeps a secret saved without a Client ID, or a cleared secret, on its own card', async () => {
    await mount(googleCards())
    const gmail = cardFor(GOOGLE_ENTRY_SLUG)
    fireEvent.change(within(gmail).getByLabelText('Client secret'), { target: { value: 'g-secret' } })
    fireEvent.click(within(gmail).getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(saveMock).toHaveBeenCalledTimes(1))
    expect(saveMock).toHaveBeenCalledWith('gmail', { client_secret: 'g-secret' })
  })

  it('keeps another card\'s unsaved Client ID when a shared save refreshes the list', async () => {
    await mountEach(googleCards())
    const drive = cardFor('google-drive')
    fireEvent.change(within(drive).getByLabelText('Client ID'), { target: { value: 'd-id' } })
    // The refetch after Gmail's shared save reports Drive on Gmail's id.
    listMock.mockResolvedValue(listResponse([
      { ...googleCard('gmail', 48104), client_id: 'g-id', client_id_source: 'config' },
      { ...googleCard('google-drive', 48103), client_id: 'g-id', client_id_source: 'config' },
      googleCard('google-docs', 48110), github(),
    ]))
    const gmail = cardFor('gmail')
    fireEvent.change(within(gmail).getByLabelText('Client ID'), { target: { value: 'g-id' } })
    fireEvent.change(within(gmail).getByLabelText('Client secret'), { target: { value: 'g-secret' } })
    fireEvent.click(within(gmail).getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(listMock).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(within(gmail).getByLabelText('Client ID')).toHaveValue('g-id'))
    expect(within(drive).getByLabelText('Client ID')).toHaveValue('d-id')
  })

  it('keeps another card\'s id with its unsaved secret, so its next Save sends that whole pair', async () => {
    await mountEach([
      { ...googleCard('gmail', 48104), client_id: 'a-id', client_id_source: 'config' },
      { ...googleCard('google-drive', 48103), client_id: 'a-id', client_id_source: 'config', client_secret_set: true, client_secret_source: 'vault', configured: true },
    ])
    const drive = cardFor('google-drive')
    fireEvent.click(within(drive).getByRole('switch', { name: /Also save on/ }))
    fireEvent.click(within(drive).getByRole('button', { name: /Replace/ }))
    fireEvent.change(within(drive).getByLabelText('Client secret'), { target: { value: 'a-secret' } })
    listMock.mockResolvedValue(listResponse([
      { ...googleCard('gmail', 48104), client_id: 'b-id', client_id_source: 'config' },
      { ...googleCard('google-drive', 48103), client_id: 'b-id', client_id_source: 'config', client_secret_set: true, client_secret_source: 'vault', configured: true },
    ]))
    const gmail = cardFor('gmail')
    fireEvent.change(within(gmail).getByLabelText('Client ID'), { target: { value: 'b-id' } })
    fireEvent.change(within(gmail).getByLabelText('Client secret'), { target: { value: 'b-secret' } })
    fireEvent.click(within(gmail).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(listMock).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(within(gmail).getByLabelText('Client ID')).toHaveValue('b-id'))

    expect(within(drive).getByLabelText('Client ID')).toHaveValue('a-id')
    fireEvent.click(within(drive).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(saveMock).toHaveBeenLastCalledWith('google-drive', { client_id: 'a-id', client_secret: 'a-secret' }))
  })

  it('saves only its own card when the toggle is off', async () => {
    await mount(googleCards())
    const gmail = cardFor(GOOGLE_ENTRY_SLUG)
    fireEvent.click(within(gmail).getByRole('switch', { name: /Also save on/ }))
    fireEvent.change(within(gmail).getByLabelText('Client ID'), { target: { value: 'g-id' } })
    fireEvent.click(within(gmail).getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(saveMock).toHaveBeenCalledTimes(1))
    expect(saveMock).toHaveBeenCalledWith('gmail', { client_id: 'g-id' })
  })

  it('keeps a writable secret for a Google product whose client ID is set by the environment', async () => {
    await mount([
      { ...googleCard('gmail', 48104), client_id: 'env-id', client_id_source: 'env', client_secret_set: false, client_secret_source: null, configured: false },
      googleCard('google-drive', 48103),
    ])
    const gmail = cardFor('gmail')
    expect(within(gmail).getByLabelText('Client ID')).toBeDisabled()
    expect(within(gmail).getByLabelText('Client ID')).toHaveValue('env-id')
    fireEvent.change(within(gmail).getByLabelText('Client secret'), { target: { value: 'g-secret' } })
    fireEvent.click(within(gmail).getByRole('button', { name: 'Save' }))
    // Only the writable half is sent; the environment's Client ID is never written.
    await waitFor(() => expect(saveMock).toHaveBeenCalledWith('gmail', { client_secret: 'g-secret' }))
    expect(saveMock).toHaveBeenCalledTimes(1)
  })

  it('never writes a partly environment-owned product from the shared Google save', async () => {
    await mount([
      googleCard('google-drive', 48103),
      { ...googleCard('gmail', 48104), client_id: 'env-id', client_id_source: 'env', client_secret_set: false, client_secret_source: null, configured: false },
    ])
    const google = cardFor(GOOGLE_ENTRY_SLUG)
    fireEvent.change(within(google).getByLabelText('Client ID'), { target: { value: 'd-id' } })
    fireEvent.change(within(google).getByLabelText('Client secret'), { target: { value: 'd-secret' } })
    fireEvent.click(within(google).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(saveMock).toHaveBeenCalledTimes(1))
    expect(saveMock).toHaveBeenCalledWith('google-drive', { client_id: 'd-id', client_secret: 'd-secret' })
  })

  it('gives the other cards this card\'s id when only the secret changed', async () => {
    await mount([
      { ...googleCard('gmail', 48104), client_id: 'g-id', client_id_source: 'config' },
      { ...googleCard('google-drive', 48103), client_id: 'g-id', client_id_source: 'config' },
    ])
    const gmail = cardFor(GOOGLE_ENTRY_SLUG)
    fireEvent.change(within(gmail).getByLabelText('Client secret'), { target: { value: 'g-secret' } })
    fireEvent.click(within(gmail).getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(saveMock).toHaveBeenCalledTimes(2))
    expect(saveMock.mock.calls).toEqual([['gmail', { client_id: 'g-id', client_secret: 'g-secret' }], ['google-drive', { client_secret: 'g-secret', client_id: 'g-id' }]])
  })

  it('sends Remove to every stored Google product and skips one the environment owns', async () => {
    const stored = { client_id: 'g-id', client_id_source: 'config' as const, client_secret_set: true, client_secret_source: 'vault' as const, configured: true }
    await mount([
      { ...googleCard('gmail', 48104), ...stored },
      { ...googleCard('google-drive', 48103), ...stored },
      { ...googleCard('google-docs', 48110), client_id: 'env-id', client_id_source: 'env', client_secret_source: 'env', client_secret_set: true, configured: true },
    ])
    const google = cardFor(GOOGLE_ENTRY_SLUG)
    fireEvent.click(within(google).getByRole('button', { name: /Remove the Google OAuth app/ }))
    fireEvent.click(within(google).getByRole('button', { name: 'Remove OAuth app' }))
    await waitFor(() => expect(deleteMock).toHaveBeenCalledTimes(2))
    expect(deleteMock.mock.calls.map(c => c[0])).toEqual(['gmail', 'google-drive'])
  })

  it('gives a Google product with a different stored client its own form, out of the shared Save and Remove', async () => {
    const stored = { client_id_source: 'config' as const, client_secret_set: true, client_secret_source: 'vault' as const, configured: true }
    await mount([
      { ...googleCard('gmail', 48104), ...stored, client_id: 'g-id' },
      { ...googleCard('google-drive', 48103), ...stored, client_id: 'other-id' },
    ])
    // Drive's own client is shown, on its own form.
    expect(within(cardFor('google-drive')).getByLabelText('Client ID')).toHaveValue('other-id')
    const google = cardFor(GOOGLE_ENTRY_SLUG)
    fireEvent.click(within(google).getByRole('button', { name: /Remove the Google OAuth app/ }))
    fireEvent.click(within(google).getByRole('button', { name: 'Remove OAuth app' }))
    await waitFor(() => expect(deleteMock).toHaveBeenCalledTimes(1))
    expect(deleteMock).toHaveBeenCalledWith('gmail')
  })

  it('keeps the shared Google Remove disabled while a shared Save is still writing', async () => {
    const stored = { client_id: 'g-id', client_id_source: 'config' as const, client_secret_set: true, client_secret_source: 'vault' as const, configured: true }
    let release: (v: unknown) => void = () => {}
    saveMock.mockImplementation(() => new Promise(resolve => { release = resolve }))
    // The edited product holds the shared id with no secret yet; the stored peer shares that id.
    await mount([{ ...googleCard('gmail', 48104), client_id: 'g-id', client_id_source: 'config' }, { ...googleCard('google-drive', 48103), ...stored }])
    const google = cardFor(GOOGLE_ENTRY_SLUG)
    expect(within(google).getByRole('button', { name: /Remove the Google OAuth app/ })).toBeEnabled()
    fireEvent.change(within(google).getByLabelText('Client ID'), { target: { value: 'new-id' } })
    fireEvent.change(within(google).getByLabelText('Client secret'), { target: { value: 'new-secret' } })
    fireEvent.click(within(google).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(saveMock).toHaveBeenCalledTimes(1))
    // The first PUT is held: the fan-out has not finished, so Remove must wait.
    expect(within(google).getByRole('button', { name: /Remove the Google OAuth app/ })).toBeDisabled()
    release({ ok: true, client: null })
  })

  it('offers no shared Google Remove when every Google client comes from the environment', async () => {
    const env = { client_id: 'env-id', client_id_source: 'env' as const, client_secret_set: true, client_secret_source: 'env' as const, configured: true }
    await mount(['gmail', 'google-drive', 'google-docs'].map((slug, i) => ({ ...googleCard(slug, 48104 + i), ...env })))
    expect(within(cardFor(GOOGLE_ENTRY_SLUG)).getByRole('button', { name: /Remove the Google OAuth app/ })).toBeDisabled()
  })

  it('leaves a partly environment-owned product to its own form when the shared Google app is removed', async () => {
    const stored = { client_id: 'g-id', client_id_source: 'config' as const, client_secret_set: true, client_secret_source: 'vault' as const, configured: true }
    await mount([
      { ...googleCard('google-drive', 48103), ...stored },
      { ...googleCard('google-docs', 48110), ...stored },
      // Gmail: the environment sets the Client ID, the secret is stored.
      { ...googleCard('gmail', 48104), client_id: 'env-id', client_id_source: 'env', client_secret_set: true, client_secret_source: 'vault', configured: true },
    ])
    const google = cardFor(GOOGLE_ENTRY_SLUG)
    fireEvent.click(within(google).getByRole('button', { name: /Remove the Google OAuth app/ }))
    fireEvent.click(within(google).getByRole('button', { name: 'Remove OAuth app' }))
    await waitFor(() => expect(deleteMock).toHaveBeenCalledTimes(2))
    expect(deleteMock.mock.calls.map(c => c[0])).toEqual(['google-drive', 'google-docs'])
  })

  it('edits the first Google product the environment does not own', async () => {
    await mount([
      { ...googleCard('gmail', 48104), client_id: 'env-id', client_id_source: 'env', client_secret_source: 'env', client_secret_set: true, configured: true },
      googleCard('google-drive', 48103),
    ])
    const google = cardFor(GOOGLE_ENTRY_SLUG)
    expect(within(google).getByLabelText('Client ID')).toBeEnabled()
    fireEvent.change(within(google).getByLabelText('Client ID'), { target: { value: 'g-id' } })
    fireEvent.click(within(google).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(saveMock).toHaveBeenCalledWith('google-drive', { client_id: 'g-id' }))
  })
})
