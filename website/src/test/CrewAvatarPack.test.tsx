/**
 * `CrewAvatar` — the appearance-pack tier.
 *
 * The pack tier is the one tier whose art this build did not draw: the record
 * names a pack and `PackAvatar` draws whatever format that pack holds. So what
 * matters here is the HANDOFF — that a pack record reaches `PackAvatar` with the
 * resolved state, that the failure it reports falls back to the crew's own face
 * rather than a broken-image glyph, and that the one pack which must never be
 * fetched (the built-in `kiro-ghost`, whose art is this bundle's own ghost) takes
 * the local path instead.
 *
 * `PackAvatar` itself is stubbed: which player draws which format, and the slot
 * fallback chain, are its own tests (`PackAvatar.test.tsx`).
 */
import { describe, it, expect, vi } from 'vitest'
import { act, render, screen } from '@testing-library/react'
import CrewAvatar, { hasAvatarOverride, packAvatarFrom, unclaimedAvatarFrom } from '../components/CrewAvatar'
import { BUILTIN_PACK_ID } from '../lib/appearancePacks/library'

/** The pack renderer, reduced to the props it was handed plus a way to make it
 *  fail: a click reports through `onError` and draws the caller's `fallback` in
 *  place — the real component's contract, which is what keeps its query observed
 *  (see `CrewAvatarPackRecovery.test.tsx` for the real one recovering). */
vi.mock('../components/appearancePacks/PackAvatar', async () => {
  const React = await import('react')
  function PackAvatarStub({ id, state, size, onError, fallback }: {
      id: string; state: string; size: number; onError?: () => void; fallback?: React.ReactNode
    }) {
      const [failed, setFailed] = React.useState(false)
      return (
        <>
          <button
            type="button"
            aria-label="pack avatar stub"
            data-testid="pack-avatar-stub"
            data-id={id}
            data-state={state}
            data-size={String(size)}
            data-failed={String(failed)}
            onClick={() => {
              setFailed(true)
              onError?.()
            }}
          />
          {failed ? fallback : null}
        </>
      )
  }
  return { default: PackAvatarStub }
})

const src = (container: HTMLElement) => container.querySelector('img')!.getAttribute('src')!
const stub = () => screen.getByTestId('pack-avatar-stub')

describe('packAvatarFrom', () => {
  it('reads a pack override', () => {
    expect(packAvatarFrom({ kind: 'pack', id: 'aurora' })).toEqual({ id: 'aurora' })
    expect(packAvatarFrom({ kind: 'pack', id: BUILTIN_PACK_ID })).toEqual({ id: BUILTIN_PACK_ID })
  })

  it('is total: absent, junk and another tier all read as "no pack"', () => {
    expect(packAvatarFrom(undefined)).toBeNull()
    expect(packAvatarFrom(null)).toBeNull()
    expect(packAvatarFrom('pack')).toBeNull()
    expect(packAvatarFrom({})).toBeNull()
    expect(packAvatarFrom({ kind: 'ghost', traits: {} })).toBeNull()
    expect(packAvatarFrom({ kind: 'image', v: 3 })).toBeNull()
    expect(packAvatarFrom({ kind: 'pack' })).toBeNull()
    expect(packAvatarFrom({ kind: 'pack', id: 7 })).toBeNull()
  })

  it('rejects an id the library could not hold', () => {
    // The backend's rule is letters, digits, dash and underscore, capped at 64.
    // Anything else names no directory, so it can only be junk — and reads as
    // the default face.
    expect(packAvatarFrom({ kind: 'pack', id: '' })).toBeNull()
    expect(packAvatarFrom({ kind: 'pack', id: '../etc' })).toBeNull()
    expect(packAvatarFrom({ kind: 'pack', id: 'a b' })).toBeNull()
    expect(packAvatarFrom({ kind: 'pack', id: 'a.b' })).toBeNull()
    expect(packAvatarFrom({ kind: 'pack', id: 'x'.repeat(65) })).toBeNull()
    expect(packAvatarFrom({ kind: 'pack', id: 'x'.repeat(64) })).toEqual({ id: 'x'.repeat(64) })
  })

  it('accepts the Unicode ids the backend really installs', () => {
    // `safe_pack_id` tests each character with Python's `str.isalnum()`, which is
    // Unicode-aware, and `import_bundle` installs such an id. An ASCII-only rule
    // here read those records as "no override" — so an unrelated save wrote `{}`
    // over them, which is the very loss the pack tier exists to prevent.
    expect(packAvatarFrom({ kind: 'pack', id: 'auróra' })).toEqual({ id: 'auróra' })
    expect(packAvatarFrom({ kind: 'pack', id: '아우로라' })).toEqual({ id: '아우로라' })
    expect(packAvatarFrom({ kind: 'pack', id: 'пакет_1' })).toEqual({ id: 'пакет_1' })
    expect(packAvatarFrom({ kind: 'pack', id: '外观包' })).toEqual({ id: '外观包' })
  })

  it('trims like the backend, so the slot URL names the pack it stored', () => {
    expect(packAvatarFrom({ kind: 'pack', id: '  aurora  ' })).toEqual({ id: 'aurora' })
    expect(packAvatarFrom({ kind: 'pack', id: '   ' })).toBeNull()
  })

  it('counts the length in code points, as the backend does', () => {
    // 64 astral characters is 128 UTF-16 units; measuring the string's `.length`
    // would refuse an id the backend accepts.
    const astral = '𝘢'.repeat(64)
    expect(packAvatarFrom({ kind: 'pack', id: astral })).toEqual({ id: astral })
    expect(packAvatarFrom({ kind: 'pack', id: '𝘢'.repeat(65) })).toBeNull()
  })
})

