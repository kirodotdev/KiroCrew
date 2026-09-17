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
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

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
  // The Library pane's invalidation hook reads the QueryClient; the seam under
  // test is the builder's, so the client carries only a retry-free default.
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const utils = render(
    <QueryClientProvider client={qc}>
      <CrewAvatarBuilder open name="oncall" value={value} onCancel={onCancel} onSave={onSave} />
    </QueryClientProvider>,
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

  it('offers no Reactions tab on a pack, and carries a legacy sound out of the record', async () => {
    // A pack ships its own per-state art AND its own audio, so a preset on the
    // crew record would be a second, competing answer to the same question.
    // There is nothing to author here, and a tab that renders only a note
    // saying so is a promise the tier cannot keep.
    const stored = { kind: 'pack', id: 'aurora', sounds: { done: 'chime' } }
    const { onSave } = mount(stored as Parameters<typeof mount>[0])
    await screen.findByTestId('avatar-library-pane')

    expect(screen.queryByRole('button', { name: 'Reactions' })).toBeNull()

    apply()
    expect(saved(onSave)).toEqual({ kind: 'pack', id: 'aurora' })
  })

  it('offers the Reactions tab only while the ghost is the selected tier', async () => {
    mount({ kind: 'image', v: 3 })
    expect(screen.queryByRole('button', { name: 'Reactions' })).toBeNull()
    gotoTier('Ghost face')
    expect(screen.getByRole('button', { name: 'Reactions' })).toBeInTheDocument()
    gotoTier('Reactions')
    // The hint covers all THREE rows it renders: it promised two moments while a
    // Working row was on screen, and a first-run reader guessed at that row. It
    // also says the working cue fires when work STARTS -- "while it works" read
    // as a sound that runs for the whole turn, where it plays once on the edge.
    expect(
      screen.getByText(
        'Pick a motion and a sound for when a turn finishes or fails, and the sound it makes when work starts.',
      ),
    ).toBeInTheDocument()
  })

  it('explains on both served tiers why the Reactions tab is not theirs', async () => {
    // The tab is simply absent there, and an absence with no word said reads as
    // something missing rather than as something decided — a first-run reader
    // could not tell which. The line goes where they are looking.
    // "a pack from the Library", not "a pack": a cold reader could not place the
    // bare word ("I don't know what a pack is… that's a guess").
    // "…so there is no Reactions tab here": a cold reader understood the concept
    // and still asked why the tab was gone, so the line names the tab.
    const NOTE =
      'Reactions belong to the ghost face, so there is no Reactions tab here. A picture stays still and silent; a pack from the Library plays its own art and sound.'
    const { unmount } = mount({ kind: 'pack', id: 'aurora' })
    await screen.findByTestId('avatar-library-pane')
    expect(screen.getByTestId('avatar-reactions-absent-pack')).toHaveTextContent(NOTE)
    unmount()

    mount({ kind: 'image', v: 3 })
    expect(screen.getByTestId('avatar-reactions-absent-picture')).toHaveTextContent(NOTE)
    // The ghost tier has the tab, so it needs no such line.
    gotoTier('Ghost face')
    expect(screen.queryByTestId('avatar-reactions-absent-picture')).toBeNull()
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
