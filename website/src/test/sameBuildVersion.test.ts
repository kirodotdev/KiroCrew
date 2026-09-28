// Contract under test — `sameBuildVersion`, the "is this one build stamped
// twice?" check behind the About hero's version lanes.
//
// The release pipeline stamps one build in two spellings: SemVer for the
// desktop shell, PEP 440 for the wheel the gateway runs from. Every pair below
// is lifted from the shapes `releaseVersion.ts` documents the pipeline emitting.
import { describe, it, expect } from 'vitest'
import { sameBuildVersion } from '../utils/displayVersion'

describe('sameBuildVersion', () => {
  it.each([
    ['0.6.0', '0.6.0'],
    ['0.6.0-insider.4', '0.6.0rc4'],           // insider desktop vs its wheel
    ['0.6.0-rc.2', '0.6.0rc2'],                // rc tag vs its wheel
    ['0.6.0-beta.4', '0.6.0rc4'],              // release.yml maps ANY -<label>.N tag to rcN
    ['0.5.0-beta-preview.1', '0.5.0rc1'],      // ...including a label with a hyphen inside it
    ['0.6.0-nightly.20260806t065257', '0.6.0.dev20260806065257'],
    ['0.6.0+abc123', '0.6.0'],                 // build segment is not a version
    ['0.6', '0.6.0'],                          // zero-padded core
    ['0.6.0-INSIDER.4', '0.6.0RC4'],           // case is not information
  ])('%s and %s are the same build', (a, b) => {
    expect(sameBuildVersion(a, b)).toBe(true)
    expect(sameBuildVersion(b, a)).toBe(true)
  })

  it.each([
    ['0.6.0-insider.6', '0.8.0'],              // the reported mismatch (#11356)
    ['0.6.0-insider.4', '0.6.0rc5'],           // adjacent RCs are different builds
    ['0.6.0-insider.4', '0.6.0'],              // a prerelease is not its release
    ['0.6.0-nightly.20260806t065257', '0.6.0.dev20260807065257'],
    ['0.6.0.dev20260806065257', '0.6.0rc4'],   // a nightly wheel is never a tagged prerelease
    ['0.6.0rc10', '0.6.0rc1'],                 // no prefix matching on digits
  ])('%s and %s are different builds', (a, b) => {
    expect(sameBuildVersion(a, b)).toBe(false)
    expect(sameBuildVersion(b, a)).toBe(false)
  })

  it('never reads an unparseable version as equal, even to itself', () => {
    expect(sameBuildVersion('', '')).toBe(false)
    expect(sameBuildVersion('unknown', 'unknown')).toBe(false)
    expect(sameBuildVersion('—', '0.6.0')).toBe(false)
  })
})
