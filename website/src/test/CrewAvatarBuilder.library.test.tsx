/**
 * CrewAvatarBuilder — the Library tier.
 *
 * `CrewAvatarLibraryTab.test.tsx` covers the pane's own listing, import and
 * delete. This file covers the seam: the fourth tab exists, picking a pack in it
 * is what Apply commits, the reaction layer rides along with a pack the way it
 * does with a picture, and the reset link puts the crew back to its own face.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'

const mockApi = vi.hoisted(() => ({
  appearances: {
    list: vi.fn(),
    detail: vi.fn(),
    importBundle: vi.fn(),
    remove: vi.fn(),
  },
}))
vi.mock('../api/client', () => ({ api: mockApi }))

vi.mock('framer-motion', async () => {
  const React = await import('react')
  const FRAMER_PROPS = new Set([
    'layout', 'layoutId', 'initial', 'animate', 'exit', 'transition',
    'variants', 'whileHover', 'whileTap', 'onAnimationComplete',
  ])
  const make = (tag: string) =>
    React.forwardRef((props: Record<string, unknown>, ref: React.Ref<unknown>) => {
      const clean: Record<string, unknown> = {}
      for (const k of Object.keys(props)) {
        if (k === 'children' || FRAMER_PROPS.has(k)) continue
        clean[k] = props[k]
      }
      return React.createElement(tag, { ...clean, ref }, props.children as React.ReactNode)
    })
  const cache = new Map<string, unknown>()
  return {
    motion: new Proxy({}, {
      get: (_t, tag: string) => {
        if (!cache.has(tag)) cache.set(tag, make(tag))
        return cache.get(tag)
      },
    }),
    AnimatePresence: ({ children }: { children?: React.ReactNode }) =>
      React.createElement(React.Fragment, null, children),
    useReducedMotion: () => false,
  }
})

import CrewAvatarBuilder from '../components/CrewAvatarBuilder'
import type { CrewAvatarOverride } from '../components/CrewAvatar'
import { BUILTIN_PACK_ID } from '../lib/appearancePacks/library'

const AURORA = {
  id: 'aurora',
  name: 'Aurora',
  author: 'zoe',
  description: 'a paper fox',
  type: 'custom',
  format: 'svg',
}
const BUILTIN = {
  id: BUILTIN_PACK_ID,
  name: 'Kiro',
  author: 'Kiro Crew',
  description: 'The default companion.',
  type: 'builtin',
  format: 'svg',
}

function mount(value: CrewAvatarOverride | null = null) {
  const onSave = vi.fn()
  const onCancel = vi.fn()
  const utils = render(
    <CrewAvatarBuilder open name="oncall" value={value} onCancel={onCancel} onSave={onSave} />,
  )
  return { ...utils, onSave, onCancel }
}

const apply = () => fireEvent.click(screen.getByTestId('avatar-builder-save'))
const saved = (onSave: ReturnType<typeof vi.fn>) =>
  onSave.mock.calls.at(-1)?.[0] as CrewAvatarOverride | null

/** Move to a tier. The mode strip renders every tier as a button (it is compact,
 *  never collapsed), so the tab is reached by its label. */
const gotoTier = (label: string) => fireEvent.click(screen.getByRole('button', { name: label }))

beforeEach(() => {
  vi.clearAllMocks()
  mockApi.appearances.list.mockResolvedValue({ packs: [BUILTIN, AURORA] })
})

