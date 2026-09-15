/**
 * The reaction layer's resolution rules — coercion, overlay, and what each
 * state actually renders.
 *
 * Every render assertion compares the rendered `src` against a data URI this
 * test composes itself through the same `ghostDataUri` the roster uses. That is
 * the only comparison worth making: a looser one ("the src changed") would pass
 * for an overlay that also moved an identity axis, which is the single thing
 * this layer must never do.
 */
import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'

import CrewAvatar, { ghostTraitsFrom, seededTraits } from '../components/CrewAvatar'
import type { CrewAvatarOverride } from '../components/CrewAvatar'
import { ghostDataUri, type KiroGhostTraits } from '../lib/kiroGhostAvatar'
import {
  AVATAR_STATES,
  applyExpression,
  expressionFor,
  expressionsFrom,
  soundsFrom,
} from '../lib/crewAvatarState'

const SEED = 'radar'

const PINNED: KiroGhostTraits = {
  eyes: 'canon',
  brows: 'flat',
  mouth: 'smile',
  accessory: 'crown',
  prop: 'mug',
  blush: true,
  flip: false,
  tile: '#25679d',
}

const src = (el: HTMLElement) => el.querySelector('img')?.getAttribute('src') ?? ''

describe('expressionsFrom / soundsFrom coercion', () => {
  it('reads the three states and drops everything else', () => {
    expect(
      expressionsFrom({
        kind: 'ghost',
        expressions: {
          working: { eyes: 'squint' },
          done: { mouth: 'grin' },
          error: { eyes: 'cross', mouth: 'wobble' },
          sleeping: { eyes: 'closed' },
        },
      }),
    ).toEqual({
      working: { eyes: 'squint' },
      done: { mouth: 'grin' },
      error: { eyes: 'cross', mouth: 'wobble' },
    })
  })

  it('keeps only eyes and mouth — no other axis may vary per state', () => {
    // The whole point of the layer: a crew that changed hat or tile between
    // states would read as a different crew.
    expect(
      expressionsFrom({
        kind: 'ghost',
        expressions: { working: { eyes: 'wide', tile: '#de2121', accessory: 'halo', blush: true } },
      }),
    ).toEqual({ working: { eyes: 'wide' } })
  })

  it('treats an empty string as "same as idle" rather than as a trait name', () => {
    expect(expressionsFrom({ kind: 'ghost', expressions: { working: { eyes: '', mouth: '' } } })).toBeNull()
    expect(expressionsFrom({ kind: 'ghost', expressions: { done: { eyes: '', mouth: 'oh' } } })).toEqual({
      done: { mouth: 'oh' },
    })
  })

  it('is total: junk of every shape collapses to null instead of throwing', () => {
    for (const junk of [null, undefined, 0, 'ghost', [], { kind: 'ghost' }, { expressions: 7 }, { expressions: { working: 'wide' } }]) {
      expect(expressionsFrom(junk)).toBeNull()
      expect(soundsFrom(junk)).toBeNull()
    }
  })

  it('keeps an unknown eyes name verbatim so a newer vocabulary still renders', () => {
    // `compose` resolves an unknown key to "absent", which is the same
    // forgiveness the identity traits already get.
    expect(expressionsFrom({ kind: 'ghost', expressions: { working: { eyes: 'lasers' } } })).toEqual({
      working: { eyes: 'lasers' },
    })
  })

  it('keeps a known preset and "none", and drops an unrecognised one', () => {
    // Unlike a face trait, a preset this client cannot synthesize has no
    // forgiving rendering — it would be silence with no way to tell it from a
    // deliberate one.
    expect(
      soundsFrom({ kind: 'ghost', sounds: { working: 'blip', done: 'none', error: 'foghorn' } }),
    ).toEqual({ working: 'blip', done: 'none' })
  })

  it('canonicalizes order, so two equal sets of overrides stringify equally', () => {
    // The crew editor's unsaved-changes check compares these maps with
    // JSON.stringify, which is order-sensitive. Both sides go through this
    // coercion precisely so the draft's touch order cannot read as a change.
    const touchedErrorFirst = expressionsFrom({
      kind: 'ghost',
      expressions: { error: { mouth: 'wobble', eyes: 'cross' }, working: { eyes: 'squint' } },
    })
    const touchedWorkingFirst = expressionsFrom({
      kind: 'ghost',
      expressions: { working: { eyes: 'squint' }, error: { eyes: 'cross', mouth: 'wobble' } },
    })
    expect(JSON.stringify(touchedErrorFirst)).toBe(JSON.stringify(touchedWorkingFirst))
    expect(JSON.stringify(soundsFrom({ kind: 'ghost', sounds: { done: 'chime', working: 'blip' } })))
      .toBe(JSON.stringify(soundsFrom({ kind: 'ghost', sounds: { working: 'blip', done: 'chime' } })))
  })

  it('reads sounds off a picture too — a picture has no face but still has sounds', () => {
    expect(soundsFrom({ kind: 'image', v: 3, sounds: { done: 'chime' } })).toEqual({ done: 'chime' })
  })
})