describe('hasAvatarOverride', () => {
  it('counts a pack as a customized face', () => {
    expect(hasAvatarOverride({ kind: 'pack', id: 'aurora' })).toBe(true)
    // A junk id renders the name-derived ghost, so it is NOT an override —
    // the same coercion the renderer applies.
    expect(hasAvatarOverride({ kind: 'pack', id: '../etc' })).toBe(false)
  })
})

describe('unclaimedAvatarFrom', () => {
  it('holds a record no reader understands, so a save can write it back', () => {
    // The wipe this tier's fix is about belongs to the ENUMERATION: the editor's
    // payload is `draft ?? {}`, so anything the readers do not claim is erased.
    // A fourth tier written by a newer client must survive an old client's save.
    expect(unclaimedAvatarFrom({ kind: 'hologram', id: 'x' })).toEqual({ kind: 'hologram', id: 'x' })
    expect(unclaimedAvatarFrom({ kind: 'pack', id: 'a b' })).toEqual({ kind: 'pack', id: 'a b' })
  })

  it('holds an unknown tier that ALSO carries reactions', () => {
    // `expressionsFrom`/`soundsFrom` are kind-AGNOSTIC: asked about a record
    // whose tier they know nothing about, they still answer "mine" on the
    // strength of a valid reaction map. Deciding "claimed" on that answer left
    // the wipe alive for every unknown tier that happens to carry a chime — the
    // record read as understood and the tier half went out with the next
    // unrelated save. The KIND has to decide it.
    expect(unclaimedAvatarFrom({ kind: 'hologram', sounds: { done: 'chime' } })).toEqual({
      kind: 'hologram',
      sounds: { done: 'chime' },
    })
    expect(
      unclaimedAvatarFrom({ kind: 'hologram', expressions: { done: { eyes: 'wink' } } }),
    ).toEqual({ kind: 'hologram', expressions: { done: { eyes: 'wink' } } })
    // Same for one of OUR kinds carrying a payload its own reader rejects: the
    // id fails the id rule, so no reader can reproduce the record either way.
    expect(unclaimedAvatarFrom({ kind: 'pack', id: 'a b', sounds: { done: 'chime' } })).toEqual({
      kind: 'pack',
      id: 'a b',
      sounds: { done: 'chime' },
    })
  })

  it('claims nothing a reader already understands', () => {
    expect(unclaimedAvatarFrom({ kind: 'ghost', traits: { eyes: 'wink' } })).toBeNull()
    expect(unclaimedAvatarFrom({ kind: 'image', v: 2 })).toBeNull()
    expect(unclaimedAvatarFrom({ kind: 'pack', id: 'aurora' })).toBeNull()
    // Reactions alone are a record the readers DO understand.
    expect(unclaimedAvatarFrom({ kind: 'ghost', sounds: { done: 'chime' } })).toBeNull()
  })

  it('is null for the shapes that already mean "no override"', () => {
    expect(unclaimedAvatarFrom(undefined)).toBeNull()
    expect(unclaimedAvatarFrom(null)).toBeNull()
    expect(unclaimedAvatarFrom({})).toBeNull()
    expect(unclaimedAvatarFrom('ghost')).toBeNull()
    expect(unclaimedAvatarFrom([1, 2])).toBeNull()
  })
})

