/**
 * `readDecisions` and its two halves — the consent body and the config's bucket —
 * plus the two constants the card and the backend must agree on.
 *
 * Consent is a KEYSTONE, not a config path: nothing in `config.json` may read as
 * "on", and the toggle that writes it carries no `configKey` because there is no
 * config path for it to name.
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { describe, it, expect } from 'vitest'

import { SETTINGS_REGISTRY } from '../../components/commandPalette/settingsRegistry.gen'
import {
  DECISIONS_BUCKET_PATH,
  DECISIONS_LIVE_POINT,
  readBucket,
  readConsent,
  readDecisions,
  DECISIONS_COMPACTION_POINT,
} from './decisionsPreview'

const ENDPOINT = 'https://api.typesafe.ai/v1/systemone'
const OFF = {
  supported: false,
  enabled: false,
  configuredEndpoint: '',
  endpointMoved: false,
  // The tool-argument egress scope reads FALSE for an unreadable body, on the same
  // fail-closed terms as `enabled`: a body nobody could parse grants nothing.
  toolArgs: false,
  // And so does the whole-transcript scope, for the same reason.
  compaction: false,
}

describe('readConsent', () => {
  it('reads an absent or unresolved body as unsupported', () => {
    for (const value of [undefined, null, 'x', 7, [], {}]) {
      expect(readConsent(value)).toEqual(OFF)
    }
  })

  it('reads a literal true as on and anything else as off', () => {
    expect(readConsent({ enabled: true, configured_endpoint: ENDPOINT, permits: true }))
      .toEqual({
        supported: true,
        enabled: true,
        configuredEndpoint: ENDPOINT,
        endpointMoved: false,
        // Consent to SEND is not consent to send tool arguments: a body that does
        // not mention the scope grants none of it.
        toolArgs: false,
        // Nor is it consent to send a whole transcript, which is wider still.
        compaction: false,
      })
    for (const sloppy of [false, 'true', 1, null]) {
      expect(readConsent({ enabled: sloppy, configured_endpoint: ENDPOINT, permits: false }).enabled).toBe(false)
    }
  })

  it('reads the tool-argument scope as an exact true, like the switch itself', () => {
    // The field decides whether a NEW category of conversation content leaves the
    // machine, so a truthy stand-in is not a deliberate yes -- and an older gateway
    // omits it entirely, which must read as off rather than as unknown.
    const base = { enabled: true, configured_endpoint: ENDPOINT, permits: true }
    expect(readConsent({ ...base, tool_args: true }).toolArgs).toBe(true)
    for (const sloppy of [undefined, false, 'true', 1, 0, null, [], {}]) {
      expect(readConsent({ ...base, tool_args: sloppy }).toolArgs).toBe(false)
    }
  })

  it('flags a moved address from the server verdict, only while on', () => {
    // On, but the gate refuses: config.json names another address than consent was given for.
    expect(readConsent({ enabled: true, configured_endpoint: 'https://x.example', permits: false }).endpointMoved)
      .toBe(true)
    // Off is off; a stale recorded address is not a warning.
    expect(readConsent({ enabled: false, configured_endpoint: ENDPOINT, permits: false }).endpointMoved)
      .toBe(false)
  })
})

describe('readBucket', () => {
  it('keeps every whole number in range, 0 and 100 included', () => {
    expect(readBucket({ decisions: { bucket: 25 } })).toBe(25)
    // "On, and sampling nobody" is a state worth printing.
    expect(readBucket({ decisions: { bucket: 0 } })).toBe(0)
    // 100 is the shipped default and the share consent most needs to see.
    expect(readBucket({ decisions: { bucket: 100 } })).toBe(100)
  })

  it('reports nothing for an absent rate', () => {
    // Absent: an older section, or one an operator trimmed. The backend's
    // default decides, and this reader must not print a number it invented.
    expect(readBucket({ decisions: {} })).toBeNull()
    expect(readBucket({})).toBeNull()
    expect(readBucket(undefined)).toBeNull()
  })

  it('reports nothing for a rate the backend would clamp or refuse', () => {
    for (const bad of [-1, 101, 12.5, '25', null, true]) {
      expect(readBucket({ decisions: { bucket: bad } })).toBeNull()
    }
  })
})

describe('readDecisions', () => {
  it('is unsupported whenever consent is, whatever the config says', () => {
    // A shadow-era `preview: true`, or a hand-edited `enabled: true` in
    // config.json, is NOT consent: that file is agent-writable.
    const config = { decisions: { preview: true, enabled: true, bucket: 25 } }
    expect(readDecisions(undefined, config)).toEqual({ ...OFF, bucket: null })
  })

  it('combines the keystone and the config bucket', () => {
    const on = { enabled: true, configured_endpoint: ENDPOINT, permits: true }
    const off = { enabled: false, configured_endpoint: ENDPOINT, permits: false }
    expect(readDecisions(on, { decisions: { bucket: 25 } }))
      .toEqual({
        supported: true,
        enabled: true,
        configuredEndpoint: ENDPOINT,
        endpointMoved: false,
        bucket: 25,
        toolArgs: false,
        compaction: false,
      })
    expect(readDecisions(off, { decisions: { bucket: 100 } }).bucket).toBe(100)
    expect(readDecisions(off, undefined).bucket).toBeNull()
  })

  it('reads the whole-transcript scope as an exact true, and never off the narrower one', () => {
    // The widest of the three categories, so the same exactness applies -- and
    // `tool_args` must NOT grant it: that scope was reviewed as the arguments of the
    // one call about to run, not as everything the session has run.
    const base = { enabled: true, configured_endpoint: ENDPOINT, permits: true }
    expect(readConsent({ ...base, compaction: true }).compaction).toBe(true)
    expect(readConsent({ ...base, tool_args: true }).compaction).toBe(false)
    for (const sloppy of ['true', 1, 'yes', null, undefined]) {
      expect(readConsent({ ...base, compaction: sloppy }).compaction).toBe(false)
    }
  })

  it('spells the constants the backend spells', () => {
    expect(DECISIONS_BUCKET_PATH).toBe('decisions.bucket')
    // The compaction point's identifier, which the card names and the card's record
    // dispatches on.
    expect(DECISIONS_COMPACTION_POINT).toBe('compaction.keep')
    // Singular on purpose: `skills.dedupe` and `cron.novelty` shipped as rows in
    // the shadow release and are retired here, because a row for an answer
    // nothing consumes described a comparison rather than a thing being on.
    expect(DECISIONS_LIVE_POINT).toBe('skills.select')
  })
})

/**
 * The toggle writes a keystone, not a config path, so it must NOT carry a
 * `configKey`: one would name a config path nothing reads, which is exactly the
 * drift `test/test_settingref_schema_fixture.py` exists to catch. The registry
 * entry still exists (search deep-links reach the toggle by id + label) and
 * simply has no config key.
 */
describe('the Decisions toggle has no configKey', () => {
  // `__dirname`, not `import.meta.url`: under vitest the module URL is not a
  // file: URL, so `readFileSync` on it throws before any assertion runs.
  const source = readFileSync(resolve(__dirname, 'FeaturePreviewsSection.tsx'), 'utf-8')

  it('names no config path for the consent switch', () => {
    expect(source).not.toContain('configKey="decisions.enabled"')
    expect(source).not.toContain('configKey={DECISIONS_ENABLED_PATH}')
    expect(source).toContain('api.saveDecisionsConsent(')
  })

  it('reaches the generated registry without a config key', () => {
    const entry = SETTINGS_REGISTRY.find(
      e => e.labelKey === 'pages.developer.featurePreviewsTab.decisions',
    )
    expect(entry).toBeDefined()
    expect(entry?.configKey).toBeUndefined()
  })
})
