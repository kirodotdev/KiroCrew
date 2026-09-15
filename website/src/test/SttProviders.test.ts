// The STT provider vocabulary and the availability-reason vocabulary the UI
// shares with the backend.
//
// Both maps in `lib/sttProviders` fail SILENTLY when they drift, which is why they
// are pinned here rather than left to a screenshot: a provider missing from
// `PROVIDER_LABEL_KEY` renders as a bare wire id in the dropdown ("local"), and a
// code missing from `UNAVAILABLE_CODE_KEY` collapses four unrelated remedies
// (install a package, install a compiler, upgrade macOS, fetch a model) into the
// backend's raw English sentence.
//
// They are imported from the shipping module, not copied, so the test binds to the
// values the dashboard actually renders.

import { describe, it, expect } from 'vitest'

import { CATALOGS as RUNTIME_CATALOGS } from '../i18n/catalogs'
import { SUPPORTED_LANGUAGES } from '../i18n/languages'
import {
  CATALOG_MODEL_PROVIDERS,
  FALLBACK_PROVIDERS,
  FALLBACK_STREAMING_PROVIDERS,
  PROVIDER_APPLE,
  PROVIDER_LABEL_KEY,
  PROVIDER_LOCAL,
  PROVIDER_TRANSCRIBE,
  STREAM_ERROR_CODE_KEY,
  UNAVAILABLE_CODE_KEY,
  streamErrorMessage,
} from '../lib/sttProviders'
import EN_MANUAL from '../i18n/locales/en.manual.json'

const manualStt = (EN_MANUAL as { pages: { settings: { sttSettings: Record<string, string> } } })
  .pages.settings.sttSettings
const manualStreamErrors = (EN_MANUAL as { lib: { sttProviders: Record<string, string> } })
  .lib.sttProviders

/** A key's leaf name under `pages.settings.sttSettings`. */
const leaf = (key: string) => key.replace('pages.settings.sttSettings.', '')

describe('provider labels', () => {
  it('labels every provider the backend can advertise', () => {
    // `_VALID_STT_PROVIDERS` in the config loader, mirrored by hand because it is
    // Python. An id absent here falls back to the raw string, so the dropdown would
    // read "local". This list IS the mirror, so an omission is exactly the failure
    // it exists to catch — including a retired provider left behind, which would
    // keep offering a label for something the backend refuses.
    expect(Object.keys(PROVIDER_LABEL_KEY).sort()).toEqual(
      [PROVIDER_APPLE, PROVIDER_LOCAL, PROVIDER_TRANSCRIBE].sort(),
    )
  })

  it('has a catalog string behind each label key', () => {
    for (const [provider, key] of Object.entries(PROVIDER_LABEL_KEY)) {
      expect(manualStt[leaf(key)], provider).toBeTruthy()
    }
  })
})

describe('the served-list fallbacks', () => {
  it('always offers the provider that works on every host', () => {
    // `local` needs no platform support and no account, so a gateway that serves no
    // provider list must still offer it. Dropping it here would leave a fresh
    // install with only a paid off-host option.
    expect(FALLBACK_PROVIDERS).toContain(PROVIDER_LOCAL)
  })

  it('leaves the macOS-only provider out of the offered fallback', () => {
    // `apple` needs macOS 26+. Advertising it with no served list would offer a
    // provider that cannot start on most hosts.
    expect(FALLBACK_PROVIDERS).not.toContain(PROVIDER_APPLE)
  })

  it('assumes every provider streams when no capability list is served', () => {
    // All three stream. The served `streaming_providers` stays authoritative, so
    // this fallback only decides what an older gateway gets — and hiding the
    // toggle there is what made `apple`'s whole reason for existing unreachable.
    for (const p of [PROVIDER_LOCAL, PROVIDER_APPLE, PROVIDER_TRANSCRIBE]) {
      expect(FALLBACK_STREAMING_PROVIDERS).toContain(p)
    }
  })
})

describe('the model picker gate', () => {
  it('covers the provider whose model comes from the download catalog', () => {
    expect(CATALOG_MODEL_PROVIDERS).toContain(PROVIDER_LOCAL)
  })

  it('excludes the providers that have no model to choose', () => {
    // `apple` ships its model with the OS and `transcribe` runs the model on AWS,
    // so a picker for either would write a `stt.model` nothing reads.
    expect(CATALOG_MODEL_PROVIDERS).not.toContain(PROVIDER_APPLE)
    expect(CATALOG_MODEL_PROVIDERS).not.toContain(PROVIDER_TRANSCRIBE)
  })
})

