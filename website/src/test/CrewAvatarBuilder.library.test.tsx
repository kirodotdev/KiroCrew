/**
 * CrewAvatarBuilder — the Library tier.
 *
 * `CrewAvatarLibraryTab.test.tsx` covers the pane's own listing, import and
 * delete. This file covers the seam: the Library tab exists, picking a pack in
 * it is what Apply commits, and the reset link puts the crew back to its own
 * name-derived face.
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

/** Move to a tier. The mode strip is a SegmentedControl, so every tier is a
 *  radio (it is compact, never collapsed) reached by its label. */
const gotoTier = (label: string) => fireEvent.click(screen.getByRole('radio', { name: label }))

beforeEach(() => {
  vi.clearAllMocks()
  mockApi.appearances.list.mockResolvedValue({ packs: [BUILTIN, AURORA] })
})

describe('avatar builder — Library tier', () => {
  it('offers the Library tab and reads the library when it opens', async () => {
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
    // A pack ships its own per-state art AND its own audio, and the ghost
    // reaction authoring is gone entirely, so there is nothing to author here.
    const stored = { kind: 'pack', id: 'aurora', sounds: { done: 'chime' } }
    const { onSave } = mount(stored as Parameters<typeof mount>[0])
    await screen.findByTestId('avatar-library-pane')

    expect(screen.queryByRole('radio', { name: 'Reactions' })).toBeNull()

    apply()
    expect(saved(onSave)).toEqual({ kind: 'pack', id: 'aurora' })
  })

  it('reset puts the crew back on its own face, pack included', async () => {
    const { onSave } = mount({ kind: 'pack', id: 'aurora' })
    await screen.findByTestId('avatar-library-pane')

    fireEvent.click(screen.getByTestId('avatar-builder-reset'))
    apply()

    expect(saved(onSave)).toBeNull()
  })

  it('switching tiers keeps each tier\u2019s draft, so a pack pick survives a look at Icon', async () => {
    const { onSave } = mount()
    gotoTier('Library')
    fireEvent.click(await screen.findByTestId('avatar-pack-select-aurora'))

    gotoTier('Icon')
    await waitFor(() => expect(screen.getByTestId('avatar-icon-pane')).toBeInTheDocument())
    gotoTier('Library')
    await screen.findByTestId('avatar-library-pane')

    apply()
    expect(saved(onSave)).toEqual({ kind: 'pack', id: 'aurora' })
  })

  it('an icon pick wins once the Icon tier is the selected one', async () => {
    // The tier decides what Apply commits; a pack id held from an earlier click
    // must not leak into a pose the user then chose.
    const { onSave } = mount()
    gotoTier('Library')
    fireEvent.click(await screen.findByTestId('avatar-pack-select-aurora'))
    gotoTier('Icon')
    fireEvent.click(screen.getByTestId('avatar-icon-pose-pose-2'))
    apply()

    expect(saved(onSave)).toMatchObject({ kind: 'icon', pose: 'pose-2' })
  })
})
