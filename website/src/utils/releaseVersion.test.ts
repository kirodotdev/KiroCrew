import { describe, it, expect } from 'vitest'
import { baseRelease, compareVersions, isNewSection, notesPredateRunningRelease } from './releaseVersion'

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

  it('keeps the release section on a build that is a prerelease of it', () => {
    // Reported defect (#14705): running 0.8.0-insider.1 after 0.7.1-insider, the
    // [0.8.0] section was SUPPRESSED because a release outranks its own
    // prerelease (0.8.0 > 0.8.0-insider.1), leaving [0.7.1] as the newest
    // qualifying section. The relaxed upper bound admits the same-core release.
    expect(isNewSection('0.8.0', '0.7.1-insider.1', '0.8.0-insider.1')).toBe(true)
    // Generalised, and with the wheel spelling of the prerelease tail.
    expect(isNewSection('0.2.0', '0.1.0insider2', '0.2.0insider1')).toBe(true)
  })

  it('still refuses a FUTURE release on a prerelease build', () => {
    // The relaxation is scoped to the SAME core: a genuinely newer release the
    // build has not reached differs in core, so section <= running still governs.
    expect(isNewSection('0.9.0', '0.7.1', '0.8.0-insider.1')).toBe(false)
    expect(isNewSection('0.9.0-insider.1', '0.7.1', '0.8.0-insider.1')).toBe(false)
  })

  it('refuses a NEWER prerelease of the same release on a prerelease build', () => {
    // Only the release itself (no tail) is admitted above the running build; a
    // later draft of the same line is still a build the reader does not have.
    expect(isNewSection('0.8.0-insider.3', '0.7.0', '0.8.0-insider.1')).toBe(false)
    expect(isNewSection('0.2.0rc9', '0.1.0', '0.2.0rc8')).toBe(false)
  })

  it('opens the release section once per line, not on every prerelease step', () => {
    // insider.1 -> insider.2: the reader was already on 0.8.0 and has had its
    // notes, so stepping within the line does not re-open them.
    expect(isNewSection('0.8.0', '0.8.0-insider.1', '0.8.0-insider.2')).toBe(false)
    expect(isNewSection('0.2.0', '0.2.0rc1', '0.2.0rc2')).toBe(false)
    // Stepping in from an earlier line still opens them.
    expect(isNewSection('0.8.0', '0.7.1', '0.8.0-insider.2')).toBe(true)
    // And reaching the release itself is unaffected (section <= running path).
    expect(isNewSection('0.8.0', '0.8.0-insider.2', '0.8.0')).toBe(true)
  })

  it('refuses an unorderable heading instead of guessing', () => {
    expect(isNewSection('Unreleased', '0.5.0', '0.6.0')).toBe(false)
  })
})

describe('baseRelease', () => {
  it('drops every prerelease, dev and build spelling the pipeline emits', () => {
    expect(baseRelease('0.8.0')).toBe('0.8.0')
    expect(baseRelease('0.8.0-insider.1')).toBe('0.8.0')
    expect(baseRelease('0.8.0-rc.2')).toBe('0.8.0')
    expect(baseRelease('0.8.0rc4')).toBe('0.8.0')
    expect(baseRelease('0.8.0.dev20260806065257')).toBe('0.8.0')
    expect(baseRelease('0.8.0-nightly.20260806t065257')).toBe('0.8.0')
    expect(baseRelease('0.8.0+abc123')).toBe('0.8.0')
    expect(baseRelease(' 0.10.1 ')).toBe('0.10.1')
  })

  it('folds a fourth numeric segment, a build number within the release', () => {
    expect(baseRelease('0.7.1.5')).toBe('0.7.1')
    expect(baseRelease('0.7.1.5-insider.1')).toBe('0.7.1')
  })

  it('returns null when there is no numeric core', () => {
    expect(baseRelease('Unreleased')).toBeNull()
    expect(baseRelease('')).toBeNull()
  })
})

describe('notesPredateRunningRelease', () => {
  it('flags an older release\'s notes shown to a prerelease of the next one', () => {
    // The reported case: 0.8.0-insider.1 shown the 0.7.1 section.
    expect(notesPredateRunningRelease('0.7.1', '0.8.0-insider.1')).toBe(true)
    expect(notesPredateRunningRelease('0.7.1', '0.8.0.dev20260806065257')).toBe(true)
    expect(notesPredateRunningRelease('0.7.1', '0.8.0')).toBe(true)
  })

  it('does not flag notes that belong to the running release', () => {
    expect(notesPredateRunningRelease('0.7.1', '0.7.1')).toBe(false)
    expect(notesPredateRunningRelease('0.7.1', '0.7.1+local')).toBe(false)
    // A prerelease section of the running line is this release's own notes.
    expect(notesPredateRunningRelease('0.8.0rc2', '0.8.0-rc.3')).toBe(false)
    expect(notesPredateRunningRelease('0.8.0', '0.8.0rc2')).toBe(false)
    // A four-segment build is still its three-segment release.
    expect(notesPredateRunningRelease('0.7.1', '0.7.1.5')).toBe(false)
  })

  it('makes no claim about versions it cannot place', () => {
    expect(notesPredateRunningRelease('Unreleased', '0.8.0')).toBe(false)
    expect(notesPredateRunningRelease('0.7.1', '—')).toBe(false)
  })
})
