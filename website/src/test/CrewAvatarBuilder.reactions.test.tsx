/**
 * The Reactions pane of the avatar builder.
 *
 * `CrewAvatarBuilder.test.tsx` covers the two identity tiers; this file covers
 * the reaction layer alone — the three state rows, the shape Apply hands the
 * editor, and the one rule the tier switch has to keep: reactions authored on
 * the ghost tier must survive a trip through the Picture tab, because silently
 * discarding them is indistinguishable from the builder forgetting them.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import CrewAvatarBuilder from '../components/CrewAvatarBuilder'
import type { CrewAvatarOverride } from '../components/CrewAvatar'
import { AVATAR_STATES, type RetiredCue } from '../lib/crewAvatarState'
import { playPreset } from '../hooks/useNotificationSound'

vi.mock('../hooks/useNotificationSound', async importOriginal => {
  const actual = await importOriginal<typeof import('../hooks/useNotificationSound')>()
  return {
    ...actual,
    playPreset: vi.fn(),
    loadSoundSettings: vi.fn(() => ({ enabled: true, volume: 0.4, perCategory: {} })),
  }
})

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

type Ghost = Extract<CrewAvatarOverride, { kind: 'ghost' }>
type Picture = Extract<CrewAvatarOverride, { kind: 'image' }>

function mount(
  value: CrewAvatarOverride | null = null,
  {
    retiredCue = null,
    savedPack = false,
    savedReactions,
  }: {
    retiredCue?: RetiredCue | null
    savedPack?: boolean
    savedReactions?: { motions?: boolean; sounds?: boolean }
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
        savedPack={savedPack}
        savedReactions={savedReactions}
        onCancel={onCancel}
        onSave={onSave}
      />
    </QueryClientProvider>,
  )
  return { ...utils, onSave, onCancel }
}

const openReactions = () => fireEvent.click(screen.getByRole('button', { name: 'Reactions' }))
const openPicture = () => fireEvent.click(screen.getByRole('button', { name: 'Picture' }))
const openLibrary = () => fireEvent.click(screen.getByRole('button', { name: 'Library' }))
const openFace = () => fireEvent.click(screen.getByRole('button', { name: 'Ghost face' }))
const apply = () => fireEvent.click(screen.getByTestId('avatar-builder-save'))
const lastSaved = (onSave: ReturnType<typeof vi.fn>) =>
  onSave.mock.calls.at(-1)?.[0] as CrewAvatarOverride | null

/** Choose an option from one of a state's two themed dropdowns. */
async function choose(testId: string, label: string) {
  const row = screen.getByTestId(testId)
  fireEvent.click(row.querySelector('[role="combobox"]') as HTMLElement)
  fireEvent.click(await screen.findByRole('option', { name: label }))
}

const chooseMotion = (state: string, label: string) =>
  choose(`avatar-state-motion-${state}`, label)
const chooseSound = (state: string, label: string) =>
  choose(`avatar-state-sound-${state}`, label)

