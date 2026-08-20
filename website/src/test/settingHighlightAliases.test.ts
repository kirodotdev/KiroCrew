/**
 * resolveLegacyHighlightId — migrations for `?highlight=` deep-link ids.
 *
 * Registry ids are `<tab>.<kebab-label>`, so tab collapses and label renames
 * orphan old ids in bookmarks and palette history. The resolver maps them to
 * their current equivalents; unknown/current ids pass through unchanged.
 */
import { describe, it, expect } from 'vitest'
import { resolveLegacyHighlightId } from '../hooks/useSettingHighlight'
import { SETTINGS_REGISTRY } from '../components/commandPalette/settingsRegistry.gen'

describe('resolveLegacyHighlightId', () => {
  it('maps slack.* ids to the suffixed channels.* forms (tab collapse + label suffix)', () => {
    expect(resolveLegacyHighlightId('slack.slash-command')).toBe('channels.slash-command-slack')
    expect(resolveLegacyHighlightId('slack.owner-slack-member-id')).toBe('channels.owner-slack-member-id-slack')
  })

  it('maps pre-suffix channels.* ids (all SlackPanel rows) to the -slack forms', () => {
    expect(resolveLegacyHighlightId('channels.slash-command')).toBe('channels.slash-command-slack')
    // A suffixed (current) id passes through untouched.
    expect(resolveLegacyHighlightId('channels.folder-name-teams')).toBe('channels.folder-name-teams')
  })

  it('maps the four positional voice AWS ids to their qualified forms', () => {
    expect(resolveLegacyHighlightId('voice.aws-profile')).toBe('voice.aws-profile-transcribe')
    expect(resolveLegacyHighlightId('voice.aws-profile-2')).toBe('voice.aws-profile-amazon-polly')
    expect(resolveLegacyHighlightId('voice.aws-region')).toBe('voice.aws-region-transcribe')
    expect(resolveLegacyHighlightId('voice.aws-region-2')).toBe('voice.aws-region-amazon-polly')
  })

  // The qualifier itself was renamed once the label was corrected to the
  // service's real name, so the ids that shipped as `-polly` are legacy too.
  it('maps the short-form Polly ids to the Amazon Polly forms', () => {
    expect(resolveLegacyHighlightId('voice.aws-profile-polly')).toBe('voice.aws-profile-amazon-polly')
    expect(resolveLegacyHighlightId('voice.aws-region-polly')).toBe('voice.aws-region-amazon-polly')
  })

  it('passes current ids through unchanged', () => {
    expect(resolveLegacyHighlightId('chat.quick-send')).toBe('chat.quick-send')
    expect(resolveLegacyHighlightId('display.zoom-level')).toBe('display.zoom-level')
  })

  it('every migrated target exists in the generated registry', () => {
    const ids = new Set(SETTINGS_REGISTRY.map(e => e.id))
    for (const legacy of [
      'slack.slash-command', 'slack.owner-slack-member-id', 'slack.phase-reactions', 'slack.show-thinking',
      'voice.aws-profile', 'voice.aws-profile-2', 'voice.aws-region', 'voice.aws-region-2',
      'voice.aws-profile-polly', 'voice.aws-region-polly',
    ]) {
      const target = resolveLegacyHighlightId(legacy)
      expect(ids.has(target), `${legacy} -> ${target} missing from registry`).toBe(true)
    }
  })
})