describe('avatar builder — Library tier', () => {
  it('offers Library as a fourth tab and reads the library when it opens', async () => {
    mount()
    expect(mockApi.appearances.list).not.toHaveBeenCalled()

    gotoTier('Library')

    expect(await screen.findByTestId('avatar-library-pane')).toBeInTheDocument()
    expect(mockApi.appearances.list).toHaveBeenCalledTimes(1)
  })

  it('Apply commits the picked pack as the crew\u2019s avatar', async () => {
    const { onSave } = mount()
    gotoTier('Library')
    fireEvent.click(await screen.findByTestId('avatar-pack-select-aurora'))
    apply()
    expect(saved(onSave)).toEqual({ kind: 'pack', id: 'aurora' })
  })

  it('refuses to Apply an empty pack tier', async () => {
    mount()
    gotoTier('Library')
    await screen.findByTestId('avatar-library-pane')
    // Nothing picked yet: Apply would mean "wear nothing", which is the reset
    // link's job and not this tab's.
    expect(screen.getByTestId('avatar-builder-save')).toBeDisabled()
  })

  it('opens on the Library tier for a crew that already wears a pack, preselected', async () => {
    mount({ kind: 'pack', id: 'aurora' })
    expect(await screen.findByTestId('avatar-library-pane')).toBeInTheDocument()
    expect(screen.getByTestId('avatar-pack-select-aurora')).toHaveAttribute('aria-selected', 'true')
  })

  it('keeps a pack crew\u2019s sounds and drops the face pickers it has no use for', async () => {
    const { onSave } = mount({ kind: 'pack', id: 'aurora', sounds: { done: 'chime' } })
    await screen.findByTestId('avatar-library-pane')

    gotoTier('Expressions')
    // Self-defining: this is the first place the word "pack" can reach a user
    // who never opened the Library tier, so the note says what one IS.
    expect(screen.getByTestId('avatar-expressions-pack-note')).toHaveTextContent(
      'This crew wears a pack — a ready-made set of faces — so only the sound applies here.',
    )
    // Sounds still apply — a pack reacts by ear.
    expect(screen.getByTestId('avatar-state-sound-done')).toBeInTheDocument()
    // Eyes and mouth do not: there is no face here to repaint.
    expect(screen.queryByTestId('avatar-expr-done-eyes')).toBeNull()
    expect(screen.queryByTestId('avatar-expr-done-mouth')).toBeNull()

    apply()
    expect(saved(onSave)).toEqual({ kind: 'pack', id: 'aurora', sounds: { done: 'chime' } })
  })

  it('drops the "pick a face" lead-in on the tiers that have no face', async () => {
    // The generic hint sat directly above each tier's own note, which says the
    // opposite ("only the sound applies here"). It belongs to the one tier that
    // has a face to pick.
    const HINT = 'Pick a different face and a sound for each moment.'
    const { unmount } = mount({ kind: 'pack', id: 'aurora' })
    await screen.findByTestId('avatar-library-pane')
    gotoTier('Expressions')
    expect(screen.queryByText(HINT)).toBeNull()
    unmount()

    mount({ kind: 'image', v: 3 })
    gotoTier('Expressions')
    expect(screen.queryByText(HINT)).toBeNull()
    gotoTier('Ghost face')
    gotoTier('Expressions')
    expect(screen.getByText(HINT)).toBeInTheDocument()
  })

  it('reset puts the crew back on its own face, pack included', async () => {
    const { onSave } = mount({ kind: 'pack', id: 'aurora' })
    await screen.findByTestId('avatar-library-pane')

    fireEvent.click(screen.getByTestId('avatar-builder-reset'))
    apply()

    expect(saved(onSave)).toBeNull()
  })

  it('switching tiers keeps each tier\u2019s draft, so a pack pick survives a look at Ghost face', async () => {
    const { onSave } = mount()
    gotoTier('Library')
    fireEvent.click(await screen.findByTestId('avatar-pack-select-aurora'))

    gotoTier('Ghost face')
    await waitFor(() => expect(screen.getByTestId('avatar-builder-preview')).toBeInTheDocument())
    gotoTier('Library')
    await screen.findByTestId('avatar-library-pane')

    apply()
    expect(saved(onSave)).toEqual({ kind: 'pack', id: 'aurora' })
  })

  it('a ghost pick wins once the ghost tier is the selected one', async () => {
    // The tier decides what Apply commits; a pack id held from an earlier click
    // must not leak into a face the user then chose.
    const { onSave } = mount()
    gotoTier('Library')
    fireEvent.click(await screen.findByTestId('avatar-pack-select-aurora'))
    gotoTier('Ghost face')
    fireEvent.click(await screen.findByTestId('avatar-opt-wink'))
    apply()

    const result = saved(onSave)
    expect(result?.kind).toBe('ghost')
  })
})
