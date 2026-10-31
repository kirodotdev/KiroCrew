// The Browse pane's New folder control: a toggle beside the favourite star opens a name field;
// Create (or Enter) makes the folder through `api.createBrowseDir` in the directory on screen and
// then lists it, so Select commits the new folder. A refusal is told in the field's own notice
// with the server's code mapped to copy, and leaves the typed name in place.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, act, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ProjectPicker from '../components/ProjectPicker'
import { api, ApiError } from '../api/client'
import { i18nT } from '../i18n/t'

const rect = (top: number, left: number, width = 80, height = 24): DOMRect => ({
  top, left, width, height, bottom: top + height, right: left + width, x: left, y: top, toJSON: () => ({}),
} as DOMRect)

const HOME = { path: '/home/u', parent: '/home', dirs: [{ name: 'workplace', path: '/home/u/workplace' }] }

function listing(path?: string) {
  if (!path || path === '/home/u') return Promise.resolve(HOME)
  return Promise.resolve({ path, parent: path.slice(0, path.lastIndexOf('/')) || '/', dirs: [] })
}

function mount(onOpenChange = vi.fn(), onSelect = vi.fn()) {
  renderWithProviders(
    <ProjectPicker open onOpenChange={onOpenChange} anchorRect={rect(100, 50)} onSelect={onSelect} />,
  )
  return { onOpenChange, onSelect }
}

const refusal = (status: number, code: string) =>
  new ApiError(status, code, JSON.stringify({ error: code, code }))

async function openField() {
  fireEvent.click(await screen.findByTestId('pp-new-folder-toggle'))
  return screen.getByTestId('pp-new-folder-name') as HTMLInputElement
}

