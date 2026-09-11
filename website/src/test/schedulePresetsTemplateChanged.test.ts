import { describe, it, expect, beforeEach } from 'vitest'

import { SCHEDULE_PRESETS, templateUpdate, presetCanonicalPrompt } from '../utils/schedulePresets'
import { initI18n } from '../i18n'
import enManual from '../i18n/locales/en.manual.json'

const sample = SCHEDULE_PRESETS[0]

beforeEach(() => { initI18n('en') })

describe('presetCanonicalPrompt (locale-stable operand)', () => {
  it('every preset exposes a non-empty canonical prompt', () => {
    // Exercises each preset's canonicalMessage getter (and its siblings), so the
    // whole catalog's locale-stable operand is covered rather than one sample.
    for (const p of SCHEDULE_PRESETS) {
      expect(typeof p.title).toBe('string')
      expect(typeof p.description).toBe('string')
      expect(typeof p.prefill.message).toBe('string')
      const canon = p.prefill.canonicalMessage
      expect(typeof canon).toBe('string')
      expect((canon as string).length).toBeGreaterThan(0)
      expect(presetCanonicalPrompt(p.id)).toBe(canon)
    }
  })

  it('resolves to the English source string for the preset', () => {
    // The change-detection operand is pinned to English via i18next `{ lng }`,
    // so it does not follow the viewer's language. Asserting it equals the
    // English catalog value directly is the deterministic proof of that pin --
    // the jsdom i18n harness resolves every getter to English regardless of the
    // active language, so a runtime language switch cannot be exercised here;
    // the pin is what guarantees stability in the app, where all catalogs load.
    const enValue = (enManual as Record<string, Record<string, Record<string, string>>>)
      .utils.schedulePresets[`${sample.id.replace(/-/g, '_')}_message`]
    expect(presetCanonicalPrompt(sample.id)).toBe(enValue)
  })

  it('returns "" for an empty id', () => {
    expect(presetCanonicalPrompt('')).toBe('')
  })
})

describe('templateUpdate — attribution', () => {
  it('fires ONLY when the template moved (snapshot != current canonical prompt)', () => {
    const canonical = presetCanonicalPrompt(sample.id)
    const job = { source_preset: sample.id, source_template_prompt: canonical + ' (old template wording)' }
    const r = templateUpdate(job)
    expect(r).not.toBeNull()
    expect(r!.title).toBe(sample.title)
  })

  it('does NOT fire when the snapshot matches the current canonical prompt', () => {
    // Template unchanged. The job's own message is not read here, so a user's
    // edit to their copy cannot fire the hint.
    const job = { source_preset: sample.id, source_template_prompt: presetCanonicalPrompt(sample.id) }
    expect(templateUpdate(job)).toBeNull()
  })

  it('compares the canonical operand, not the job message', () => {
    // Detection must use presetCanonicalPrompt, not the (localized, editable)
    // display message. Snapshot == canonical -> no hint, whatever a divergent
    // display message might say.
    const job = { source_preset: sample.id, source_template_prompt: presetCanonicalPrompt(sample.id) }
    expect(templateUpdate(job)).toBeNull()
  })

  it('returns null when the job carries no source_preset', () => {
    expect(templateUpdate({ source_template_prompt: 'anything' })).toBeNull()
    expect(templateUpdate({ source_preset: '', source_template_prompt: 'anything' })).toBeNull()
    expect(templateUpdate({ source_preset: null, source_template_prompt: 'anything' })).toBeNull()
  })

  it('returns null for a retired template (id no longer in the catalog)', () => {
    expect(templateUpdate({ source_preset: 'retired-id', source_template_prompt: 'x' })).toBeNull()
  })
})
