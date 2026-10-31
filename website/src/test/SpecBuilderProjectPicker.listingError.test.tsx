/**
 * The spec builder's folder picker says when a listing failed.
 *
 * A refused `/browse` read left `dirs` empty, so the panel claimed "No subfolders
 * here" about a folder it never saw into. The empty message stays for a listing
 * that succeeded with no subfolders.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const browse = vi.fn()
vi.mock('../apps/spec-builder/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../apps/spec-builder/api')>()
  return { ...actual, specApi: { ...actual.specApi, browse: (...a: unknown[]) => browse(...a) } }
})

import ProjectPicker from '../apps/spec-builder/components/ProjectPicker'

const EMPTY = /No subfolders here/

function renderPicker() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <ProjectPicker value="" onChange={() => {}} />
    </QueryClientProvider>,
  )
}

describe('spec-builder ProjectPicker failed listing', () => {
  beforeEach(() => {
    browse.mockReset()
  })

  it('shows the error with a retry instead of "No subfolders here"', async () => {
    browse.mockRejectedValue(new Error('403 path outside the allowed roots'))
    renderPicker()
    fireEvent.click(screen.getByRole('button', { name: 'Choose a project folder' }))

    const notice = await screen.findByTestId('spec-builder-picker-error')
    expect(notice.textContent).toContain('Could not list this folder')
    expect(notice.textContent).toContain('403 path outside the allowed roots')
    expect(screen.queryByText(EMPTY)).toBeNull()

    browse.mockResolvedValue({ path: '/zz/project', parent: '/zz', dirs: [], recents: [] })
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await screen.findByText(EMPTY)).toBeInTheDocument()
    expect(screen.queryByTestId('spec-builder-picker-error')).toBeNull()
  })

  it('keeps "No subfolders here" for a listing that succeeded empty', async () => {
    browse.mockResolvedValue({ path: '/zz/project', parent: '/zz', dirs: [], recents: [] })
    renderPicker()
    fireEvent.click(screen.getByRole('button', { name: 'Choose a project folder' }))
    expect(await screen.findByText(EMPTY)).toBeInTheDocument()
    expect(screen.queryByTestId('spec-builder-picker-error')).toBeNull()
  })
})
