/**
 * The reaction layer's resolution rules — coercion, defaults, and what each
 * state actually renders.
 *
 * Every render assertion compares the rendered `src` against a data URI this
 * test composes itself through the same `ghostDataUri` the roster uses. That is
 * the only comparison worth making: a looser one ("the src changed") would pass
 * for a reaction that also moved an identity axis, which is the single thing
 * this layer must never do.
 */
import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'

import CrewAvatar, { ghostTraitsFrom, seededTraits, avatarFrameIn } from '../components/CrewAvatar'
import type { CrewAvatarOverride } from '../components/CrewAvatar'
import { ghostDataUri, type KiroGhostTraits } from '../lib/kiroGhostAvatar'
import { iconMotionVars, poseBodyDataUri, poseDataUri, poseEyeVars, poseEyesDataUri } from '../lib/avatarPoses'
import {
  AVATAR_STATES,
  MOTION_DEFAULTS,
  motionFor,
  motionsFrom,
  retiredCarryFrom,
  retiredCueFrom,
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

describe('motionsFrom coercion', () => {
  it('reads the two states that have a motion and drops everything else', () => {
    expect(
      motionsFrom({
        kind: 'ghost',
        motions: { done: 'nod', error: 'droop', working: 'bounce', sleeping: 'nod' },
      }),
    ).toEqual({ done: 'nod', error: 'droop' })
  })

  it('is ghost-only: a picture is static and a pack animates from its own files', () => {
    expect(motionsFrom({ kind: 'image', v: 2, motions: { done: 'nod' } })).toBeNull()
    expect(motionsFrom({ kind: 'pack', id: 'aurora', motions: { done: 'nod' } })).toBeNull()
  })

  it('is total: junk of every shape collapses to null instead of throwing', () => {
    for (const junk of [
      null,
      undefined,
      0,
      'ghost',
      [],
      {},
      { kind: 'ghost' },
      { motions: 7 },
      { kind: 'ghost', motions: 7 },
      { kind: 'ghost', motions: { done: 5 } },
      { kind: 'ghost', motions: { done: '' } },
    ]) {
      expect(motionsFrom(junk)).toBeNull()
      expect(soundsFrom(junk)).toBeNull()
    }
  })

  it('keeps an unknown motion name verbatim rather than substituting one', () => {
    // `motionFrom` resolves it to the still frame, so a newer vocabulary renders
    // as no reaction — never as a different reaction the author did not choose.
    expect(motionsFrom({ kind: 'ghost', motions: { done: 'backflip' } })).toEqual({
      done: 'backflip',
    })
  })

  it('canonicalizes order, so two equal sets of reactions stringify equally', () => {
    // The crew editor's unsaved-changes check compares these maps with
    // JSON.stringify, which is order-sensitive. Both sides go through this
    // coercion precisely so the draft's touch order cannot read as a change.
    expect(
      JSON.stringify(motionsFrom({ kind: 'ghost', motions: { error: 'droop', done: 'nod' } })),
    ).toBe(JSON.stringify(motionsFrom({ kind: 'ghost', motions: { done: 'nod', error: 'droop' } })))
  })
})

describe('soundsFrom coercion', () => {
  it('keeps a known preset and "none", and drops an unrecognised one', () => {
    // Unlike a motion, a preset this client cannot synthesize has no forgiving
    // rendering — it would be silence with no way to tell it from a deliberate
    // one.
    expect(
      soundsFrom({ kind: 'ghost', sounds: { working: 'blip', done: 'none', error: 'foghorn' } }),
    ).toEqual({ working: 'blip', done: 'none' })
  })

  it('is silent for a picture and for a pack', () => {
    // A picture is static and silent by design; a pack ships its own audio
    // files, so a preset on the crew record would be a second, competing answer.
    expect(soundsFrom({ kind: 'image', v: 3, sounds: { done: 'chime' } })).toBeNull()
    expect(soundsFrom({ kind: 'pack', id: 'aurora', sounds: { done: 'chime' } })).toBeNull()
  })
})

describe('retiredCarryFrom', () => {
  it('reports the retired keys a served tier is holding, with the tier they came from', () => {
    // Both reaction readers are ghost-gated, so on a picture or a pack EVERY
    // reaction key is inert -- and every one of them is a value the previous
    // release's builder could write.
    expect(
      retiredCarryFrom({ kind: 'image', v: 3, sounds: { done: 'chime' } }),
    ).toEqual({ kind: 'image', keys: { sounds: { done: 'chime' } } })
    expect(
      retiredCarryFrom({
        kind: 'pack',
        id: 'aurora',
        motions: { done: 'nod' },
        sounds: { error: 'pop' },
      }),
    ).toEqual({
      kind: 'pack',
      keys: { motions: { done: 'nod' }, sounds: { error: 'pop' } },
    })
  })

  it('carries only `expressions` off a ghost, whose other two keys are live', () => {
    // `motions` and `sounds` ARE read on a ghost, so they travel on the draft
    // like any other edit; carrying them here as well would re-assert a value
    // the user had just cleared in the builder.
    expect(
      retiredCarryFrom({
        kind: 'ghost',
        motions: { done: 'nod' },
        sounds: { done: 'chime' },
        expressions: { done: { eyes: 'wink' } },
      }),
    ).toEqual({ kind: 'ghost', keys: { expressions: { done: { eyes: 'wink' } } } })
  })

  it('carries nothing for a record holding no retired key, and nothing for junk', () => {
    expect(retiredCarryFrom({ kind: 'ghost', traits: { eyes: 'dot' } })).toBeNull()
    expect(retiredCarryFrom({ kind: 'image', v: 1 })).toBeNull()
    // A scalar was never a reaction map, so handing it back would re-assert
    // something the validator strips anyway.
    expect(retiredCarryFrom({ kind: 'image', v: 1, sounds: 'chime' })).toBeNull()
    expect(retiredCarryFrom({ kind: 'image', v: 1, sounds: ['chime'] })).toBeNull()
    expect(retiredCarryFrom({ kind: 'nonsense', sounds: { done: 'chime' } })).toBeNull()
    expect(retiredCarryFrom(null)).toBeNull()
    expect(retiredCarryFrom('image')).toBeNull()
  })
})

describe('retiredCueFrom', () => {
  it('names the one case a user notices: a served tier that was saved with a chime', () => {
    // The crew went quiet without its owner touching anything, so the builder
    // says so in a line rather than letting the silence be discovered. The
    // tier rides along so that line renders on ITS pane and no other.
    expect(retiredCueFrom({ kind: 'image', v: 3, sounds: { done: 'chime' } })).toEqual({
      kind: 'image',
      states: ['done'],
    })
    expect(retiredCueFrom({ kind: 'pack', id: 'aurora', sounds: { working: 'blip' } })).toEqual({
      kind: 'pack',
      states: ['working'],
    })
  })

  it('reports every lost state, in the vocabulary order the pane lists them in', () => {
    // The notice reads these out, so their order is the order the user sees
    // their own rows in rather than whatever order the record's keys took.
    expect(
      retiredCueFrom({
        kind: 'image',
        v: 3,
        sounds: { error: 'pop', working: 'blip', done: 'chime' },
      })?.states,
    ).toEqual(['working', 'done', 'error'])
  })

  it('stays quiet for a ghost, whose presets still play', () => {
    expect(retiredCueFrom({ kind: 'ghost', sounds: { done: 'chime' } })).toBeNull()
  })

  it('stays quiet for a stored silence and for junk, which were never audible', () => {
    // `'none'` IS the saved silence: nothing was lost, so there is nothing to say.
    expect(retiredCueFrom({ kind: 'image', v: 1, sounds: { done: 'none' } })).toBeNull()
    expect(retiredCueFrom({ kind: 'image', v: 1, sounds: { done: 'foghorn' } })).toBeNull()
    expect(retiredCueFrom({ kind: 'image', v: 1, sounds: 'chime' })).toBeNull()
    expect(retiredCueFrom({ kind: 'image', v: 1 })).toBeNull()
    expect(retiredCueFrom(null)).toBeNull()
    expect(retiredCueFrom('image')).toBeNull()
  })
})

describe('motionFor', () => {
  it('plays the stored motion for a state that has one', () => {
    expect(motionFor({ done: 'nod', error: 'droop' }, 'done')).toEqual({
      state: 'done',
      name: 'nod',
    })
  })

  it('falls back to the default, so the layer is on for an unconfigured crew', () => {
    expect(motionFor(null, 'done')).toEqual({ state: 'done', name: MOTION_DEFAULTS.done })
    expect(motionFor(null, 'error')).toEqual({ state: 'error', name: MOTION_DEFAULTS.error })
    expect(motionFor({ done: 'nod' }, 'error')).toEqual({ state: 'error', name: 'shake' })
  })

  it('pins the defaults themselves', () => {
    // A default is a product decision, not an implementation detail: changing it
    // changes what every uncustomized crew does.
    expect(MOTION_DEFAULTS).toEqual({ done: 'bounce', error: 'shake' })
  })

  it('keeps `none` as the stored opt-out rather than re-defaulting it', () => {
    expect(motionFor({ done: 'none' }, 'done')).toEqual({ state: 'done', name: 'none' })
  })

  it('has no reaction for the resting face or for work in progress', () => {
    // `working` is the ghost's own animation, and a reaction fires on the
    // transition out of it.
    expect(motionFor({ done: 'nod' }, 'idle')).toBeNull()
    expect(motionFor({ done: 'nod' }, 'working')).toBeNull()
    expect(motionFor({ done: 'nod' }, undefined)).toBeNull()
  })
})

describe('ghostTraitsFrom stays total', () => {
  it('answers null for a ghost override that pins no face', () => {
    // Reactions alone are a valid override. Resolving them to the seeded face
    // is the renderer's job — this function's null is what tells the editor's
    // dirty check that no face was pinned.
    expect(ghostTraitsFrom({ kind: 'ghost' })).toBeNull()
    expect(ghostTraitsFrom({ kind: 'ghost', motions: { done: 'nod' } })).toBeNull()
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
    motions: { done: 'nod', error: 'droop' },
  }

  it('plays the stored motion on the pinned face', () => {
    for (const state of ['done', 'error'] as const) {
      const { container, unmount } = render(
        <CrewAvatar seed={SEED} avatar={avatar} state={state} />,
      )
      expect(src(container)).toBe(
        ghostDataUri(PINNED, undefined, { state, name: state === 'done' ? 'nod' : 'droop' }),
      )
      unmount()
    }
  })

  it('animates the working state through the eye-bound working variant', () => {
    const { container } = render(<CrewAvatar seed={SEED} avatar={avatar} state="working" />)
    expect(src(container)).toBe(ghostDataUri(PINNED, 'subtle'))
  })

  it('honours the requested working intensity', () => {
    const { container } = render(
      <CrewAvatar seed={SEED} avatar={avatar} state="working" working="full" />,
    )
    expect(src(container)).toBe(ghostDataUri(PINNED, 'full'))
  })

  it('draws the resting face for idle, with no reaction at all', () => {
    const { container } = render(<CrewAvatar seed={SEED} avatar={avatar} state="idle" />)
    expect(src(container)).toBe(ghostDataUri(PINNED))
  })

  it('plays the DEFAULT motion for a state the record does not configure', () => {
    const partial: CrewAvatarOverride = { kind: 'ghost', traits: PINNED, motions: { done: 'nod' } }
    const { container } = render(<CrewAvatar seed={SEED} avatar={partial} state="error" />)
    expect(src(container)).toBe(ghostDataUri(PINNED, undefined, { state: 'error', name: 'shake' }))
  })

  it('reacts on the name-derived face too, with no override at all', () => {
    const { container } = render(<CrewAvatar seed={SEED} state="done" />)
    expect(src(container)).toBe(
      ghostDataUri(seededTraits(SEED), undefined, { state: 'done', name: 'bounce' }),
    )
  })

  it('resolves the seeded face as the base when the record pins no traits', () => {
    const reactionsOnly: CrewAvatarOverride = { kind: 'ghost', motions: { done: 'sparkle' } }
    const { container } = render(<CrewAvatar seed={SEED} avatar={reactionsOnly} state="done" />)
    expect(src(container)).toBe(
      ghostDataUri(seededTraits(SEED), undefined, { state: 'done', name: 'sparkle' }),
    )
  })

  it('a bare `working` prop still means state working', () => {
    // Back-compat: MembersPage shipped `working` before `state` existed.
    const legacy = render(<CrewAvatar seed={SEED} avatar={avatar} working="subtle" />)
    expect(src(legacy.container)).toBe(ghostDataUri(PINNED, 'subtle'))
  })

  it('plays the working body motion on an icon at one amplitude, whatever intensity is asked', () => {
    // An icon has no reaction layer, but "at work" is body motion that only
    // moves the pose — so it must read as working beside a ghost. The ghost's
    // intensity knob is not the icon's: a dense roster asks `subtle` and the
    // icon still moves at the one amplitude it draws.
    const icon: CrewAvatarOverride = { kind: 'icon', pose: 'pose-3', bg: '#25679d' }
    const cases = [
      <CrewAvatar key="bare" seed={SEED} avatar={icon} state="working" />,
      <CrewAvatar key="subtle" seed={SEED} avatar={icon} state="working" working="subtle" />,
      <CrewAvatar key="full" seed={SEED} avatar={icon} state="working" working="full" />,
      // The legacy bare `working` spelling reaches the icon too.
      <CrewAvatar key="legacy" seed={SEED} avatar={icon} working="subtle" />,
    ]
    for (const el of cases) {
      const { container, unmount } = render(el)
      const root = container.querySelector('[data-testid="crew-icon-motion"]') as HTMLElement
      expect(root.style.getPropertyValue('--crew-icon-secs')).toBe(iconMotionVars()['--crew-icon-secs'])
      unmount()
    }
  })

  it('moves a working icon body and eyes inside one transform chain, over a still tile', () => {
    // An SVG that animates itself is re-rastered every frame, in pieces on iOS.
    // So the tile is the root's own background, and two still images, the
    // silhouette and its eyes, sit inside the same body-motion wrappers. Only
    // the eyes add a blink and a glance, as the ghost tier's eyes do.
    const icon: CrewAvatarOverride = { kind: 'icon', pose: 'pose-3', bg: '#25679d' }
    for (const size of [20, 28, 40, 64, 96]) {
      const { container, unmount } = render(
        <CrewAvatar seed={SEED} avatar={icon} state="working" size={size} />,
      )
      const root = container.querySelector('[data-testid="crew-icon-motion"]') as HTMLElement
      expect(root.style.width).toBe(`${size}px`)
      expect(root.style.backgroundColor).toBe('#25679d')
      const imgs = root.querySelectorAll('img')
      expect(imgs).toHaveLength(2)
      expect(imgs[0].getAttribute('src')).toBe(poseBodyDataUri('pose-3'))
      expect(imgs[1].getAttribute('src')).toBe(poseEyesDataUri('pose-3'))
      const chainOf = (img: Element) => {
        const chain: string[] = []
        for (let n = img.parentElement; n && n !== root; n = n.parentElement) chain.push(n.className)
        return chain
      }
      // The body is the innermost child of the four motion wrappers, in order.
      const body = ['crew-icon-sq', 'crew-icon-bob', 'crew-icon-sway', 'crew-icon-fit']
      expect(chainOf(imgs[0])).toEqual(body)
      // The eyes ride the SAME chain, with their own blink and glance inside it.
      expect(chainOf(imgs[1])).toEqual(['crew-icon-blink', 'crew-icon-look', 'crew-icon-eyes', ...body])
      const blink = imgs[1].parentElement as HTMLElement
      expect(blink.style.getPropertyValue('--crew-icon-eye-y')).toBe(poseEyeVars('pose-3')['--crew-icon-eye-y'])
      // Nothing inside either image animates, and nothing above the fit moves the tile.
      for (const img of imgs) expect(decodeURIComponent(img.getAttribute('src')!)).not.toContain('<style')
      expect(root.className).not.toMatch(/crew-icon-(sway|bob|sq)/)
      unmount()
    }
  })

  it('names the frame a working icon draws its border on, and the one image of every other tier', () => {
    // A flight that counter-scales "the face's border" must find the element
    // that carries it: the motion root for a working icon, not its bare image.
    const icon: CrewAvatarOverride = { kind: 'icon', pose: 'pose-3', bg: '#25679d' }
    const moving = render(<CrewAvatar seed={SEED} avatar={icon} state="working" />)
    const root = moving.container.querySelector('[data-testid="crew-icon-motion"]')
    expect(avatarFrameIn(moving.container)).toBe(root)
    expect(avatarFrameIn(moving.container)?.className).toMatch(/\bborder\b/)
    moving.unmount()
    const still = render(<CrewAvatar seed={SEED} avatar={icon} />)
    expect(avatarFrameIn(still.container)).toBe(still.container.querySelector('img'))
    still.unmount()
  })

  it('paints the same tile behind a still and a moving icon', () => {
    const icon: CrewAvatarOverride = { kind: 'icon', pose: 'pose-3', bg: '#25679d' }
    const still = render(<CrewAvatar seed={SEED} avatar={icon} />)
    const stillBg = (still.container.querySelector('img') as HTMLElement).style.backgroundColor
    still.unmount()
    const moving = render(<CrewAvatar seed={SEED} avatar={icon} state="working" />)
    const movingBg = (moving.container.querySelector('[data-testid="crew-icon-motion"]') as HTMLElement).style.backgroundColor
    expect(stillBg).toBe('#25679d')
    expect(movingBg).toBe(stillBg)
  })

  it('draws an icon still for every state but working — it has no reactions', () => {
    const icon = { kind: 'icon', pose: 'pose-3', bg: '#25679d', motions: { done: 'bounce' } }
    for (const state of AVATAR_STATES) {
      if (state === 'working') continue
      const { container, unmount } = render(<CrewAvatar seed={SEED} avatar={icon} state={state} />)
      expect(src(container)).toBe(poseDataUri('pose-3', '#25679d'))
      unmount()
    }
  })

  it('serves a picture unchanged for every state — a picture is static', () => {
    const picture: CrewAvatarOverride = { kind: 'image', v: 4 }
    for (const state of AVATAR_STATES) {
      const { container, unmount } = render(
        <CrewAvatar seed={SEED} avatar={picture} state={state} />,
      )
      expect(src(container)).toBe(`/api/agents/${SEED}/avatar?v=4`)
      unmount()
    }
  })

  it('drops a motion smuggled onto a picture record instead of playing it', () => {
    // The ghost drawn behind a picture is the FALLBACK for one that will not
    // load; a reaction there would report a turn the crew never ran.
    const smuggled = { kind: 'image', motions: { done: 'bounce' } }
    const { container } = render(<CrewAvatar seed={SEED} avatar={smuggled} state="done" />)
    expect(src(container)).toBe(`/api/agents/${SEED}/avatar`)
  })

  it('a legacy record carrying `expressions` renders without it', () => {
    // The pickers are retired; the key round-trips on the backend for one more
    // release and nothing here reads it. The face must be the resting one.
    const legacy = { kind: 'ghost', traits: PINNED, expressions: { done: { mouth: 'grin' } } }
    const { container } = render(<CrewAvatar seed={SEED} avatar={legacy} state="idle" />)
    expect(src(container)).toBe(ghostDataUri(PINNED))
  })

  it('every state is renderable for a record that configures both reactions', () => {
    for (const state of AVATAR_STATES) {
      const { container, unmount } = render(<CrewAvatar seed={SEED} avatar={avatar} state={state} />)
      expect(src(container)).not.toBe(ghostDataUri(PINNED))
      unmount()
    }
  })
})
