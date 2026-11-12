/**
 * The artifact card's remove button keeps keyboard focus while its removal runs.
 *
 * A disabled button leaves the tab order, so disabling the remove button for the
 * removal dropped the focus of the user who pressed it to <body>. It is
 * aria-disabled instead, and a second press is refused until the removal ends.
 * A failed removal leaves the card mounted, so the user stays on the button and
 * can press again.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, fireEvent, screen } from '@testing-library/react'
import { useState } from 'react'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'
import { LibraryMasonry, type GridEntry } from '../pages/ArtifactsPageGallery'
import type { Artifact } from '../types'

vi.mock('../api/client')
vi.mock('../utils/artifactPopout', () => ({ openPopout: vi.fn() }))

const art = {
  slug: 'cr-queue',
  name: 'cr queue',
  kind: 'markdown',
  version: 1,
  updated_at: '2026-01-01T00:00:00.000Z',
  created_at: '2026-01-01T00:00:00.000Z',
  tags: [],
  description: '',
  source: 'chat',
  pinned: false,
} as unknown as Artifact

const entries: GridEntry[] = [{ kind: 'local', key: art.slug, art }]
const onDelete = vi.fn()
let finishDelete: () => void = () => {}

// Mirrors ArtifactsPage: the press starts a removal, and deletingSlug names the
// artifact until it settles. A failed removal clears deletingSlug but leaves the
// card in the list, which is what this harness reproduces.
function Harness() {
  const [deletingSlug, setDeletingSlug] = useState<string | null>(null)
  return (
    <LibraryMasonry
      entries={entries}
      cols={1}
      widthRef={() => {}}
      scrollerRef={{ current: null }}
      fillPage={false}
      onOpen={vi.fn()}
      onDelete={(a) => {
        onDelete(a)
        setDeletingSlug(a.slug)
        finishDelete = () => setDeletingSlug(null)
      }}
      deletingSlug={deletingSlug}
      onTogglePin={vi.fn()}
      pinningSlug={null}
    />
  )
}

// Mirrors the parent when the user cancels the confirm dialog: onDelete runs but
// no removal starts, so deletingSlug stays null and the button is never busy.
function CancelHarness() {
  return (
    <LibraryMasonry
      entries={entries}
      cols={1}
      widthRef={() => {}}
      scrollerRef={{ current: null }}
      fillPage={false}
      onOpen={vi.fn()}
      onDelete={(a) => onDelete(a)}
      deletingSlug={null}
      onTogglePin={vi.fn()}
      pinningSlug={null}
    />
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(api).artifact = vi.fn().mockResolvedValue({ ...art, content: '# hi' })
})

describe('artifact card remove button while removing', () => {
  it('keeps focus and ignores a second press until a failed removal ends', () => {
    renderWithProviders(<Harness />)
    const remove = screen.getByRole('button', { name: 'Remove from artifacts library' })
    remove.focus()
    fireEvent.click(remove)
    fireEvent.click(remove)

    expect(onDelete).toHaveBeenCalledTimes(1)
    expect(remove).toHaveAttribute('aria-disabled', 'true')
    expect(remove).toHaveAttribute('aria-busy', 'true')
    expect(remove).not.toBeDisabled()
    expect(remove).toHaveFocus()

    // A failed removal: the card stays, deletingSlug clears, focus is kept.
    act(() => { finishDelete() })
    expect(remove).toHaveAttribute('aria-disabled', 'false')
    expect(remove).toHaveFocus()
    fireEvent.click(remove)
    expect(onDelete).toHaveBeenCalledTimes(2)
  })

  it('stays live after a cancel that starts no removal', () => {
    // No removal runs, so the button is never busy and a later press is not
    // refused: the guard keys on `deleting`, not a sticky claim ref.
    renderWithProviders(<CancelHarness />)
    const remove = screen.getByRole('button', { name: 'Remove from artifacts library' })
    fireEvent.click(remove)
    expect(remove).toHaveAttribute('aria-disabled', 'false')
    fireEvent.click(remove)
    expect(onDelete).toHaveBeenCalledTimes(2)
  })
})
