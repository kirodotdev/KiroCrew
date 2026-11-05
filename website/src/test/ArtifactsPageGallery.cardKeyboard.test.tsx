/**
 * Keyboard reach of an artifact card's own buttons (#26289).
 *
 * The card used to be a `role="button"` div whose Enter/Space handler had no
 * `e.target === e.currentTarget` guard, with the star / pop-out / delete buttons
 * nested inside it. Enter or Space on any of them bubbled to the card, which
 * `preventDefault()`-ed the button's click and opened the artifact instead.
 * The card is now a plain container holding an "open" button and those three
 * as siblings, so each keystroke reaches the control that has focus and nothing
 * else.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from './helpers'
import { api } from '../api/client'
import { LibraryMasonry, type GridEntry } from '../pages/ArtifactsPageGallery'
import type { Artifact } from '../types'

vi.mock('../api/client')

const openPopout = vi.hoisted(() => vi.fn())
vi.mock('../utils/artifactPopout', () => ({ openPopout }))

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

function renderCard() {
  const onOpen = vi.fn()
  const onDelete = vi.fn()
  const onTogglePin = vi.fn()
  const entries: GridEntry[] = [{ kind: 'local', key: art.slug, art }]
  renderWithProviders(
    <LibraryMasonry
      entries={entries}
      cols={1}
      widthRef={() => {}}
      scrollerRef={{ current: null }}
      fillPage={false}
      onOpen={onOpen}
      onDelete={onDelete}
      deletingSlug={null}
      onTogglePin={onTogglePin}
      pinningSlug={null}
    />,
  )
  return { onOpen, onDelete, onTogglePin }
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(api).artifact = vi.fn().mockResolvedValue({ ...art, content: '# hi' })
})

describe('artifact card keyboard', () => {
  it.each(['{Enter}', ' '])('runs each inner button from the keyboard (%s) without opening the card', async (key) => {
    const user = userEvent.setup()
    const { onOpen, onDelete, onTogglePin } = renderCard()

    const cases: [string, () => void][] = [
      ['Star artifact', () => expect(onTogglePin).toHaveBeenCalledWith(art)],
      ['Pop out to window', () => expect(openPopout).toHaveBeenCalledWith(art.slug, art.name)],
      ['Remove from artifacts library', () => expect(onDelete).toHaveBeenCalledWith(art)],
    ]
    for (const [label, fired] of cases) {
      const button = await screen.findByRole('button', { name: label })
      // Not nested inside another control: that is what let the card take its keys.
      expect(button.parentElement?.closest('button, [role="button"]')).toBeNull()
      button.focus()
      await user.keyboard(key)
      fired()
    }
    expect(onOpen).not.toHaveBeenCalled()
  })

  it('opens the artifact on Enter and Space from its open button', async () => {
    const user = userEvent.setup()
    const { onOpen } = renderCard()

    const open = await screen.findByRole('button', { name: art.name })
    open.focus()
    await user.keyboard('{Enter}')
    expect(onOpen).toHaveBeenCalledWith(art.slug)
    await user.keyboard(' ')
    expect(onOpen).toHaveBeenCalledTimes(2)
  })
})
