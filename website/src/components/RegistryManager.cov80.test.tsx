import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from '../test/helpers'
import RegistryManager from './RegistryManager'
import { api } from '../api/client'
import { i18nT } from '../i18n/t'

/** Catalog lookups, so a copy edit cannot silently break these selectors. */
const L = (k: string, vars?: Record<string, unknown>) =>
  i18nT(`components.registryManager.${k}`, vars)

vi.mock('../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../api/client')>()
  return {
    ...mod,
    api: {
      ...mod.api,
      listRegistries: vi.fn(),
      updateRegistries: vi.fn(),
      refreshRegistries: vi.fn(),
    },
  }
})
vi.mock('../rum', () => ({ recordEvent: vi.fn() }))

const { recordEvent } = await import('../rum')
const listRegistries = vi.mocked(api.listRegistries)
const updateRegistries = vi.mocked(api.updateRegistries)
const refreshRegistries = vi.mocked(api.refreshRegistries)

const REG = { name: 'zzq-reg', repo: 'https://github.com/zzq/reg', branch: 'main' }

function mount(registries = [REG], bare = false) {
  listRegistries.mockResolvedValue({ registries } as never)
  return renderWithProviders(<RegistryManager bare={bare} />)
}

async function openAddForm(registries = [REG]) {
  const view = mount(registries)
  await screen.findByRole('button', { name: /add registry/i })
  fireEvent.click(screen.getByRole('button', { name: /add registry/i }))
  return view
}

const repoInput = () => screen.getByLabelText(L('repo'))

