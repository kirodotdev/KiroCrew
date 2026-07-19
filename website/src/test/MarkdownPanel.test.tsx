import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { OverflowMenu } from '../components/MarkdownPanel'
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
  Object.assign(navigator, { clipboard: { writeText } })
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

  // ── Phase 6 (Mesh-1654): Add to artifacts (no-modal flow) ─────────────

  // ── Coverage push for new hook paths ────────────────────────────────────

})
