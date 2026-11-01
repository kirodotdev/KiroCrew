/**
 * The avatar builder's reaction-layer surfaces that OUTLIVE the ghost-face
 * picker's removal (issue #18616).
 *
 * The Reactions pane itself was the ghost tier's sub-tab, so with the ghost-face
 * picker gone it is no longer reachable and its authoring is not exercised here
 * any more. What remains, and is covered below, is everything the picture and
 * pack tiers still say ABOUT reactions — the "this tier has no Reactions tab"
 * note and the retired-cue notices — plus the invariant that no tier offers a
 * path back to the ghost face or its Reactions tab.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import CrewAvatarBuilder from '../components/CrewAvatarBuilder'
import type { CrewAvatarOverride } from '../components/CrewAvatar'
import type { RetiredCue } from '../lib/crewAvatarState'

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

type Picture = Extract<CrewAvatarOverride, { kind: 'image' }>

function mount(
  value: CrewAvatarOverride | null = { kind: 'ghost' } as CrewAvatarOverride,
  {
    retiredCue = null,
  }: {
    retiredCue?: RetiredCue | null
  } = {},
) {
  const onSave = vi.fn()
  const onCancel = vi.fn()
  // A pack-wearing crew opens on the Library pane, which reads the pack list
  // through React Query. No retry: nothing here answers the route, and a retry
  // ladder would only slow the test down.
  const queries = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const utils = render(
    <QueryClientProvider client={queries}>
      <CrewAvatarBuilder
        open
        name="radar"
        value={value}
        retiredCue={retiredCue}
        onCancel={onCancel}
        onSave={onSave}
      />
    </QueryClientProvider>,
  )
  return { ...utils, onSave, onCancel }
}

const openPicture = () => fireEvent.click(screen.getByRole('radio', { name: 'Picture' }))
const openLibrary = () => fireEvent.click(screen.getByRole('radio', { name: 'Library' }))
const apply = () => fireEvent.click(screen.getByTestId('avatar-builder-save'))
const lastSaved = (onSave: ReturnType<typeof vi.fn>) =>
  onSave.mock.calls.at(-1)?.[0] as CrewAvatarOverride | null


describe('CrewAvatarBuilder — reaction surfaces after the ghost-face picker removal (#18616)', () => {
  it('offers no Reactions tab on a picture — it is static and silent', () => {
    mount({ kind: 'image', v: 2 })
    expect(screen.queryByRole('radio', { name: 'Reactions' })).toBeNull()
  })

  it('a picture Apply carries the picture and no reaction key', () => {
    // A picture crew cannot reach the ghost tier (new ghost authoring is
    // removed, issue #18616), so there is no Ghost face tab to detour through —
    // the picture saves verbatim, carrying no reaction key.
    const { onSave } = mount({ kind: 'image', v: 2 })
    expect(screen.queryByRole('radio', { name: 'Ghost face' })).toBeNull()
    expect(screen.queryByRole('radio', { name: 'Reactions' })).toBeNull()
    apply()
    const saved = lastSaved(onSave) as Picture
    expect(saved.kind).toBe('image')
    expect(saved.v).toBe(2)
    expect('motions' in saved).toBe(false)
    expect('sounds' in saved).toBe(false)
  })

  it('offers no path to the ghost face or its Reactions tab, on any tier (#18616)', () => {
    // The ghost-face picker is gone in both flows: neither a new crew, a
    // picture, a pack, nor an existing ghost crew is offered the Ghost face or
    // Reactions tab. Existing ghosts keep rendering (covered in CrewAvatarBuilder.test).
    for (const value of [null, { kind: 'image', v: 2 }, { kind: 'pack', id: 'aurora' }, { kind: 'ghost', traits: {} }] as (CrewAvatarOverride | null)[]) {
      const { unmount } = mount(value)
      expect(screen.queryByRole('radio', { name: 'Ghost face' })).toBeNull()
      expect(screen.queryByRole('radio', { name: 'Reactions' })).toBeNull()
      unmount()
    }
  })

  it('tells a picture crew that its saved sound has stopped playing', () => {
    // Silence with no word said is the worst version of the retirement: the user
    // hunts for a broken sound setting instead of reading one line.
    mount({ kind: 'image', v: 2 }, { retiredCue: { kind: 'image', states: ['done', 'error'] } })
    expect(screen.getByTestId('avatar-reactions-retired-cue-picture')).toBeInTheDocument()
  })

  it('shows each tier\u2019s notice on that tier\u2019s pane only, never across', async () => {
    // A saved-PICTURE crew browsing the Library pane must not read "this pack
    // plays its own sounds instead" with no pack worn or selected -- a true
    // sentence about the wrong tier. And the mirror: a saved-PACK crew on the
    // Picture pane must not be told "a picture is silent" about a crew that is
    // not one.
    const picture = mount({ kind: 'image', v: 2 }, { retiredCue: { kind: 'image', states: ['done'] } })
    openLibrary()
    await screen.findByTestId('avatar-reactions-absent-pack')
    expect(screen.queryByTestId('avatar-reactions-retired-cue-pack')).toBeNull()
    picture.unmount()
    mount({ kind: 'pack', id: 'aurora' }, { retiredCue: { kind: 'pack', states: ['done'] } })
    openPicture()
    expect(screen.getByTestId('avatar-reactions-absent-picture')).toBeInTheDocument()
    expect(screen.queryByTestId('avatar-reactions-retired-cue-picture')).toBeNull()
  })

  it('does not tell a PACK crew it has gone silent, because this build gives it a cue', async () => {
    // A picture really is silent, while a worn pack plays the sounds its own
    // manifest declares.
    mount({ kind: 'pack', id: 'aurora' }, { retiredCue: { kind: 'pack', states: ['done'] } })
    const note = await screen.findByTestId('avatar-reactions-retired-cue-pack')
    expect(note.textContent).toContain('its own')
    expect(note.textContent).not.toMatch(/is silent/)
  })

  it('promises no permanence it cannot keep, on either tier', async () => {
    const picture = mount({ kind: 'image', v: 2 }, { retiredCue: { kind: 'image', states: ['done'] } })
    const pictureNote = screen.getByTestId('avatar-reactions-retired-cue-picture')
    expect(pictureNote.textContent).not.toMatch(/for good/)
    // The ghost-face tab is gone (#18616), so the notice must NOT send the user
    // to it — there is no re-pick path, and a dead direction is worse than none.
    expect(pictureNote.textContent).not.toMatch(/Ghost face/)
    expect(pictureNote.textContent).not.toMatch(/delete|deleted|drops/i)
    picture.unmount()
    mount({ kind: 'pack', id: 'aurora' }, { retiredCue: { kind: 'pack', states: ['done'] } })
    const packNote = await screen.findByTestId('avatar-reactions-retired-cue-pack')
    expect(packNote.textContent).not.toMatch(/for good/)
    expect(packNote.textContent).not.toMatch(/Ghost face/)
  })

  it('names the states whose sound is going, rather than an unspecified one', () => {
    mount({ kind: 'image', v: 2 }, { retiredCue: { kind: 'image', states: ['done', 'error'] } })
    const note = screen.getByTestId('avatar-reactions-retired-cue-picture')
    expect(note.textContent).toContain('Finished')
    expect(note.textContent).toContain('Failed')
    expect(note.textContent).not.toContain('Working')
  })

  it('says nothing to a picture crew that never had one', () => {
    mount({ kind: 'image', v: 2 })
    expect(screen.queryByTestId('avatar-reactions-retired-cue-picture')).toBeNull()
    // The line explaining the ABSENT tab is unconditional; only the lost-cue one
    // is earned by the record.
    expect(screen.getByTestId('avatar-reactions-absent-picture')).toBeInTheDocument()
  })
})