beforeEach(() => {
  Object.defineProperty(window, 'innerHeight', { value: 768, configurable: true })
  // No recent projects and no favourites -> the picker opens on the Browse tab.
  vi.spyOn(api, 'recentProjects').mockResolvedValue({ dirs: [] })
  vi.spyOn(api, 'favoriteProjects').mockResolvedValue({ dirs: [] })
  vi.spyOn(api, 'browseDirs').mockImplementation(listing)
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('ProjectPicker: New folder', () => {
  it('creates the folder in the directory on screen, then lists it ready to Select', async () => {
    const create = vi.spyOn(api, 'createBrowseDir').mockResolvedValue({ path: '/home/u/new-app', parent: '/home/u' })
    const { onSelect } = mount()
    await screen.findByText('workplace')

    const field = await openField()
    expect(screen.getByTestId('pp-new-folder-toggle').getAttribute('aria-expanded')).toBe('true')
    fireEvent.change(field, { target: { value: '  new-app  ' } })
    fireEvent.keyDown(field, { key: 'Enter' })

    await waitFor(() => expect(create).toHaveBeenCalledWith('/home/u', 'new-app'))
    await waitFor(() => expect(api.browseDirs).toHaveBeenCalledWith('/home/u/new-app'))
    // The pane is now ON the new folder: its path is in the field and the name field is gone.
    const path = screen.getByPlaceholderText('/path/to/project') as HTMLInputElement
    await waitFor(() => expect(path.value).toBe('/home/u/new-app/'))
    await waitFor(() => expect(screen.queryByTestId('pp-new-folder-name')).toBeNull())
    expect(screen.getByText(i18nT('components.projectPicker.no_subdirectories'))).toBeTruthy()

    fireEvent.mouseDown(screen.getByRole('button', { name: 'Select' }))
    expect(onSelect).toHaveBeenCalledWith('/home/u/new-app')
  })

  it('offers Create only once there is a name to send', async () => {
    const create = vi.spyOn(api, 'createBrowseDir')
    mount()
    const field = await openField()
    const button = screen.getByTestId('pp-new-folder-create') as HTMLButtonElement
    expect(button.disabled).toBe(true)
    fireEvent.change(field, { target: { value: '   ' } })
    expect(button.disabled).toBe(true)
    fireEvent.keyDown(field, { key: 'Enter' })
    fireEvent.change(field, { target: { value: 'x' } })
    expect(button.disabled).toBe(false)
    expect(create).not.toHaveBeenCalled()
  })

  it('tells a refusal in its own notice, keeps the name, and clears it on the next edit', async () => {
    vi.spyOn(api, 'createBrowseDir').mockRejectedValue(refusal(409, 'already_exists'))
    mount()
    const field = await openField()
    fireEvent.change(field, { target: { value: 'workplace' } })
    fireEvent.click(screen.getByTestId('pp-new-folder-create'))

    const notice = await screen.findByTestId('pp-new-folder-error')
    expect(notice.textContent).toContain(
      i18nT('components.projectPicker.new_folder_exists', { name: 'workplace', path: '/home/u' }),
    )
    // No hand-off: the field holds an unsent name.
    expect(screen.queryByRole('button', { name: /ask/i })).toBeNull()
    expect((screen.getByTestId('pp-new-folder-name') as HTMLInputElement).value).toBe('workplace')
    expect(api.browseDirs).not.toHaveBeenCalledWith('/home/u/workplace')

    fireEvent.change(screen.getByTestId('pp-new-folder-name'), { target: { value: 'workplace-2' } })
    expect(screen.queryByTestId('pp-new-folder-error')).toBeNull()
  })

  it.each([
    ['invalid_name', 400, 'new_folder_invalid_name'],
    ['name_too_long', 400, 'new_folder_too_long'],
    ['access_denied', 403, 'new_folder_protected'],
    ['permission_denied', 403, 'new_folder_no_permission'],
    ['not_a_directory', 400, 'new_folder_parent_gone'],
    ['not_found', 404, 'new_folder_parent_gone'],
    ['parent_changed', 409, 'new_folder_parent_changed'],
    ['create_failed', 500, 'new_folder_failed'],
    ['path_probe_busy', 503, 'new_folder_failed'],
  ])('names the %s refusal with its own copy', async (code, status, key) => {
    vi.spyOn(api, 'createBrowseDir').mockRejectedValue(refusal(status, code))
    mount()
    const field = await openField()
    fireEvent.change(field, { target: { value: 'x' } })
    fireEvent.keyDown(field, { key: 'Enter' })
    const notice = await screen.findByTestId('pp-new-folder-error')
    expect(notice.textContent).toContain(i18nT(`components.projectPicker.${key}`, { name: 'x', path: '/home/u' }))
  })

  it('takes a failure that is not a coded refusal as the generic copy', async () => {
    vi.spyOn(api, 'createBrowseDir').mockRejectedValue(new TypeError('Failed to fetch'))
    mount()
    const field = await openField()
    fireEvent.change(field, { target: { value: 'x' } })
    fireEvent.keyDown(field, { key: 'Enter' })
    const notice = await screen.findByTestId('pp-new-folder-error')
    expect(notice.textContent).toContain(i18nT('components.projectPicker.new_folder_failed', { name: 'x' }))
  })

  it('closes only the field on Escape and hands focus back to the path field', async () => {
    const { onOpenChange } = mount()
    const field = await openField()
    fireEvent.change(field, { target: { value: 'draft' } })
    fireEvent.keyDown(field, { key: 'Escape' })

    expect(screen.queryByTestId('pp-new-folder-name')).toBeNull()
    expect(onOpenChange).not.toHaveBeenCalled()
    const path = screen.getByPlaceholderText('/path/to/project')
    await waitFor(() => expect(document.activeElement).toBe(path))
  })

  it('closes the field when the pane moves to another folder', async () => {
    mount()
    await screen.findByText('workplace')
    await openField()
    fireEvent.click(screen.getByText('workplace'))
    await waitFor(() => expect(api.browseDirs).toHaveBeenCalledWith('/home/u/workplace'))
    await waitFor(() => expect(screen.queryByTestId('pp-new-folder-name')).toBeNull())
    expect(screen.getByTestId('pp-new-folder-toggle').getAttribute('aria-expanded')).toBe('false')
  })

  it('does not pull the pane to a folder whose create answered after the field closed', async () => {
    let answer: (v: { path: string; parent: string }) => void = () => {}
    vi.spyOn(api, 'createBrowseDir').mockImplementation(() => new Promise(resolve => { answer = resolve }))
    mount()
    const field = await openField()
    fireEvent.change(field, { target: { value: 'slow' } })
    fireEvent.keyDown(field, { key: 'Enter' })
    await waitFor(() => expect(screen.getByTestId('pp-new-folder-create').textContent)
      .toBe(i18nT('components.projectPicker.new_folder_creating')))

    fireEvent.click(screen.getByTestId('pp-new-folder-cancel'))
    await act(async () => { answer({ path: '/home/u/slow', parent: '/home/u' }) })

    expect(api.browseDirs).not.toHaveBeenCalledWith('/home/u/slow')
    expect((screen.getByPlaceholderText('/path/to/project') as HTMLInputElement).value).toBe('/home/u/')
  })

  it('does not pull the pane back when a create answers after the user moved to another folder', async () => {
    let answer: (v: { path: string; parent: string }) => void = () => {}
    vi.spyOn(api, 'createBrowseDir').mockImplementation(() => new Promise(resolve => { answer = resolve }))
    let listed: (v: { path: string; parent: string; dirs: { name: string; path: string }[] }) => void = () => {}
    vi.mocked(api.browseDirs).mockImplementation(p =>
      p === '/home/u/workplace' ? new Promise(resolve => { listed = resolve }) : listing(p))
    mount()
    await screen.findByText('workplace')
    const field = await openField()
    fireEvent.change(field, { target: { value: 'slow' } })
    fireEvent.keyDown(field, { key: 'Enter' })
    await waitFor(() => expect(screen.getByTestId('pp-new-folder-create').textContent)
      .toBe(i18nT('components.projectPicker.new_folder_creating')))

    // The drill closes the field at once, before its listing answers.
    fireEvent.click(screen.getByText('workplace'))
    expect(screen.queryByTestId('pp-new-folder-name')).toBeNull()
    await act(async () => { answer({ path: '/home/u/slow', parent: '/home/u' }) })
    await act(async () => { listed({ path: '/home/u/workplace', parent: '/home/u', dirs: [] }) })

    expect(api.browseDirs).not.toHaveBeenCalledWith('/home/u/slow')
    const path = screen.getByPlaceholderText('/path/to/project') as HTMLInputElement
    await waitFor(() => expect(path.value).toBe('/home/u/workplace/'))
  })

  it('opens the field from the path field with Shift+Enter, the keyboard path to the toggle', async () => {
    mount()
    await screen.findByText('workplace')
    const path = screen.getByPlaceholderText('/path/to/project')
    fireEvent.keyDown(path, { key: 'Enter', shiftKey: true })

    const field = await screen.findByTestId('pp-new-folder-name')
    await waitFor(() => expect(document.activeElement).toBe(field))
    // Shift+Enter opens the field instead of drilling into the highlighted folder.
    expect(api.browseDirs).not.toHaveBeenCalledWith('/home/u/workplace')
    expect(screen.getByTestId('pp-new-folder-toggle').getAttribute('title')).toContain('Shift+Enter')
    expect(screen.getByTestId('pp-new-folder-toggle').getAttribute('aria-keyshortcuts')).toBe('Shift+Enter')

    // Plain Enter still drills.
    fireEvent.keyDown(field, { key: 'Escape' })
    fireEvent.keyDown(path, { key: 'Enter' })
    await waitFor(() => expect(api.browseDirs).toHaveBeenCalledWith('/home/u/workplace'))
  })

  it('keeps the favourite row to two controls', async () => {
    mount()
    const toggle = await screen.findByTestId('pp-new-folder-toggle')
    const row = toggle.parentElement!
    expect(row.querySelectorAll('button')).toHaveLength(2)
    expect(row.contains(screen.getByTestId('pp-browse-star'))).toBe(true)
  })
})
