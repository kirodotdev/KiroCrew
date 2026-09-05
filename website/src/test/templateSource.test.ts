import { describe, it, expect } from 'vitest'
import { templateSourceKind, templateSourceLabel, templateSourceBadge } from '../lib/templateSource'

describe('templateSourceKind', () => {
  it('treats a template Kiro Crew owns as built-in whatever source reports', () => {
    // The helper specs report source 'builtin' — the same value a hand-written
    // template gets — so the ownership flag is the only thing that separates them.
    expect(templateSourceKind({ source: 'builtin', kirocrew_owned: true })).toBe('builtin')
    expect(templateSourceKind({ source: 'kirocrew' })).toBe('builtin')
  })

  it('lets ownership outrank a package-shaped filename', () => {
    // `<package>-<name>.json` is a filename convention, so a package literally
    // named kirocrew would collide with a file Kiro Crew rewrites. Kiro Crew wins:
    // it is the one that will overwrite the file.
    expect(templateSourceKind({ source: 'package', package: 'kirocrew', kirocrew_owned: true }))
      .toBe('builtin')
  })

  it('falls back to custom rather than claiming an author', () => {
    expect(templateSourceKind({ source: 'builtin' })).toBe('custom')
    expect(templateSourceKind({})).toBe('custom')
  })

  it('only accepts a literal true for ownership', () => {
    // The out-of-tree edition seam passes this through from a raw row; a truthy
    // string must not become a provenance claim nothing verified.
    expect(templateSourceKind({ kirocrew_owned: 'yes' as unknown as boolean })).toBe('custom')
  })
})

describe('templateSourceLabel', () => {
  it('says nothing about a template that is not installed', () => {
    // A dangling reference to a removed template: the name still shows in the
    // dropdown, but nothing is known about where it came from.
    expect(templateSourceLabel(undefined)).toBe('')
  })

  it('keeps the category word for the source label', () => {
    expect(templateSourceLabel({ source: 'package', package: 'papyrus' })).toBe('Package')
  })
})

describe('templateSourceBadge', () => {
  it('names the actual package', () => {
    expect(templateSourceBadge({ source: 'package', package: 'papyrus' })).toBe('papyrus')
  })

  it('falls back to the category word when the package name is missing', () => {
    expect(templateSourceBadge({ source: 'package' })).toBe('Package')
  })

  it('shows the category word for non-package sources', () => {
    expect(templateSourceBadge({ source: 'kirocrew' })).toBe('Built-in')
    expect(templateSourceBadge({ source: 'builtin' })).toBe('Custom')
    expect(templateSourceBadge(undefined)).toBe('')
  })
})
