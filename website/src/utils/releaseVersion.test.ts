import { describe, it, expect } from 'vitest'
import { compareVersions, isNewSection } from './releaseVersion'

describe('compareVersions', () => {
  it('orders release cores numerically, not lexically', () => {
    expect(compareVersions('0.10.0', '0.9.0')).toBe(1)
    expect(compareVersions('0.9.0', '0.10.0')).toBe(-1)
    expect(compareVersions('1.0.0', '0.999.0')).toBe(1)
  })

  it('zero-pads the shorter core so 0.6 and 0.6.0 are the same release', () => {
    expect(compareVersions('0.6', '0.6.0')).toBe(0)
    expect(compareVersions('0.6.0.0', '0.6')).toBe(0)
  })

  it('ranks a release above every prerelease of itself', () => {
    expect(compareVersions('0.6.0', '0.6.0-rc.2')).toBe(1)
    expect(compareVersions('0.6.0rc4', '0.6.0')).toBe(-1)
    expect(compareVersions('0.6.0', '0.6.0.dev20260806065257')).toBe(1)
  })

  it('orders a prerelease line numerically past nine', () => {
    // A plain string compare says rc10 < rc9, and the insider line does reach
    // double digits.
    expect(compareVersions('0.2.0rc10', '0.2.0rc9')).toBe(1)
    expect(compareVersions('0.2.0rc8', '0.2.0rc9')).toBe(-1)
    expect(compareVersions('0.6.0-rc.10', '0.6.0-rc.2')).toBe(1)
  })

  it('reads the tag and wheel spellings of one prerelease as equal', () => {
    // `release.yml` maps `v0.6.0-rc.2` to the wheel version `0.6.0rc2`.
    expect(compareVersions('0.6.0-rc.2', '0.6.0rc2')).toBe(0)
  })

  it('ignores a build/local segment, which identifies a build and not a version', () => {
    expect(compareVersions('0.6.0+abc123', '0.6.0')).toBe(0)
    expect(compareVersions('0.6.0-rc.2+abc123', '0.6.0-rc.2')).toBe(0)
  })

  it('orders nightly stamps by their timestamp', () => {
    expect(
      compareVersions('0.6.0-nightly.20260806t065257', '0.6.0-nightly.20260805t065257'),
    ).toBe(1)
  })

  it('answers null when either side carries no numeric core', () => {
    // The caller treats null as "do not show this section": a spelling nobody
    // anticipated must produce NO notes rather than misplaced ones.
    expect(compareVersions('Unreleased', '0.6.0')).toBeNull()
    expect(compareVersions('0.6.0', '—')).toBeNull()
    expect(compareVersions('', '0.6.0')).toBeNull()
  })
})

describe('isNewSection', () => {
  it('keeps a section between the last seen version and the running build', () => {
    expect(isNewSection('0.4.0', '0.3.0', '0.4.0')).toBe(true)
  })

  it('refuses a release NEWER than the running build', () => {
    // The reported bug: a 0.6.0 dev build was shown [0.4.0] because the slice
    // had no upper bound. Any section above the running version is not in it.
    expect(isNewSection('0.7.0', '0.5.0', '0.6.0')).toBe(false)
  })

  it('refuses the whole file when the running build has no section of its own', () => {
    // `main` runs a minor ahead of the released line, so between releases NO
    // section qualifies — 0.4.0 is the last one written and 0.5.0/0.6.0 have
    // not shipped. This is the exact shape of the reported screenshot.
    expect(isNewSection('0.4.0', '0.5.0', '0.6.0')).toBe(false)
    expect(isNewSection('0.3.0', '0.5.0', '0.6.0')).toBe(false)
  })

  it('refuses a section the reader has already been shown', () => {
    expect(isNewSection('0.3.0', '0.3.0', '0.4.0')).toBe(false)
    expect(isNewSection('0.2.0', '0.3.0', '0.4.0')).toBe(false)
  })

  it('keeps an insider step inside one release line', () => {
    // rc8 -> rc9 is a real version change with real notes; folding both onto
    // 0.2.0 (what the Releases archive does) would suppress them.
    expect(isNewSection('0.2.0rc9', '0.2.0rc8', '0.2.0rc9')).toBe(true)
    expect(isNewSection('0.2.0rc9', '0.2.0rc9', '0.2.0rc9')).toBe(false)
  })

  it('keeps the stable section for a reader stepping off its own prerelease', () => {
    expect(isNewSection('0.6.0', '0.6.0rc2', '0.6.0')).toBe(true)
  })

  it('refuses an unorderable heading instead of guessing', () => {
    expect(isNewSection('Unreleased', '0.5.0', '0.6.0')).toBe(false)
  })
})
