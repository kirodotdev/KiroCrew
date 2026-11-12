/**
 * The artifact card's star keeps keyboard focus while its save runs.
 *
 * A disabled button leaves the tab order, so disabling the star for the save
 * dropped the focus of the user who pressed it to <body>. It is aria-disabled
 * instead, and a second press is refused until the save ends.
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
const onTogglePin = vi.fn()
let finishPin: () => void = () => {}

// Mirrors ArtifactsPage: the press starts a save, and pinningSlug names the
// artifact until it settles.
function Harness() {
  const [pinningSlug, setPinningSlug] = useState<string | null>(null)
  return (
    <LibraryMasonry
      entries={entries}
      cols={1}
      widthRef={() => {}}
      scrollerRef={{ current: null }}
      fillPage={false}
      onOpen={vi.fn()}
      onDelete={vi.fn()}
      deletingSlug={null}
      onTogglePin={(a) => {
        onTogglePin(a)
        setPinningSlug(a.slug)
        finishPin = () => setPinningSlug(null)
      }}
      pinningSlug={pinningSlug}
    />
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(api).artifact = vi.fn().mockResolvedValue({ ...art, content: '# hi' })
})

describe('artifact card star while saving', () => {
  it('keeps focus on the star and ignores a second press until the save ends', () => {
    renderWithProviders(<Harness />)
    const star = screen.getByRole('button', { name: 'Star artifact' })
    star.focus()
    fireEvent.click(star)
    fireEvent.click(star)

    expect(onTogglePin).toHaveBeenCalledTimes(1)
    expect(star).toHaveAttribute('aria-disabled', 'true')
    expect(star).toHaveAttribute('aria-busy', 'true')
    expect(star).not.toBeDisabled()
    expect(star).toHaveFocus()

    act(() => { finishPin() })
    expect(star).toHaveAttribute('aria-disabled', 'false')
    fireEvent.click(star)
    expect(onTogglePin).toHaveBeenCalledTimes(2)
  })
})