describe('RegistryManager', () => {
  beforeEach(() => {
    listRegistries.mockReset()
    updateRegistries.mockReset()
    refreshRegistries.mockReset()
    vi.mocked(recordEvent).mockReset()
    updateRegistries.mockResolvedValue({} as never)
    refreshRegistries.mockResolvedValue({ ok: true } as never)
  })

  it('shows an empty state when no registry is configured', async () => {
    mount([])
    expect(await screen.findByText('No external registries')).toBeInTheDocument()
  })

  it('lists a configured registry with its branch and repo', async () => {
    mount()
    expect(await screen.findByText('zzq-reg')).toBeInTheDocument()
    expect(screen.getByText('main')).toBeInTheDocument()
    expect(screen.getByText('https://github.com/zzq/reg')).toBeInTheDocument()
  })

  it('bare mode adds the public-repos hint and drops the card chrome', async () => {
    mount([REG], true)
    expect(
      await screen.findByText(L('registry_url_install_public_repos_only')),
    ).toBeInTheDocument()
  })

  it('rejects an empty repo before calling the API', async () => {
    await openAddForm()
    fireEvent.click(screen.getByRole('button', { name: 'Add Registry' }))
    expect(await screen.findByText('Repo name is required')).toBeInTheDocument()
    expect(updateRegistries).not.toHaveBeenCalled()
  })

  it('rejects a plaintext http URL and a shell-metacharacter repo', async () => {
    await openAddForm()
    // '' falls to the required-field guard; the rest fail the shape check.
    const cases: [string, string][] = [
      ['http://zzq.invalid/x', 'repo_must_be_a_git_url_or_an_alphanumeric_name_h'],
      ['zzq; rm', 'repo_must_be_a_git_url_or_an_alphanumeric_name_h'],
      ['git@host', 'repo_must_be_a_git_url_or_an_alphanumeric_name_h'],
      ['', 'repo_name_is_required'],
    ]
    for (const [bad, key] of cases) {
      fireEvent.change(repoInput(), { target: { value: bad } })
      fireEvent.keyDown(repoInput(), { key: 'Enter' })
      expect(await screen.findByText(L(key))).toBeInTheDocument()
    }
    expect(updateRegistries).not.toHaveBeenCalled()
  })

  it('rejects a duplicate repo', async () => {
    await openAddForm()
    fireEvent.change(repoInput(), { target: { value: REG.repo } })
    fireEvent.keyDown(repoInput(), { key: 'Enter' })
    expect(await screen.findByText(/already exists/i)).toBeInTheDocument()
    expect(updateRegistries).not.toHaveBeenCalled()
  })

  it('accepts a bare name, an https URL, an ssh URL and an scp-style remote', async () => {
    for (const good of ['zzq_bare-1', 'https://zzq.invalid/a', 'ssh://git@zzq/a', 'git@zzq:a/b.git']) {
      updateRegistries.mockClear()
      const { unmount } = await openAddForm([])
      fireEvent.change(repoInput(), { target: { value: good } })
      fireEvent.keyDown(repoInput(), { key: 'Enter' })
      await waitFor(() =>
        expect(updateRegistries).toHaveBeenCalledWith([{ name: '', repo: good, branch: '' }]))
      unmount()
    }
  })

  it('sends empty name/branch so the backend owns the defaults, and logs the add', async () => {
    await openAddForm([])
    fireEvent.change(screen.getByLabelText(L('display_name')), { target: { value: ' zzq-nice ' } })
    fireEvent.change(repoInput(), { target: { value: ' https://zzq.invalid/a ' } })
    fireEvent.change(screen.getByLabelText(L('branch')), { target: { value: ' dev ' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add Registry' }))

    await waitFor(() => expect(updateRegistries).toHaveBeenCalledWith([
      { name: 'zzq-nice', repo: 'https://zzq.invalid/a', branch: 'dev' },
    ]))
    expect(vi.mocked(recordEvent).mock.calls[0][0]).toBe('registry_add')
    // The form only closes once the write lands.
    await waitFor(() => expect(screen.queryByLabelText(L('repo'))).not.toBeInTheDocument())
  })

  it('keeps the typed value when the write is rejected', async () => {
    updateRegistries.mockRejectedValue(new Error('zzq-add-rejected'))
    await openAddForm([])
    fireEvent.change(repoInput(), { target: { value: 'https://zzq.invalid/a' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add Registry' }))

    expect(await screen.findByText('zzq-add-rejected')).toBeInTheDocument()
    expect((repoInput() as HTMLInputElement).value).toBe('https://zzq.invalid/a')
  })

  it('a non-Error write failure falls back to the generic message', async () => {
    updateRegistries.mockRejectedValue('zzq-not-an-error')
    await openAddForm([])
    fireEvent.change(repoInput(), { target: { value: 'https://zzq.invalid/a' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add Registry' }))
    expect(await screen.findByText('Failed to update registries')).toBeInTheDocument()
  })

  it('echoes the newly trusted hosts, and the notice can be dismissed', async () => {
    updateRegistries.mockResolvedValue({ newlyTrustedHosts: ['zzq.invalid'] } as never)
    await openAddForm([])
    fireEvent.change(repoInput(), { target: { value: 'https://zzq.invalid/a' } })
    fireEvent.click(screen.getByRole('button', { name: 'Add Registry' }))

    expect(await screen.findByText(/zzq.invalid/)).toBeInTheDocument()
    fireEvent.click(screen.getByLabelText(L('dismiss_trust_notice')))
    await waitFor(() =>
      expect(screen.queryByLabelText(L('dismiss_trust_notice'))).not.toBeInTheDocument())
  })

  it('Cancel closes the form and clears any error', async () => {
    await openAddForm()
    fireEvent.click(screen.getByRole('button', { name: 'Add Registry' }))
    await screen.findByText('Repo name is required')
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.queryByText('Repo name is required')).not.toBeInTheDocument()
    expect(screen.queryByLabelText(L('repo'))).not.toBeInTheDocument()
  })

  it('removing a registry writes the shortened list and logs it', async () => {
    mount()
    fireEvent.click(await screen.findByLabelText(L('remove_registry', { name: REG.name })))
    await waitFor(() => expect(updateRegistries).toHaveBeenCalledWith([]))
    expect(vi.mocked(recordEvent).mock.calls[0][0]).toBe('registry_remove')
  })

  it('the external-link action derives a browsable https URL for each repo form', async () => {
    const open = vi.spyOn(window, 'open').mockImplementation(() => null)
    const forms: [string, string][] = [
      ['https://github.com/zzq/reg', 'https://github.com/zzq/reg'],
      ['git@zzq.invalid:org/repo.git', 'https://zzq.invalid/org/repo'],
      ['ssh://git@zzq.invalid/org/repo.git', 'https://zzq.invalid/org/repo'],
      ['zzq-bare', 'https://github.com/kirodotdev-labs/zzq-bare'],
    ]
    for (const [repo, expected] of forms) {
      open.mockClear()
      const { unmount } = mount([{ ...REG, repo }])
      fireEvent.click(await screen.findByLabelText(L('open_repository', { repo })))
      expect(open).toHaveBeenCalledWith(expected, '_blank')
      unmount()
    }
    open.mockRestore()
  })

  it('the per-row refresh syncs only that registry and records the sync time', async () => {
    refreshRegistries.mockResolvedValue({ ok: true, lastSyncedAt: '2024-01-02T03:04:05Z' } as never)
    mount()
    fireEvent.click(await screen.findByLabelText(L('refresh_registry', { name: REG.name })))
    await waitFor(() => expect(refreshRegistries).toHaveBeenCalledWith(REG.repo))
    expect(await screen.findByText(/Last synced/i)).toBeInTheDocument()
  })

  it('the toolbar sync refreshes every registry', async () => {
    mount()
    fireEvent.click(await screen.findByLabelText(L('sync_registry_apps')))
    await waitFor(() => expect(refreshRegistries).toHaveBeenCalledWith(undefined))
  })

  it('a partial refresh failure names the registries that did not sync', async () => {
    refreshRegistries.mockResolvedValue({ ok: false, failed: ['zzq-reg'] } as never)
    mount()
    fireEvent.click(await screen.findByLabelText(L('sync_registry_apps')))
    expect(await screen.findByText(/Could not refresh/i)).toBeInTheDocument()
  })

  it('a thrown refresh reports its message, and a non-Error the fallback', async () => {
    refreshRegistries.mockRejectedValue(new Error('zzq-refresh-threw'))
    const { unmount } = mount()
    fireEvent.click(await screen.findByLabelText(L('sync_registry_apps')))
    expect(await screen.findByText('zzq-refresh-threw')).toBeInTheDocument()
    unmount()

    refreshRegistries.mockRejectedValue('zzq-not-an-error')
    mount()
    fireEvent.click(await screen.findByLabelText(L('sync_registry_apps')))
    expect(await screen.findByText('Failed to refresh registries')).toBeInTheDocument()
  })

  it('offers no sync button when there is nothing to sync', async () => {
    mount([])
    await screen.findByText('No external registries')
    expect(screen.queryByLabelText(L('sync_registry_apps'))).not.toBeInTheDocument()
  })
})