describe('availability reasons', () => {
  it('names every code the backend can report', () => {
    // `kiro_crew.stt.engine`'s CODE_* constants plus `transcribe.py`'s own three,
    // mirrored by hand because they are Python. An unmapped code falls back to the
    // backend's untranslated `detail`.
    expect(Object.keys(UNAVAILABLE_CODE_KEY).sort()).toEqual([
      'stt_apple_needs_toolchain',
      'stt_apple_unsupported',
      'stt_disabled',
      'stt_extra_missing',
      'stt_import_failed',
      'stt_model_missing',
      'stt_no_wheel_for_platform',
    ])
  })

  it('gives each code its own message', () => {
    // One message per code is the entire point: "install the voice extra" and
    // "your platform has no prebuilt wheel" lead to different actions, so a shared
    // key would send half these users to the wrong remedy.
    const keys = Object.values(UNAVAILABLE_CODE_KEY)
    expect(new Set(keys).size).toBe(keys.length)
  })

  it('has a catalog string behind each code key', () => {
    for (const [code, key] of Object.entries(UNAVAILABLE_CODE_KEY)) {
      expect(manualStt[leaf(key)], code).toBeTruthy()
    }
  })
})

describe('the download prompt', () => {
  it('states the size before the click', () => {
    // The cost of the press is the one thing the user cannot discover afterwards:
    // the smallest model is 78 MB and the largest is 1.6 GB.
    expect(manualStt.model_download_prompt).toContain('{{size}}')
  })

  it('is translated into every shipped catalog', () => {
    // Read the catalogs exactly as the runtime composes them — including English's
    // generated (`en.json`) + manual (`en.manual.json`) merge. Globbing the locale
    // JSON directly would flag `en.json`, which is regenerated wholesale from
    // source scanning and legitimately never carries a hand-authored key.
    const seen: string[] = []
    for (const [code, bundle] of Object.entries(RUNTIME_CATALOGS)) {
      const root = (bundle as { translation: unknown }).translation as {
        pages?: { settings?: { sttSettings?: Record<string, string> } }
      }
      const value = root.pages?.settings?.sttSettings?.model_download_prompt
      // A missing key renders the dotted path into the UI, and this line is the
      // only warning a user gets before a multi-hundred-megabyte transfer.
      expect(value, code).toBeTruthy()
      expect(value, code).toContain('{{size}}')
      seen.push(code)
    }
    // Guard the guard: an empty catalog map would make the loop vacuously pass.
    expect(seen.length).toBeGreaterThanOrEqual(SUPPORTED_LANGUAGES.length)
  })
})

describe('a streaming session that failed', () => {
  /** A key's leaf name under `lib.sttProviders`. */
  const streamLeaf = (key: string) => key.replace('lib.sttProviders.', '')

  it('names every code the streaming socket can only report itself', () => {
    // `_CODE_MAX_DURATION` and `_CODE_SESSION_FAILED` in `dashboard/stt_stream.py`,
    // `CODE_DECODE_FAILED` in `stt/engine.py`, plus `stt_model_missing`, which the
    // socket also sends and which needs different words below the composer than in
    // the settings panel. An omission renders the backend's English sentence.
    expect(Object.keys(STREAM_ERROR_CODE_KEY).sort()).toEqual([
      'stt_decode_failed',
      'stt_max_duration_exceeded',
      'stt_model_missing',
      'stt_session_failed',
    ])
  })

  it('has a catalog string behind each code key', () => {
    for (const [code, key] of Object.entries(STREAM_ERROR_CODE_KEY)) {
      expect(manualStreamErrors[streamLeaf(key)], code).toBeTruthy()
    }
  })

  it('gives each code its own message', () => {
    // "Retry" and "wait for a download" are unrelated actions; one shared key would
    // send half of these users to the wrong one.
    const keys = Object.values(STREAM_ERROR_CODE_KEY)
    expect(new Set(keys).size).toBe(keys.length)
  })

  it('resolves a stream code to its own key, not the settings panel copy', () => {
    // `stt_model_missing` is in BOTH tables. The stream table has to win: the
    // settings copy says "Download it below", and below the composer there is no
    // "below".
    expect(streamErrorMessage('stt_decode_failed')).toBe(
      manualStreamErrors[streamLeaf(STREAM_ERROR_CODE_KEY.stt_decode_failed)],
    )
    const modelMissing = streamErrorMessage('stt_model_missing')
    expect(modelMissing).toBe(
      manualStreamErrors[streamLeaf(STREAM_ERROR_CODE_KEY.stt_model_missing)],
    )
    expect(modelMissing).not.toBe(manualStt.unavailable_model_missing)
  })

  it('falls back to the availability vocabulary for a setup failure', () => {
    // A missing extra or an unusable wheel travels through the socket unchanged, and
    // its remedy is the same sentence the settings panel already renders — so it is
    // localised here too rather than dropping to the server's English.
    expect(streamErrorMessage('stt_extra_missing', 'raw english')).toBe(
      manualStt.unavailable_extra_missing,
    )
  })

  it('keeps the server sentence for a code this build does not know', () => {
    // A newer gateway can name a reason this bundle has no key for. Its own prose is
    // still actionable; a generic "unavailable" would throw away the only
    // information there was.
    expect(streamErrorMessage('stt_something_newer', 'the recogniser exploded')).toBe(
      'the recogniser exploded',
    )
  })

  it('reports nothing rather than an empty sentence when it knows nothing', () => {
    // The caller supplies the generic last resort, so an empty answer has to be
    // falsy for that `||` to reach it.
    expect(streamErrorMessage('', '')).toBe('')
  })
})
