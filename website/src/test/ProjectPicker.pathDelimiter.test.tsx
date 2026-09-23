// Regression for #1196: after browsing/drilling into a directory, the browse
// combobox input should carry a trailing path delimiter so the user can start
// typing the next path segment immediately.
import { screen, waitFor, fireEvent } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import ProjectPicker from '../components/ProjectPicker'

const browseDirs = vi.fn()
const recentProjects = vi.fn()

vi.mock('../api/client', () => ({
  api: {
    browseDirs: (path?: string) => browseDirs(path),
    recentProjects: () => recentProjects(),
  },
}))

const anchorRect = {
  top: 100, bottom: 130, left: 100, right: 200, width: 100, height: 30, x: 100, y: 100,
  toJSON() {},
} as DOMRect

function open() {
  renderWithProviders(
    <ProjectPicker open onOpenChange={() => {}} anchorRect={anchorRect} onSelect={() => {}} />,
  )
}

describe('ProjectPicker path delimiter (#1196)', () => {
  beforeEach(() => {
    browseDirs.mockReset()
    recentProjects.mockReset()
    // No recent projects -> the picker opens straight on the Browse tab.
    recentProjects.mockResolvedValue({ dirs: [] })
  })

  it('appends a trailing slash to the browsed directory path', async () => {
    browseDirs.mockResolvedValue({
      path: '/home/user/project',
      parent: '/home/user',
      dirs: [{ name: 'src', path: '/home/user/project/src' }],
    })
    open()
    const input = await screen.findByRole('combobox')
    await waitFor(() => {
      expect((input as HTMLInputElement).value).toBe('/home/user/project/')
    })
  })

  it('does not double the slash for the filesystem root', async () => {
    browseDirs.mockResolvedValue({ path: '/', parent: '/', dirs: [] })
    open()
    const input = await screen.findByRole('combobox')
    await waitFor(() => {
      expect((input as HTMLInputElement).value).toBe('/')
    })
  })

  it('drilling into a subdirectory re-appends the delimiter', async () => {
    browseDirs.mockResolvedValueOnce({
      path: '/home/user/project',
      parent: '/home/user',
      dirs: [{ name: 'src', path: '/home/user/project/src' }],
    })
    open()
    const input = await screen.findByRole('combobox')
    await waitFor(() => expect((input as HTMLInputElement).value).toBe('/home/user/project/'))

    browseDirs.mockResolvedValueOnce({
      path: '/home/user/project/src',
      parent: '/home/user/project',
      dirs: [],
    })
    // Click the "src" subdir row to drill in.
    fireEvent.click(screen.getByText('src'))
    await waitFor(() =>
      expect((input as HTMLInputElement).value).toBe('/home/user/project/src/'),
    )
  })

  it('shows the delimiter in the input but commits a clean (unslashed) path', async () => {
    const onSelect = vi.fn()
    browseDirs.mockResolvedValue({
      path: '/home/user/project',
      parent: '/home/user',
      dirs: [{ name: 'src', path: '/home/user/project/src' }],
    })
    renderWithProviders(
      <ProjectPicker open onOpenChange={() => {}} anchorRect={anchorRect} onSelect={onSelect} />,
    )
    const input = await screen.findByRole('combobox')
    await waitFor(() => expect((input as HTMLInputElement).value).toBe('/home/user/project/'))
    // Cmd+Enter commits the current directory — the trailing slash shown for
    // typing must NOT ride into the committed project path.
    fireEvent.keyDown(input, { key: 'Enter', metaKey: true })
    expect(onSelect).toHaveBeenCalledWith('/home/user/project')
  })

  it('uses the native separator for a Windows path (no mixed C:\\Users\\me/)', async () => {
    browseDirs.mockResolvedValue({
      path: 'C:\\Users\\me',
      parent: 'C:\\Users',
      dirs: [{ name: 'proj', path: 'C:\\Users\\me\\proj' }],
    })
    open()
    const input = await screen.findByRole('combobox')
    await waitFor(() => expect((input as HTMLInputElement).value).toBe('C:\\Users\\me\\'))
  })

  it('preserves a Windows drive root on commit (C:\\ must not become C:)', async () => {
    const onSelect = vi.fn()
    browseDirs.mockResolvedValue({ path: 'C:\\', parent: 'C:\\', dirs: [] })
    renderWithProviders(
      <ProjectPicker open onOpenChange={() => {}} anchorRect={anchorRect} onSelect={onSelect} />,
    )
    const input = await screen.findByRole('combobox')
    await waitFor(() => expect((input as HTMLInputElement).value).toBe('C:\\'))
    fireEvent.keyDown(input, { key: 'Enter', metaKey: true })
    expect(onSelect).toHaveBeenCalledWith('C:\\')
  })

  it('strips the native trailing separator on a Windows commit', async () => {
    const onSelect = vi.fn()
    browseDirs.mockResolvedValue({
      path: 'C:\\Users\\me\\proj',
      parent: 'C:\\Users\\me',
      dirs: [],
    })
    renderWithProviders(
      <ProjectPicker open onOpenChange={() => {}} anchorRect={anchorRect} onSelect={onSelect} />,
    )
    const input = await screen.findByRole('combobox')
    await waitFor(() => expect((input as HTMLInputElement).value).toBe('C:\\Users\\me\\proj\\'))
    fireEvent.keyDown(input, { key: 'Enter', metaKey: true })
    expect(onSelect).toHaveBeenCalledWith('C:\\Users\\me\\proj')
  })

  it('preserves a literal trailing backslash in a POSIX directory name on commit', async () => {
    // On POSIX, `\` is a legal filename char — it must NOT be treated as a
    // separator (GPT 5.6). A dir literally ending in `\` keeps it; only the
    // appended `/` is stripped at commit.
    const onSelect = vi.fn()
    browseDirs.mockResolvedValue({ path: '/home/weird\\', parent: '/home', dirs: [] })
    renderWithProviders(
      <ProjectPicker open onOpenChange={() => {}} anchorRect={anchorRect} onSelect={onSelect} />,
    )
    const input = await screen.findByRole('combobox')
    // POSIX path -> `/` appended (not `\`), so the literal trailing `\` survives.
    await waitFor(() => expect((input as HTMLInputElement).value).toBe('/home/weird\\/'))
    fireEvent.keyDown(input, { key: 'Enter', metaKey: true })
    expect(onSelect).toHaveBeenCalledWith('/home/weird\\')
  })
})
