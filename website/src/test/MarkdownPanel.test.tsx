import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { OverflowMenu, breadcrumbSegments } from '../components/MarkdownPanel'
import { api } from '../api/client'

vi.mock('../api/client', () => ({
  api: {
    artifacts: vi.fn(),
    artifact: vi.fn(),
    createArtifact: vi.fn(),
  },
}))

const writeText = vi.fn()
const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
const wrapper = ({ children }: { children: React.ReactNode }) => (
  <MemoryRouter>
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  </MemoryRouter>
)

beforeEach(() => {
  writeText.mockReset()
  queryClient.clear()
  // happy-dom's navigator.clipboard is getter-only; defineProperty replaces it.
  Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
  // Default: no existing artifact for any path. Tests can override.
  vi.mocked(api).artifacts = vi.fn().mockResolvedValue({ artifacts: [] })
  vi.mocked(api).createArtifact = vi.fn().mockResolvedValue({ slug: 'test-doc-md', version: 1 })
})

function openMenu() {
  render(<OverflowMenu filePath="/tmp/hello.txt" content={'line one\nline two\n'} revealOrCopy={vi.fn()} />, { wrapper })
  fireEvent.click(screen.getAllByRole('button')[0])
}

describe('MarkdownPanel OverflowMenu', () => {
  it('exposes both Copy path and Copy content entries', () => {
    openMenu()
    expect(screen.getByText('Copy path')).toBeInTheDocument()
    expect(screen.getByText('Copy content')).toBeInTheDocument()
  })

  it('Copy path writes the filePath to the clipboard', () => {
    openMenu()
    fireEvent.click(screen.getByText('Copy path'))
    expect(writeText).toHaveBeenCalledExactlyOnceWith('/tmp/hello.txt')
  })

  it('Copy content writes the raw file content to the clipboard', () => {
    openMenu()
    fireEvent.click(screen.getByText('Copy content'))
    expect(writeText).toHaveBeenCalledExactlyOnceWith('line one\nline two\n')
  })

  it('closes the overflow menu after Copy content is clicked', () => {
    openMenu()
    expect(screen.getByText('Copy content')).toBeInTheDocument()
    fireEvent.click(screen.getByText('Copy content'))
    expect(screen.queryByText('Copy content')).not.toBeInTheDocument()
  })

  it('Copy content copies an empty string for an empty file without throwing', () => {
    render(<OverflowMenu filePath="/tmp/empty.txt" content="" revealOrCopy={vi.fn()} />, { wrapper })
    fireEvent.click(screen.getAllByRole('button')[0])
    fireEvent.click(screen.getByText('Copy content'))
    expect(writeText).toHaveBeenCalledExactlyOnceWith('')
  })
})

describe('breadcrumbSegments', () => {
  it('shows the last three segments with the file last and non-navigable', () => {
    const crumbs = breadcrumbSegments('/home/user/project/src/app.ts')
    expect(crumbs.map(c => c.seg)).toEqual(['project', 'src', 'app.ts'])
    expect(crumbs.map(c => c.isFile)).toEqual([false, false, true])
  })

  it('gives each directory segment its full absolute ancestor path', () => {
    const crumbs = breadcrumbSegments('/home/user/project/src/app.ts')
    // Even though only the tail is shown, a clicked directory opens its true
    // absolute path — not a relative fragment of the visible segments.
    expect(crumbs[0].path).toBe('/home/user/project')
    expect(crumbs[1].path).toBe('/home/user/project/src')
    expect(crumbs[2].path).toBe('/home/user/project/src/app.ts')
  })

  it('preserves a leading slash for a shallow absolute path', () => {
    const crumbs = breadcrumbSegments('/tmp/notes.md')
    expect(crumbs.map(c => c.path)).toEqual(['/tmp', '/tmp/notes.md'])
    expect(crumbs.map(c => c.isFile)).toEqual([false, true])
  })

  it('handles a relative path without inventing a leading slash', () => {
    const crumbs = breadcrumbSegments('docs/guide/intro.md')
    expect(crumbs.map(c => c.path)).toEqual(['docs', 'docs/guide', 'docs/guide/intro.md'])
  })

  it('handles a bare filename as a single file segment', () => {
    const crumbs = breadcrumbSegments('/README.md')
    expect(crumbs).toEqual([{ seg: 'README.md', path: '/README.md', isFile: true }])
  })
})