describe('CrewAvatar — pack rendering', () => {
  it('hands the pack and the resolved state to the renderer', () => {
    for (const [state, resolved] of [
      [undefined, 'idle'],
      ['idle', 'idle'],
      ['working', 'working'],
      ['done', 'done'],
      ['error', 'error'],
    ] as const) {
      const { unmount } = render(
        <CrewAvatar seed="oncall" avatar={{ kind: 'pack', id: 'aurora' }} state={state} size={38} />,
      )
      expect(stub().getAttribute('data-id')).toBe('aurora')
      expect(stub().getAttribute('data-state')).toBe(resolved)
      expect(stub().getAttribute('data-size')).toBe('38')
      unmount()
    }
  })

  it('reads a bare `working` as the working state, same as the ghost tier', () => {
    render(<CrewAvatar seed="oncall" avatar={{ kind: 'pack', id: 'aurora' }} working="full" />)
    expect(stub().getAttribute('data-state')).toBe('working')
  })

  it('composes the built-in pack locally and fetches nothing', () => {
    // `kiro-ghost` IS the name-derived ghost and the slot route answers 404
    // `builtin_no_content` for it on purpose, so a request here would render a
    // broken face on every roster.
    const { container } = render(
      <CrewAvatar seed="oncall" avatar={{ kind: 'pack', id: BUILTIN_PACK_ID }} state="working" />,
    )
    expect(screen.queryByTestId('pack-avatar-stub')).toBeNull()
    const uri = src(container)
    expect(uri).not.toContain('/api/appearances')
    expect(uri.startsWith('data:image/svg+xml')).toBe(true)
  })

  it('renders the built-in pack exactly like the ghost tier, reactions included', () => {
    const expressions = { working: { eyes: 'wink' } }
    const pack = render(
      <CrewAvatar
        seed="oncall"
        avatar={{ kind: 'pack', id: BUILTIN_PACK_ID, expressions }}
        state="working"
      />,
    )
    const ghost = render(
      <CrewAvatar seed="oncall" avatar={{ kind: 'ghost', expressions }} state="working" />,
    )
    expect(src(pack.container)).toBe(src(ghost.container))
  })

  it('hands the renderer the crew face as its fallback, and reports a failure', () => {
    const onImageError = vi.fn()
    const { container } = render(
      <CrewAvatar seed="oncall" avatar={{ kind: 'pack', id: 'aurora' }} onImageError={onImageError} />,
    )
    // Nothing drawn for the fallback until the renderer says it needs it.
    expect(container.querySelector('img')).toBeNull()
    act(() => stub().click())

    expect(onImageError).toHaveBeenCalledTimes(1)
    // A broken-image glyph on a roster reads as "this crew is broken"; the
    // name-derived ghost reads as "this pack is gone", which is the truth.
    expect(src(container).startsWith('data:image/svg+xml')).toBe(true)
    // The renderer is STILL MOUNTED: it owns the pack's query, and a refetch
    // after a gateway blip has to find it here to redraw. Unmounting it would
    // make every failed read a ghost until the record names another pack.
    expect(stub().getAttribute('data-failed')).toBe('true')
  })

  it('keeps the renderer mounted across a re-render while failed', () => {
    const onImageError = vi.fn()
    const { rerender } = render(
      <CrewAvatar seed="oncall" avatar={{ kind: 'pack', id: 'aurora' }} onImageError={onImageError} />,
    )
    act(() => stub().click())
    rerender(
      <CrewAvatar seed="oncall" avatar={{ kind: 'pack', id: 'aurora' }} state="working" onImageError={onImageError} />,
    )
    expect(stub().getAttribute('data-state')).toBe('working')
    expect(stub().getAttribute('data-failed')).toBe('true')
  })

  it('ignores a pack record\u2019s expressions — served art has no face to overlay', () => {
    // The state still picks the SLOT; what it cannot do is repaint eyes onto a
    // drawing this build never composed.
    render(
      <CrewAvatar
        seed="oncall"
        avatar={{ kind: 'pack', id: 'aurora', expressions: { working: { eyes: 'wink' } } }}
        state="working"
      />,
    )
    // Reached the renderer as the plain pack it is: no face was composed, so no
    // ghost <img> is drawn beside it carrying an overlay.
    expect(stub().getAttribute('data-state')).toBe('working')
    expect(document.querySelector('img')).toBeNull()
  })
})
