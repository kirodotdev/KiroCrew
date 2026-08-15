import { screen, fireEvent, waitFor, act } from '@testing-library/react'
import { renderWithProviders } from '../test/helpers'
import UpdateModal from './UpdateModal'
import { i18nT } from '../i18n/t'

type UpdateState = {
  state: 'checking' | 'available' | 'downloading' | 'downloaded' | 'not-available' | 'error'
  version?: string
  notes?: string
}

const install = vi.fn<() => Promise<unknown>>()

/**
 * React Query flushes cache notifications on a microtask, so seeding
 * ['update-state'] only reaches the component after an async act() tick --
 * a synchronous assertion right after setQueryData always sees no dialog.
 */
async function mount(initial?: UpdateState) {
  const rendered = renderWithProviders(<UpdateModal />)
  const push = async (next: UpdateState) => {
    await act(async () => { rendered.queryClient.setQueryData(['update-state'], next) })
  }
  if (initial) await push(initial)
  return { ...rendered, push }
}

const dialog = () => screen.queryByRole('dialog')
const byName = (key: string) => screen.getByRole('button', { name: i18nT(key) })
const downloaded: UpdateState = { state: 'downloaded', version: '9.9.9' }

describe('UpdateModal', () => {
  beforeEach(() => {
    install.mockReset()
    install.mockResolvedValue(undefined)
    ;(window as unknown as { updateAPI?: { install: () => Promise<unknown> } }).updateAPI = {
      install,
    }
  })

  afterEach(() => {
    delete (window as unknown as { updateAPI?: unknown }).updateAPI
  })

  it('renders nothing without an update state', async () => {
    const { container } = await mount()
    expect(container.firstChild).toBeNull()
  })

  it('renders nothing while the download is still in flight', async () => {
    const { container } = await mount({ state: 'downloading', version: '9.9.9' })
    expect(container.firstChild).toBeNull()
  })

  it('shows the version and the trimmed release notes once downloaded', async () => {
    await mount({ ...downloaded, notes: '  zzq changelog  ' })
    expect(dialog()).toBeInTheDocument()
    expect(screen.getByText('9.9.9')).toBeInTheDocument()
    expect(screen.getByText('zzq changelog')).toBeInTheDocument()
  })

  it('omits the notes paragraph when the notes are blank', async () => {
    await mount({ ...downloaded, notes: '   ' })
    expect(dialog()).toBeInTheDocument()
    expect(screen.queryByText(/zzq/)).not.toBeInTheDocument()
  })

  it('the header close button dismisses it', async () => {
    await mount(downloaded)
    fireEvent.click(byName('components.updateModal.dismiss'))
    expect(dialog()).not.toBeInTheDocument()
  })

  it('the Later button dismisses it', async () => {
    await mount(downloaded)
    fireEvent.click(byName('components.updateModal.later'))
    expect(dialog()).not.toBeInTheDocument()
  })

  it('a click on the backdrop itself dismisses, a click inside does not', async () => {
    await mount(downloaded)
    const backdrop = byName('components.updateModal.dismiss_update_dialog')
    fireEvent.click(dialog()!)
    expect(dialog()).toBeInTheDocument()
    fireEvent.click(backdrop)
    expect(dialog()).not.toBeInTheDocument()
  })

  it('Enter on the backdrop dismisses, a bubbled key press does not', async () => {
    await mount(downloaded)
    const backdrop = byName('components.updateModal.dismiss_update_dialog')
    fireEvent.keyDown(dialog()!, { key: 'Enter' })
    expect(dialog()).toBeInTheDocument()
    fireEvent.keyDown(backdrop, { key: ' ' })
    expect(dialog()).not.toBeInTheDocument()
  })

  it('Escape dismisses it', async () => {
    await mount(downloaded)
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(dialog()).not.toBeInTheDocument()
  })

  it('ignores an unrelated key', async () => {
    await mount(downloaded)
    fireEvent.keyDown(window, { key: 'a' })
    expect(dialog()).toBeInTheDocument()
  })

  it('re-opens for a newer version after a dismissal', async () => {
    const { push } = await mount(downloaded)
    fireEvent.click(byName('components.updateModal.later'))
    expect(dialog()).not.toBeInTheDocument()
    await push({ state: 'downloaded', version: '9.9.10' })
    // The re-open runs through a render-phase setState, so it lands one
    // commit later than the cache write itself.
    expect(await screen.findByRole('dialog')).toBeInTheDocument()
    expect(screen.getByText('9.9.10')).toBeInTheDocument()
  })

  it('installs and stays disabled after the dispatch resolves', async () => {
    await mount({ state: 'downloaded', version: '9.9.11' })
    fireEvent.click(
      await screen.findByRole('button', { name: i18nT('components.updateModal.restart_update') }),
    )
    await waitFor(() => expect(install).toHaveBeenCalledTimes(1))

    const restarting = await screen.findByRole('button', {
      name: i18nT('components.updateModal.restarting'),
    })
    expect(restarting).toBeDisabled()
    expect(byName('components.updateModal.later')).toBeDisabled()

    // Escape and the Later button must not dismiss while the install runs.
    fireEvent.keyDown(window, { key: 'Escape' })
    fireEvent.click(byName('components.updateModal.later'))
    expect(dialog()).toBeInTheDocument()
  })
})