describe('applyExpression', () => {
  it('varies only eyes and mouth, and leaves every identity axis alone', () => {
    const out = applyExpression(PINNED, { eyes: 'squint', mouth: 'wobble' })
    expect(out).toEqual({ ...PINNED, eyes: 'squint', mouth: 'wobble' })
  })

  it('applies one axis without inventing the other', () => {
    expect(applyExpression(PINNED, { eyes: 'wide' })).toEqual({ ...PINNED, eyes: 'wide' })
    expect(applyExpression(PINNED, { mouth: 'oh' })).toEqual({ ...PINNED, mouth: 'oh' })
  })

  it('returns the base itself when there is nothing to vary', () => {
    expect(applyExpression(PINNED, null)).toBe(PINNED)
    expect(applyExpression(PINNED, {})).toBe(PINNED)
  })
})

describe('expressionFor', () => {
  it('never resolves an expression for the resting face', () => {
    const expressions = { working: { eyes: 'squint' }, done: { mouth: 'grin' } }
    expect(expressionFor(expressions, 'idle')).toBeNull()
    expect(expressionFor(expressions, undefined)).toBeNull()
    expect(expressionFor(expressions, 'working')).toEqual({ eyes: 'squint' })
  })

  it('falls back to idle for a state the record does not configure', () => {
    expect(expressionFor({ working: { eyes: 'squint' } }, 'error')).toBeNull()
  })
})

describe('ghostTraitsFrom stays total', () => {
  it('answers null for a ghost override that pins no face', () => {
    // Reactions alone are a valid override. Resolving them to the seeded face
    // is the renderer's job — this function's null is what tells the editor's
    // dirty check that no face was pinned.
    expect(ghostTraitsFrom({ kind: 'ghost' })).toBeNull()
    expect(ghostTraitsFrom({ kind: 'ghost', expressions: { working: { eyes: 'wide' } } })).toBeNull()
  })

  it('answers null for junk rather than throwing', () => {
    for (const junk of [null, undefined, 7, 'x', [], {}, { kind: 'image' }, { kind: 'ghost', traits: 'no' }]) {
      expect(() => ghostTraitsFrom(junk)).not.toThrow()
      expect(ghostTraitsFrom(junk)).toBeNull()
    }
  })
})

describe('CrewAvatar renders the state it is told', () => {
  const avatar: CrewAvatarOverride = {
    kind: 'ghost',
    traits: PINNED,
    expressions: { working: { eyes: 'squint' }, error: { eyes: 'cross', mouth: 'wobble' } },
  }

  it('overlays the state expression on the pinned face at working intensity', () => {
    const { container } = render(<CrewAvatar seed={SEED} avatar={avatar} state="working" />)
    expect(src(container)).toBe(ghostDataUri({ ...PINNED, eyes: 'squint' }, 'subtle'))
  })

  it('honours the requested working intensity', () => {
    const { container } = render(<CrewAvatar seed={SEED} avatar={avatar} state="working" working="full" />)
    expect(src(container)).toBe(ghostDataUri({ ...PINNED, eyes: 'squint' }, 'full'))
  })

  it('renders a configured error state as a still frame', () => {
    const { container } = render(<CrewAvatar seed={SEED} avatar={avatar} state="error" />)
    expect(src(container)).toBe(ghostDataUri({ ...PINNED, eyes: 'cross', mouth: 'wobble' }))
  })

  it('falls back to the idle face for a state with no expression', () => {
    const idle = render(<CrewAvatar seed={SEED} avatar={avatar} state="idle" />)
    const done = render(<CrewAvatar seed={SEED} avatar={avatar} state="done" />)
    expect(src(done.container)).toBe(ghostDataUri(PINNED))
    expect(src(done.container)).toBe(src(idle.container))
  })

  it('resolves the seeded face as the base when the record pins no traits', () => {
    const seededOnly: CrewAvatarOverride = {
      kind: 'ghost',
      expressions: { done: { mouth: 'grin' } },
    }
    const { container } = render(<CrewAvatar seed={SEED} avatar={seededOnly} state="done" />)
    expect(src(container)).toBe(ghostDataUri({ ...seededTraits(SEED), mouth: 'grin' }))
  })

  it('a bare `working` prop still means state working', () => {
    // Back-compat: MembersPage shipped `working` before `state` existed.
    const legacy = render(<CrewAvatar seed={SEED} avatar={avatar} working="subtle" />)
    expect(src(legacy.container)).toBe(ghostDataUri({ ...PINNED, eyes: 'squint' }, 'subtle'))
  })

  it('ignores expressions on a picture — there is no face to change', () => {
    const picture: CrewAvatarOverride = {
      kind: 'image',
      v: 4,
      expressions: { working: { eyes: 'squint' } },
    }
    const { container } = render(<CrewAvatar seed={SEED} avatar={picture} state="working" />)
    expect(src(container)).toBe(`/api/agents/${SEED}/avatar?v=4`)
  })

  it('every state is renderable for a record that configures all three', () => {
    const full: CrewAvatarOverride = {
      kind: 'ghost',
      traits: PINNED,
      expressions: {
        working: { eyes: 'squint' },
        done: { mouth: 'grin' },
        error: { eyes: 'cross' },
      },
    }
    for (const state of AVATAR_STATES) {
      const { container } = render(<CrewAvatar seed={SEED} avatar={full} state={state} />)
      expect(src(container)).not.toBe(ghostDataUri(PINNED))
    }
  })
})
