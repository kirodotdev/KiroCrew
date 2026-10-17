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
  DECISIONS_MEMORY_POINT,
  readModelRoute,
  readPoints,
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
  // And the recalled-memory scope.
  memoryText: false,
  // And the wake-evidence scope.
  nudgeEvidence: false,
  // And the prior-conversation ceiling reads 0, which is the least that can leave.
  historyBudget: 0,
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
        // Nor to send the text of recalled memories.
        memoryText: false,
        // Nor the evidence a waiting loop is watching, which comes from OTHER
        // sessions and so is not covered by any yes about this one.
        nudgeEvidence: false,
        // Nor does it consent to prior turns: an unmentioned ceiling is 0.
        historyBudget: 0,
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
    expect(readConsent({ ...base, memory_text: true }).memoryText).toBe(true)
    // Independent fields: one granted must not read as the other.
    expect(readConsent({ ...base, tool_args: true }).memoryText).toBe(false)
    expect(readConsent({ ...base, memory_text: true }).toolArgs).toBe(false)
    // The wake-evidence scope reads on the same terms, and is independent of every
    // scope beside it: its rows come from sessions the loop WATCHES, so a yes about
    // the owner's own conversation cannot stand for it.
    expect(readConsent({ ...base, nudge_evidence: true }).nudgeEvidence).toBe(true)
    expect(readConsent({ ...base, nudge_evidence: true }).compaction).toBe(false)
    expect(readConsent({ ...base, compaction: true }).nudgeEvidence).toBe(false)
    expect(readConsent({ ...base, tool_args: true }).nudgeEvidence).toBe(false)
    for (const sloppy of [undefined, false, 'true', 1, 0, null, [], {}]) {
      expect(readConsent({ ...base, tool_args: sloppy }).toolArgs).toBe(false)
      expect(readConsent({ ...base, memory_text: sloppy }).memoryText).toBe(false)
      expect(readConsent({ ...base, nudge_evidence: sloppy }).nudgeEvidence).toBe(false)
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
    expect(readDecisions(undefined, config)).toEqual({
      ...OFF,
      bucket: null,
      historyBudget: 0,
      points: [],
    })
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
        memoryText: false,
        nudgeEvidence: false,
        // Absent from this payload, so the ceiling reads as 0 — the shipped
        // default, and the least that can leave the machine.
        historyBudget: 0,
        // A gateway that does not project its points reports none, and the card
        // says so rather than drawing a list written on this side.
        points: [],
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

  it('takes the history ceiling from the KEYSTONE, never from config.json', () => {
    // The two differ exactly when an agent has raised the config value, and the
    // gate honours the smaller one. Showing the config number would tell a reader
    // a budget is in force that the gate refuses.
    const body = {
      enabled: true,
      configured_endpoint: ENDPOINT,
      permits: true,
      history_budget_chars: 400,
    }
    const view = readDecisions(body, { decisions: { history_budget_chars: 99999 } })
    expect(view.historyBudget).toBe(400)
  })

  it('reads a ceiling the backend would refuse as 0, the fail-closed direction', () => {
    // 0 is a real answer here — it is the shipped default and means the message
    // alone — so an unusable value collapses onto the least, never onto "unknown".
    for (const bad of [-1, 12.5, '400', null, true, undefined]) {
      const body = {
        enabled: true,
        configured_endpoint: ENDPOINT,
        permits: true,
        history_budget_chars: bad,
      }
      expect(readDecisions(body, undefined).historyBudget).toBe(0)
    }
  })

  it('spells the constants the backend spells', () => {
    expect(DECISIONS_BUCKET_PATH).toBe('decisions.bucket')
    // The compaction point's identifier, which the card names and the card's record
    // dispatches on.
    expect(DECISIONS_COMPACTION_POINT).toBe('compaction.keep')
    // The recalled-memory point, which the memory strip's record dispatches on.
    expect(DECISIONS_MEMORY_POINT).toBe('memory.recall')
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
  const source = readFileSync(resolve(__dirname, 'DecisionsCard.tsx'), 'utf-8')

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


/**
 * The overview list is the GATEWAY's, and these are the properties that keep it so.
 *
 * A build that ships another point must light up a row with no edit on this side, so
 * the reader takes whatever ids the payload carries — including one it has no label
 * for. The two rejections are the ones that would otherwise put a row on screen that
 * cannot be acted on: a row with no id has nothing to label and no panel to open, and
 * a status this build cannot read must not be allowed to claim a point is running.
 *
 * The lane rides along for the same reason: on the one point with two lanes, only the
 * gateway knows which one would answer, so the reader carries the word it sent.
 */
describe('readPoints', () => {
  it('reads the rows the gateway sent, in its order, id and all', () => {
    expect(
      readPoints({
        enabled: true,
        points: [
          { id: 'skills.select', needs_scope: null, status: 'active' },
          { id: 'tool.risk', needs_scope: 'tool_args', status: 'needs_scope' },
        ],
      }),
    ).toEqual([
      { id: 'skills.select', lane: null, needsScope: null, status: 'active' },
      { id: 'tool.risk', lane: null, needsScope: 'tool_args', status: 'needs_scope' },
    ])
  })

  it('carries the lane the gateway named, so the chip never re-derives it', () => {
    const rows = readPoints({ points: [{ id: 'nudge.wake', status: 'active', lane: 'llm' }] })
    expect(rows).toEqual([
      { id: 'nudge.wake', lane: 'llm', needsScope: null, status: 'active' },
    ])
  })

  it('keeps a point this build has no label for, so a new one is never hidden', () => {
    const rows = readPoints({ points: [{ id: 'invented.point', status: 'active' }] })
    expect(rows).toEqual([
      { id: 'invented.point', lane: null, needsScope: null, status: 'active' },
    ])
  })

  it('reports none for a body that carries no rows at all', () => {
    for (const body of [undefined, {}, { points: null }, { points: 'yes' }, 'no']) {
      expect(readPoints(body)).toEqual([])
    }
  })

  it('drops a row with no id, and reads an unknown status as off', () => {
    const rows = readPoints({
      points: [{ status: 'active' }, { id: '', status: 'active' }, { id: 'a.b', status: 'invented' }],
    })
    // Only the one row with a usable id survives, and its unrecognised status is kept
    // verbatim: the chip resolves it to OFF, and rewriting it here would lose the word
    // a reader greps the log for.
    expect(rows).toEqual([
      { id: 'a.b', lane: null, needsScope: null, status: 'invented' },
    ])
  })
})

/**
 * `model.route`'s map and the prior-conversation budget, read off the ordinary config
 * GET. Both are config values rather than keystone ones because neither grants
 * anything: a tier can only name a model the provider already advertises, and the
 * budget is capped by the ceiling the owner recorded on the keystone.
 */
describe('readModelRoute', () => {
  it('always answers all three tiers, and an unnamed tier INHERITS', () => {
    expect(readModelRoute({ decisions: { model_route: { medium: 'some-model' } } })).toEqual({
      simple: '',
      medium: 'some-model',
      complex: '',
    })
  })

  it('inherits for a config that names nothing, and for one that is not an object', () => {
    for (const config of [undefined, {}, { decisions: {} }, { decisions: { model_route: 7 } }]) {
      expect(readModelRoute(config)).toEqual({ simple: '', medium: '', complex: '' })
    }
  })

  it('inherits for a tier whose value is not a string', () => {
    expect(readModelRoute({ decisions: { model_route: { simple: 42, complex: null } } })).toEqual({
      simple: '',
      medium: '',
      complex: '',
    })
  })
})
