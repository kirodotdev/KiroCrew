import { describe, it, expect } from 'vitest'
import { isBuiltinServerRow, mergeBuiltinRow } from '../components/appstore/mergeBuiltinRow'
import type { RegistryApp } from '../components/appstore/types'

/**
 * The precedence IS the contract, and it used to be spelled twice with opposite
 * answers: the browse list preferred the server row while the detail page
 * preferred the local manifest, so one app could read "Kiro Crew ·
 * Developer Tools" in the list and "kirocrew · Productivity" one click later.
 * These tests pin the single answer both surfaces now share.
 */
const ROW = {
  name: 'meetings',
  displayName: 'Meetings',
  description: 'Curated summary.',
  version: '1.2.0',
  author: 'adunuthu',
  tags: ['productivity'],
  iconUrl: '/app-assets/meetings/icon.svg',
  installed: true,
  origin: 'builtin',
} as RegistryApp

const MANIFEST = {
  displayName: 'Meetings Local',
  description: 'Local summary.',
  author: 'someone-else',
  tags: ['developer-tools'],
  highlights: ['Local highlight'],
  heroImageDetail: '/app-assets/meetings/hero-detail.svg',
  license: 'Apache-2.0',
  ui: { pages: [{ icon: 'calendar', iconUrl: '/app-assets/meetings/page-icon.svg' }] },
}

describe('isBuiltinServerRow', () => {
  it('accepts a genuine built-in row', () => {
    expect(isBuiltinServerRow(ROW)).toBe(true)
  })

  it('refuses an external row that inherited builtin origin by name collision', () => {
    // The server stamps `origin` from the INSTALLED app of the same name, so an
    // external registry row named `meetings` arrives carrying origin:'builtin'.
    // Accepting it would render attacker-controlled copy, artwork and author on
    // a first-party surface.
    const squatter = { ...ROW, _registry: 'evil-org', author: 'attacker' } as RegistryApp
    expect(isBuiltinServerRow(squatter)).toBe(false)
  })

  it('refuses a row the server marked external via provenance', () => {
    const squatter = { ...ROW, provenance: 'external' } as RegistryApp
    expect(isBuiltinServerRow(squatter)).toBe(false)
  })

  it('refuses a non-builtin row', () => {
    expect(isBuiltinServerRow({ ...ROW, origin: 'registry' } as RegistryApp)).toBe(false)
  })
})

describe('mergeBuiltinRow', () => {
  it('lets the published catalog win on every field it publishes', () => {
    const merged = mergeBuiltinRow(ROW, MANIFEST)
    // A republished document must be able to correct these without a wheel.
    expect(merged.author).toBe('adunuthu')
    expect(merged.tags).toEqual(['productivity'])
    expect(merged.version).toBe('1.2.0')
    expect(merged.iconUrl).toBe('/app-assets/meetings/icon.svg')
    expect(merged.description).toBe('Curated summary.')
  })

  it('falls back to the manifest for fields the catalog does not publish', () => {
    const merged = mergeBuiltinRow(ROW, MANIFEST)
    expect(merged.highlights).toEqual(['Local highlight'])
    expect(merged.heroImageDetail).toBe('/app-assets/meetings/hero-detail.svg')
    expect(merged.license).toBe('Apache-2.0')
    expect(merged.icon).toBe('calendar')
  })

  it('treats an empty catalog array as "not published", not as "published empty"', () => {
    // A row whose tags were dropped by validation must not blank the rail
    // placement the manifest can still supply.
    const merged = mergeBuiltinRow({ ...ROW, tags: [] } as RegistryApp, MANIFEST)
    expect(merged.tags).toEqual(['developer-tools'])
  })

  it('survives a manifest-less installed app without inventing fields', () => {
    const merged = mergeBuiltinRow(ROW, undefined)
    expect(merged.author).toBe('adunuthu')
    expect(merged.highlights).toBeUndefined()
    expect(merged.icon).toBe('')
  })

  it('never invents an author when neither source states one', () => {
    // The previous browse-side chain ended in a hardcoded 'kirocrew', which
    // asserted first-party authorship no source had made.
    const merged = mergeBuiltinRow({ ...ROW, author: undefined } as RegistryApp, {})
    expect(merged.author).toBe('')
  })

  it('produces the SAME author and tags for the browse and detail call shapes', () => {
    // AppDetailPage passes its looser RegistryEntry shape; AppsPage passes a
    // normalised RegistryApp. Both must resolve identically or the two surfaces
    // disagree about one app again.
    const looseRow = { name: 'meetings', author: 'adunuthu', tags: ['productivity'] }
    const fromDetail = mergeBuiltinRow(looseRow, MANIFEST)
    const fromBrowse = mergeBuiltinRow(ROW, MANIFEST)
    expect(fromDetail.author).toBe(fromBrowse.author)
    expect(fromDetail.tags).toEqual(fromBrowse.tags)
  })

  it('prefers the INSTALLED version over the catalog version', () => {
    // The catalog is fetched from the network and can advertise a release newer
    // than the wheel the user runs. A row claiming a version the user does not
    // have is a lie about their own machine, so local wins on this one field.
    const merged = mergeBuiltinRow(
      { name: 'meetings', origin: 'builtin', version: '2.0.0' },
      { version: '1.4.0', author: 'Nirav Adunuthula' },
    )
    expect(merged.version).toBe('1.4.0')
  })

  it('falls back to the catalog version when nothing is installed locally', () => {
    const merged = mergeBuiltinRow({ name: 'meetings', origin: 'builtin', version: '2.0.0' }, {})
    expect(merged.version).toBe('2.0.0')
  })

  it('never invents an author when neither source states one', () => {
    // The browse chain used to hardcode `|| 'kirocrew'`, asserting authorship no
    // source stated; the offline fallback carried the same literal. Both are gone.
    const merged = mergeBuiltinRow({ name: 'meetings', origin: 'builtin' }, {})
    expect(merged.author).toBe('')
  })
})
