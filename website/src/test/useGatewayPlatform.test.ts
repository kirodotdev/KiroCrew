import { describe, it, expect } from 'vitest'

import { classifyPlatform } from '../hooks/useGatewayPlatform'

// Pins reveal-open-gate-parity: the gateway's prerequisite snapshot reports a
// human DISPLAY label (`_platform_label` in kiro_prerequisite.py returns the
// capitalized "Windows"/"macOS"/"Linux"), NOT a raw `sys.platform` value. A
// lowercase-only `startsWith('win')` collapsed "Windows" to 'other', which made
// useCanOpenFile show the "Open with default app" row on a Windows gateway even
// though files.py degrades open→clipboard-copy there. classifyPlatform must
// case-fold so the ONE shared predicate classifies the label as 'windows' and
// every Windows-gated affordance (the FilePathMenu Open row) hides correctly.
describe('classifyPlatform', () => {
  it('classifies the backend "Windows" display label as windows', () => {
    expect(classifyPlatform('Windows')).toBe('windows')
  })

  it('classifies raw process.platform / sys.platform windows tokens as windows', () => {
    expect(classifyPlatform('win32')).toBe('windows')
    expect(classifyPlatform('windows')).toBe('windows')
    expect(classifyPlatform('WINDOWS')).toBe('windows')
  })

  it('classifies darwin as darwin', () => {
    expect(classifyPlatform('darwin')).toBe('darwin')
  })

  it('classifies the backend "macOS" display label as darwin', () => {
    // The sibling of the "Windows" case above, and the reason this whole
    // predicate case-folds: `_platform_label` publishes "macOS", so an exact
    // match on the raw `sys.platform` token alone sent every Mac gateway to
    // 'other' and offered the generic "Show in file manager" instead of Finder.
    expect(classifyPlatform('macOS')).toBe('darwin')
    expect(classifyPlatform('macos')).toBe('darwin')
    expect(classifyPlatform('MACOS')).toBe('darwin')
  })

  it('collapses an unrelated or unreadable label to other', () => {
    expect(classifyPlatform('Linux')).toBe('other')
    expect(classifyPlatform('linux')).toBe('other')
    expect(classifyPlatform('gateway')).toBe('other')
    expect(classifyPlatform('')).toBe('other')
    expect(classifyPlatform(undefined)).toBe('other')
    expect(classifyPlatform(null)).toBe('other')
  })

  it('does not treat a macOS label as windows', () => {
    expect(classifyPlatform('macOS')).not.toBe('windows')
  })
})
