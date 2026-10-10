/**
 * CrewIdentityMark — the per-crew identity cue in the navigation switcher.
 *
 * Pins the ONE load-bearing property: the tint is a pure function of the
 * crew's STABLE id, not its list position — so reordering, renaming or
 * removing a crew cannot recolour its neighbours, and an embedded pane
 * derives the same tint with no shared state. Also pins local (null id)
 * vs remote rendering.
 */
import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import CrewIdentityMark, { crewIdentityTint } from '../components/CrewIdentityMark'

describe('crewIdentityTint', () => {
  it('is deterministic for a given id', () => {
    expect(crewIdentityTint('crew-abc')).toBe(crewIdentityTint('crew-abc'))
  })

  it('maps a null id (Local) to the aim token, never a remote tint', () => {
    expect(crewIdentityTint(null)).toBe('var(--aim)')
  })

  it('resolves every id to one of the five remote tint tokens', () => {
    const tints = new Set(['--ok', '--info', '--warn', '--clarify', '--danger'].map(t => `var(${t})`))
    for (const id of ['a', 'crew-1', 'crew-2', 'zzz', 'remote::xyz', '42']) {
      expect(tints.has(crewIdentityTint(id))).toBe(true)
    }
  })

  it('depends on the id value, not its position — reorder cannot recolour a crew', () => {
    const roster = ['crew-alpha', 'crew-beta', 'crew-gamma']
    const before = roster.map(crewIdentityTint)
    const reordered = [...roster].reverse().map(crewIdentityTint)
    // The tint for a given id is unchanged regardless of where it sits.
    expect(crewIdentityTint('crew-beta')).toBe(before[1])
    expect(reordered).toEqual([...before].reverse())
  })
})

describe('CrewIdentityMark', () => {
  it('renders the Local product icon for a null id, with no identity border', () => {
    const { getByTestId } = render(<CrewIdentityMark id={null} />)
    // The /logo.png img is mounted (so the browser fetches it) but hidden until
    // it loads; a bundled ghost-mark fallback is shown meanwhile, so the
    // collapsed rail is never blank on a failed logo fetch.
    expect(getByTestId('local-crew-icon')).toBeInTheDocument()
    expect(getByTestId('local-crew-icon-fallback')).toBeInTheDocument()
    expect(getByTestId('crew-identity-mark').className).not.toContain('border')
  })

  it('renders the ghost mark tinted and bordered for a remote crew id', () => {
    const { getByTestId, queryByTestId } = render(<CrewIdentityMark id="remote-xyz" />)
    expect(getByTestId('kiro-ghost-mark')).toBeInTheDocument()
    expect(queryByTestId('local-crew-icon')).toBeNull()
    const mark = getByTestId('crew-identity-mark')
    expect(mark.className).toContain('border')
    // jsdom does not serialize color-mix() into the style attribute, so assert
    // the tint the component applied (the `color` token) rather than the
    // color-mix background/border strings it also sets.
    expect(mark.getAttribute('style')).toContain('var(--')
  })
})
