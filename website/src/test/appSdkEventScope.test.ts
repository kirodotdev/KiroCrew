/**
 * Tests for the vendored SDK's WS subscription pre-check.
 *
 * The gateway (`kiro_crew/dashboard/ws_event_scope.py`) is authoritative; this
 * table is advisory. The last test cross-checks the two so drift is visible
 * here rather than as a wrong console warning in an app author's browser.
 */
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { describe, expect, it } from 'vitest'

import { checkSubscribeAllowed } from '../app-sdk'

describe('checkSubscribeAllowed', () => {
  it('treats tier 0 events as always delivered', () => {
    for (const e of ['dashboard', 'refresh', 'update_progress']) {
      expect(checkSubscribeAllowed(e, []).level).toBe('ok')
    }
  })

  it('reports own-slot-only for slot-scoped events with no slots scope', () => {
    const res = checkSubscribeAllowed('chat_chunk', [])
    expect(res.level).toBe('own-only')
    if (res.level === 'own-only') expect(res.hint).toContain('slots:user')
  })

  it('accepts slot-scoped events once any slots/subagent scope is declared', () => {
    expect(checkSubscribeAllowed('chat_chunk', ['slots:user']).level).toBe('ok')
    expect(checkSubscribeAllowed('subagent_status', ['subagent:all']).level).toBe('ok')
  })

  it('marks global events denied without the required scope, and ok with it', () => {
    const denied = checkSubscribeAllowed('artifact_update', [])
    expect(denied.level).toBe('denied')
    if (denied.level === 'denied') expect(denied.hint).toContain('artifacts')
    expect(checkSubscribeAllowed('artifact_update', ['artifacts']).level).toBe('ok')
  })

  it('accepts any variant of a scope family', () => {
    // The gateway picks between `notification` (own-app) and
    // `notification:system` (cron / send_message) from the payload's
    // source_app, which is not knowable at subscribe time.
    expect(checkSubscribeAllowed('notification', ['notification:all']).level).toBe('ok')
    expect(checkSubscribeAllowed('notification', ['notification:system']).level).toBe('ok')
    const denied = checkSubscribeAllowed('notification', [])
    expect(denied.level).toBe('denied')
    if (denied.level === 'denied') expect(denied.hint).toContain('notification:system')
  })

  it('accepts the wildcard declaration', () => {
    expect(checkSubscribeAllowed('anything_at_all', ['*']).level).toBe('ok')
  })

  it('needs no declaration for the slots list re-push', () => {
    // Payload is filtered per app server-side (_serialize_for_client).
    expect(checkSubscribeAllowed('slots', []).level).toBe('ok')
  })

  it('flags an unrecognised event as unknown, not denied', () => {
    // A distinct level matters: the console prefix for `denied` asserts the
    // manifest rejected it, which would be a false claim for a custom event.
    expect(checkSubscribeAllowed('chat_chunkk', ['slots:all']).level).toBe('unknown')
  })

  it('does not let a subagent scope widen non-subagent slot events', () => {
    // subagent:* is an independent dimension in the gate, so it must not
    // predict `ok` for chat -- the gateway still delivers own-slot chat only.
    expect(checkSubscribeAllowed('chat_message', ['subagent:all']).level).toBe('own-only')
    expect(checkSubscribeAllowed('subagent_status', ['subagent:all']).level).toBe('ok')
    // A slots:* scope widens both families.
    expect(checkSubscribeAllowed('subagent_status', ['slots:user']).level).toBe('ok')
  })

  it('matches the gateway tables in ws_event_scope.py', () => {
    // Drift between the advisory table and the authoritative gate produces
    // wrong developer warnings, so pin them together.
    const py = readFileSync(
      join(__dirname, '../../../src/kiro_crew/dashboard/ws_event_scope.py'),
      'utf-8',
    )
    const names = (block: string): string[] => {
      const m = py.match(new RegExp(`${block}[^{]*\\{([\\s\\S]*?)\\}\\)`))
      if (!m) throw new Error(`block ${block} not found in ws_event_scope.py`)
      // Strip `#` comment lines first: a quoted word inside a comment (a payload
      // shape, a cross-reference) is prose, not a member, and reading it as one
      // asserts the SDK classifies an event that does not exist.
      const body = m[1]
        .split('\n')
        .filter((line) => !line.trim().startsWith('#'))
        .join('\n')
      return [...body.matchAll(/"([^"]+)"/g)].map((x) => x[1])
    }
    // Scope the scan to the ONE table being mirrored. A whole-file regex for
    // `"key": "value",` matches every other 4-space str->str dict in the module
    // too (`_SUBAGENT_BATCH_ITEM_KEY` maps an event to its payload KEY, not to a
    // scope), which would assert the SDK grants `subagent_batch_update` on a
    // "scope" named `updates`. Anchored at line start so a PROSE mention of the
    // table name in a comment is not mistaken for its definition.
    const dictBlock = (block: string): string => {
      const m = py.match(new RegExp(`^${block}[^{\\n]*\\{([\\s\\S]*?)^\\}`, 'm'))
      if (!m) throw new Error(`dict block ${block} not found in ws_event_scope.py`)
      return m[1]
    }
    for (const e of names('_TIER0_ALWAYS')) {
      expect(checkSubscribeAllowed(e, []).level, `${e} should be tier 0`).toBe('ok')
    }
    for (const e of names('_SLOT_SCOPED_EVENTS')) {
      expect(
        checkSubscribeAllowed(e, []).level,
        `${e} should be slot-scoped in the SDK table`,
      ).toBe('own-only')
    }
    // `_SUBAGENT_EVENTS` is the one table the slot-scoped loop above cannot
    // stand in for. Its members are a SUBSET of `_SLOT_SCOPED_EVENTS`, so that
    // loop proves each is own-only by default -- but not that the SDK also
    // widens it under a `subagent:*` scope. Without this loop an event the
    // gateway delivers own-only can be predicted `ok` by the SDK, which is a
    // misleading developer hint rather than a data leak (the gateway is
    // authoritative), so assert the widening for every member by name.
    const subagentEvents = names('_SUBAGENT_EVENTS')
    expect(subagentEvents.length).toBeGreaterThan(3)
    for (const e of subagentEvents) {
      expect(
        checkSubscribeAllowed(e, ['subagent:all']).level,
        `${e} should widen to ok under subagent:all`,
      ).toBe('ok')
    }
    const globals = [
      ...dictBlock('_GLOBAL_EVENT_DECLARATIONS').matchAll(
        /^\s{4}"([a-z_.]+)": "([a-z_:]+)",/gm,
      ),
    ].map((m) => [m[1], m[2]])
    expect(globals.length).toBeGreaterThan(5)
    for (const [event, scope] of globals) {
      expect(
        checkSubscribeAllowed(event, [scope]).level,
        `${event} should be allowed by scope ${scope}`,
      ).toBe('ok')
      expect(checkSubscribeAllowed(event, []).level, `${event} should need a scope`).toBe(
        'denied',
      )
    }
  })
})