describe('CrewAvatarBuilder — Reactions pane', () => {
  it('renders one row per state, with a motion only where there is one to play', () => {
    mount()
    openReactions()
    expect(screen.getByTestId('avatar-reactions-pane')).toBeInTheDocument()
    for (const state of AVATAR_STATES) {
      expect(screen.getByTestId(`avatar-state-row-${state}`)).toBeInTheDocument()
      expect(screen.getByTestId(`avatar-state-sound-${state}`)).toBeInTheDocument()
    }
    // Working has no motion picker: the ghost's working animation is its own,
    // bound to the eyes the face already wears.
    expect(screen.queryByTestId('avatar-state-motion-working')).toBeNull()
    expect(screen.queryByTestId('avatar-state-preview-working')).toBeNull()
    for (const state of ['done', 'error'] as const) {
      expect(screen.getByTestId(`avatar-state-motion-${state}`)).toBeInTheDocument()
      expect(screen.getByTestId(`avatar-state-preview-${state}`)).toBeInTheDocument()
    }
  })

  it('shows each state its own vocabulary, with the default already selected', () => {
    mount()
    openReactions()
    expect(screen.getByTestId('avatar-state-motion-done')).toHaveTextContent('Bounce')
    expect(screen.getByTestId('avatar-state-motion-error')).toHaveTextContent('Shake')
  })

  it('offers only that state\u2019s motions — a bounce is not an error reaction', async () => {
    mount()
    openReactions()
    const row = screen.getByTestId('avatar-state-motion-done')
    fireEvent.click(row.querySelector('[role="combobox"]') as HTMLElement)
    for (const label of ['Still', 'Bounce', 'Nod', 'Sparkle']) {
      expect(await screen.findByRole('option', { name: label })).toBeInTheDocument()
    }
    expect(screen.queryByRole('option', { name: 'Shake' })).toBeNull()
    expect(screen.queryByRole('option', { name: 'Droop' })).toBeNull()
  })

  it('emits only the states that were touched', async () => {
    const { onSave } = mount()
    openReactions()
    await chooseMotion('done', 'Nod')
    apply()
    const saved = lastSaved(onSave) as Ghost
    expect(saved.kind).toBe('ghost')
    expect(saved.motions).toEqual({ done: 'nod' })
  })

  it('never normalizes a stored motion away for matching the default', async () => {
    // A record that says `bounce` keeps saying it, even though bounce is what an
    // unconfigured crew already does: stripping it would make the crew follow a
    // future change of default, which is not what its author chose. (Picking the
    // entry the trigger already shows stores nothing, because the select fires
    // no change — that is the control's own no-op, not a normalization here.)
    const { onSave } = mount({ kind: 'ghost', motions: { done: 'bounce' } })
    openReactions()
    await chooseSound('error', 'Pop')
    apply()
    const saved = lastSaved(onSave) as Ghost
    expect(saved.motions).toEqual({ done: 'bounce' })
    expect(saved.sounds).toEqual({ error: 'pop' })
  })

  it('stores "Still" as the opt-out it is, not as an absence', async () => {
    const { onSave } = mount()
    openReactions()
    await chooseMotion('error', 'Still')
    apply()
    expect((lastSaved(onSave) as Ghost).motions).toEqual({ error: 'none' })
  })

  it('reactions alone are a complete override — no traits, so the seeded face stays', async () => {
    // `{kind:'ghost'}` with no traits means "the name-derived face, plus these
    // reactions"; writing traits here would silently pin the drawn face.
    const { onSave } = mount()
    openReactions()
    await chooseMotion('done', 'Sparkle')
    apply()
    const saved = lastSaved(onSave) as Ghost
    expect(saved.traits).toBeUndefined()
    expect(saved.motions).toEqual({ done: 'sparkle' })
  })

  it('carries a chosen sound through Apply', async () => {
    const { onSave } = mount()
    openReactions()
    await chooseSound('done', 'Chime')
    apply()
    expect((lastSaved(onSave) as Ghost).sounds).toEqual({ done: 'chime' })
  })

  it('the silent row clears a state back to no sound', async () => {
    const { onSave } = mount({ kind: 'ghost', sounds: { done: 'chime' } })
    openReactions()
    await chooseSound('done', 'No sound')
    apply()
    expect(lastSaved(onSave)).toBeNull()
  })

  it('previews a chosen sound through the shared synth, and cannot preview none', async () => {
    mount()
    openReactions()
    const button = screen.getByTestId('avatar-state-sound-preview-working')
    expect(button).toBeDisabled()
    await chooseSound('working', 'Blip')
    fireEvent.click(screen.getByTestId('avatar-state-sound-preview-working'))
    expect(vi.mocked(playPreset)).toHaveBeenCalledWith('blip', 0.4)
  })

  it('names the row on its reset link, so the two reset scopes are told apart', () => {
    mount()
    openReactions()
    // The pane also carries the footer's whole-avatar reset. A reader of two
    // unscoped "Reset" controls "would be afraid the wrong one wipes everything".
    expect(screen.getByTestId('avatar-state-reset-done')).toHaveTextContent('Reset Finished')
    expect(screen.getByTestId('avatar-state-reset-error')).toHaveTextContent('Reset Failed')
  })

  it('the per-state reset clears that state and leaves the others alone', async () => {
    const { onSave } = mount()
    openReactions()
    await chooseMotion('done', 'Nod')
    await chooseMotion('error', 'Droop')
    await chooseSound('done', 'Blip')
    fireEvent.click(screen.getByTestId('avatar-state-reset-done'))
    apply()
    const saved = lastSaved(onSave) as Ghost
    expect(saved.motions).toEqual({ error: 'droop' })
    expect(saved.sounds).toBeUndefined()
  })

  it('pre-fills from the stored record', () => {
    mount({ kind: 'ghost', motions: { done: 'sparkle' }, sounds: { error: 'pulse' } })
    openReactions()
    expect(screen.getByTestId('avatar-state-motion-done')).toHaveTextContent('Sparkle')
    expect(screen.getByTestId('avatar-state-sound-error')).toHaveTextContent('Pulse')
  })

  it('shows a motion name from a newer vocabulary rather than an empty trigger', () => {
    mount({ kind: 'ghost', motions: { done: 'backflip' } })
    openReactions()
    expect(screen.getByTestId('avatar-state-motion-done')).toHaveTextContent('backflip')
  })

  it('offers no Reactions tab on a picture — it is static and silent', () => {
    mount({ kind: 'image', v: 2 })
    expect(screen.queryByRole('button', { name: 'Reactions' })).toBeNull()
  })

  it('a picture Apply carries the picture and no reaction key', async () => {
    // Reactions authored on the ghost tier stay behind rather than riding out
    // on a record that cannot play them.
    const { onSave } = mount({ kind: 'image', v: 2 })
    openFace()
    openReactions()
    await chooseMotion('done', 'Nod')
    await chooseSound('done', 'Chime')
    openPicture()
    apply()
    const saved = lastSaved(onSave) as Picture
    expect(saved.kind).toBe('image')
    expect(saved.v).toBe(2)
    expect('motions' in saved).toBe(false)
    expect('sounds' in saved).toBe(false)
  })

  it('reactions survive a trip through the Picture tab and back', async () => {
    const { onSave } = mount()
    openReactions()
    await chooseMotion('done', 'Nod')
    openPicture()
    openFace()
    openReactions()
    expect(screen.getByTestId('avatar-state-motion-done')).toHaveTextContent('Nod')
    apply()
    expect((lastSaved(onSave) as Ghost).motions).toEqual({ done: 'nod' })
  })

  it('names the key when the last motion is cleared, so the clear reaches the wire', async () => {
    // The backend keeps a ghost's stored `motions` when a save says NOTHING
    // about the key (`_carry_motions_through_motionless_save`), because the older
    // editor could not author them. Omitting the key on a deliberate clear reads
    // as that client, and the motion the user just reset comes back.
    const { onSave } = mount({
      kind: 'ghost',
      traits: { eyes: 'canon', mouth: 'smile', tile: '#259d85' },
      motions: { done: 'nod' },
    } as CrewAvatarOverride)
    openReactions()
    fireEvent.click(screen.getByTestId('avatar-state-reset-done'))
    apply()
    const saved = lastSaved(onSave) as Ghost
    expect('motions' in saved).toBe(true)
    expect(saved.motions).toEqual({})
  })

  it('clears a stored sound the same way, by naming the key', async () => {
    const { onSave } = mount({
      kind: 'ghost',
      traits: { eyes: 'canon', mouth: 'smile', tile: '#259d85' },
      sounds: { done: 'chime' },
    } as CrewAvatarOverride)
    openReactions()
    fireEvent.click(screen.getByTestId('avatar-state-reset-done'))
    apply()
    expect((lastSaved(onSave) as Ghost).sounds).toEqual({})
  })

  it('still names a cleared key on a reopen whose draft already forgot it', () => {
    // The scenario: clear a motion, Apply (the draft now holds `motions: {}`),
    // reopen, Apply again without touching anything. The draft re-inits from
    // `{}` and cannot tell "cleared" from "never had one" — but the SAVED record
    // still carries the motion until the editor's Save lands, and an omitted key
    // would let the backend's carry restore it.
    const { onSave } = mount(
      { kind: 'ghost', traits: { eyes: 'canon', mouth: 'smile', tile: '#259d85' }, motions: {} },
      { savedReactions: { motions: true } },
    )
    openReactions()
    apply()
    const saved = lastSaved(onSave) as Ghost
    expect(saved.motions).toEqual({})
  })

  it('still omits a key the record never had', async () => {
    // Naming a key costs nothing to clear and everything to over-claim: an
    // untouched layer must stay absent, which is what "omit only when untouched"
    // means.
    const { onSave } = mount()
    openReactions()
    await chooseMotion('done', 'Nod')
    apply()
    const saved = lastSaved(onSave) as Ghost
    expect(saved.motions).toEqual({ done: 'nod' })
    expect('sounds' in saved).toBe(false)
  })

  it('a reactions-only crew that clears its last reaction resets instead of sending empty maps', () => {
    // No face pinned and nothing set: `{kind:'ghost', motions:{}}` would reach
    // the validator's all-empty collapse and be refused as junk, so the honest
    // answer is the reset — which deletes the record, cleared maps included.
    const { onSave } = mount({ kind: 'ghost', motions: { done: 'nod' } })
    openReactions()
    fireEvent.click(screen.getByTestId('avatar-state-reset-done'))
    apply()
    expect(lastSaved(onSave)).toBeNull()
  })

  it('pins the shown face when a pack crew moves to the ghost tier with a reaction', async () => {
    // A faceless ghost cannot express this change: the backend's
    // `_carry_pack_through_faceless_save` reads a ghost with no `traits` as a save
    // from a client that cannot see packs, keeps the pack and drops the motion —
    // so the crew stayed dressed and the reaction the user just picked was gone.
    const { onSave } = mount({ kind: 'pack', id: 'aurora' }, { savedPack: true })
    openFace()
    openReactions()
    await chooseMotion('done', 'Nod')
    apply()
    const saved = lastSaved(onSave) as Ghost
    expect(saved.kind).toBe('ghost')
    expect(saved.motions).toEqual({ done: 'nod' })
    // The face is stated rather than derived, which is what makes the tier change
    // legible on the wire.
    expect(saved.traits).toBeTruthy()
  })

  it('still resets when a pack crew moves to the ghost tier and picks nothing', () => {
    // Nothing to carry, so the honest answer is the explicit reset — which the
    // backend honours on every tier, pack included — not a face pinned on the
    // user's behalf.
    const { onSave } = mount({ kind: 'pack', id: 'aurora' }, { savedPack: true })
    openFace()
    apply()
    expect(lastSaved(onSave)).toBeNull()
  })

  it('keeps the faceless spelling for a ghost crew, where the name derives the face', async () => {
    const { onSave } = mount(null, { savedPack: false })
    openReactions()
    await chooseMotion('done', 'Nod')
    apply()
    const saved = lastSaved(onSave) as Ghost
    expect('traits' in saved).toBe(false)
    expect(saved.motions).toEqual({ done: 'nod' })
  })

  it('reset to the default face clears the reactions too, and says so on the link', () => {
    const { onSave } = mount({ kind: 'ghost', motions: { done: 'nod' }, sounds: { done: 'chime' } })
    openReactions()
    const reset = screen.getByTestId('avatar-builder-reset')
    // The label names BOTH halves: a link promising only the face while also
    // erasing two motions and three sounds is a control nobody can predict.
    expect(reset).toHaveTextContent(/reactions/i)
    fireEvent.click(reset)
    apply()
    expect(lastSaved(onSave)).toBeNull()
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
    // sentence about the wrong tier, which is a false reason. The reader could
    // not tell "if the warning is early". And the mirror: a saved-PACK crew on
    // the Picture pane must not be told "a picture is silent" about a crew
    // that is not one.
    const picture = mount({ kind: 'image', v: 2 }, { retiredCue: { kind: 'image', states: ['done'] } })
    openLibrary()
    await screen.findByTestId('avatar-reactions-absent-pack')
    expect(screen.queryByTestId('avatar-reactions-retired-cue-pack')).toBeNull()
    picture.unmount()
    mount({ kind: 'pack', id: 'aurora' }, { retiredCue: { kind: 'pack', states: ['done'] }, savedPack: true })
    openPicture()
    expect(screen.getByTestId('avatar-reactions-absent-picture')).toBeInTheDocument()
    expect(screen.queryByTestId('avatar-reactions-retired-cue-picture')).toBeNull()
  })

  it('does not tell a PACK crew it has gone silent, because this build gives it a cue', async () => {
    // The two tiers lost the same preset but did not end up in the same place:
    // a picture really is silent, while a worn pack plays the sounds its own
    // manifest declares. One shared sentence claimed silence for both and was
    // simply false on the pack pane.
    mount({ kind: 'pack', id: 'aurora' }, { retiredCue: { kind: 'pack', states: ['done'] }, savedPack: true })
    const note = await screen.findByTestId('avatar-reactions-retired-cue-pack')
    expect(note.textContent).toContain('its own')
    expect(note.textContent).not.toMatch(/is silent/)
  })

  it('promises no permanence it cannot keep, on either tier', async () => {
    // "for good" read as unrecoverable and scared a reader off the tier
    // altogether, but a preset is a pick from a fixed list: choosing the ghost
    // face and picking again restores it. The line ends on that action.
    const picture = mount({ kind: 'image', v: 2 }, { retiredCue: { kind: 'image', states: ['done'] } })
    const pictureNote = screen.getByTestId('avatar-reactions-retired-cue-picture')
    expect(pictureNote.textContent).not.toMatch(/for good/)
    expect(pictureNote.textContent).toMatch(/Ghost face/)
    // And it makes no claim about deletion at all, because there is none left to
    // claim: the page carries these keys through a save (see CrewRoster). Three
    // rounds of review went into wording a deletion that should not happen.
    expect(pictureNote.textContent).not.toMatch(/delete|deleted|drops/i)
    picture.unmount()
    mount({ kind: 'pack', id: 'aurora' }, { retiredCue: { kind: 'pack', states: ['done'] }, savedPack: true })
    const packNote = await screen.findByTestId('avatar-reactions-retired-cue-pack')
    expect(packNote.textContent).not.toMatch(/for good/)
    expect(packNote.textContent).toMatch(/Ghost face/)
  })

  it('names the states whose sound is going, rather than an unspecified one', () => {
    // A notice that says "your saved sound" about one of three leaves the
    // reader guessing which, and a reader who cannot tell what they are about
    // to lose does not press Apply at all. The record knows; the line says so.
    mount({ kind: 'image', v: 2 }, { retiredCue: { kind: 'image', states: ['done', 'error'] } })
    const note = screen.getByTestId('avatar-reactions-retired-cue-picture')
    expect(note.textContent).toContain('Finished')
    expect(note.textContent).toContain('Failed')
    // The state it does NOT name is the one the record never carried.
    expect(note.textContent).not.toContain('Working')
  })

  it('says nothing to a picture crew that never had one', () => {
    mount({ kind: 'image', v: 2 })
    expect(screen.queryByTestId('avatar-reactions-retired-cue-picture')).toBeNull()
    // The line explaining the ABSENT tab is unconditional; only the lost-cue one
    // is earned by the record.
    expect(screen.getByTestId('avatar-reactions-absent-picture')).toBeInTheDocument()
  })

  it('drops a legacy record\u2019s expressions on the next Apply', async () => {
    // The retirement, stated as a test: the pickers are gone, so an Apply
    // rewrites the record without the key rather than carrying it forward.
    const legacy = {
      kind: 'ghost',
      motions: { done: 'nod' },
      expressions: { done: { mouth: 'grin' } },
    } as unknown as CrewAvatarOverride
    const { onSave } = mount(legacy)
    openReactions()
    await chooseMotion('error', 'Droop')
    apply()
    const saved = lastSaved(onSave) as Ghost
    expect(saved.motions).toEqual({ done: 'nod', error: 'droop' })
    expect('expressions' in saved).toBe(false)
  })
})
