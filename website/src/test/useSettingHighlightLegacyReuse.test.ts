import { describe, expect, it } from 'vitest'
import { resolveLegacyHighlightId } from '../hooks/useSettingHighlight'
import { SETTINGS_REGISTRY } from '../components/commandPalette/settingsRegistry.gen'

describe('resolveLegacyHighlightId against the real registry', () => {
  it('keeps a current id that a legacy alias once named', () => {
    // "Fallback Model" now labels the throttle-fallback row; a guide or link to
    // it must highlight that row, not the Default Model row it was renamed to.
    expect(SETTINGS_REGISTRY.some(e => e.id === 'chat.fallback-model')).toBe(true)
    expect(resolveLegacyHighlightId('chat.fallback-model')).toBe('chat.fallback-model')
  })

  it('still rewrites a legacy id that no current row uses', () => {
    expect(resolveLegacyHighlightId('voice.aws-profile')).toBe('voice.aws-profile-transcribe')
  })
})
