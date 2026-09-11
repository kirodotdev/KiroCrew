import { describe, it, expect } from 'vitest'
import * as fs from 'fs'
import * as path from 'path'
import { fileURLToPath } from 'url'
import { extractAll, generateAgentRegistryJson } from '../../../../scripts/settingsExtract'
import { SUBNAV_LEGACY_PARAMS, SUBNAV_PARAM } from '../../subNavParams'
import { SETTINGS_REGISTRY } from '../settingsRegistry.gen'

/**
 * Anti-stale guard for the settings registry.
 *
 * Runs the extractor in-memory over real source files and asserts the result
 * matches the checked-in generated registry. If this test fails:
 *
 *   run `npm run gen:settings` and commit the updated registry.
 *
 * Both artifacts of that one command are covered: the UI registry imported here,
 * and the agent-facing JSON bundled into the Python docs package. The second is
 * checked BYTE-FOR-BYTE rather than by shape, because it is what a deployed
 * gateway reads to answer "where is that setting?" — a stale copy hands the user
 * a link to a control that no longer exists, and nothing at runtime can tell.
 */

const __filename = fileURLToPath(import.meta.url)
const __dirname = path.dirname(__filename)
const SETTINGS_DIR = path.resolve(__dirname, '../../../pages/settings')
/** Repo root is five levels up: providers → commandPalette → components → src → website. */
const AGENT_REGISTRY_FILE = path.resolve(
  __dirname,
  '../../../../..',
  'src/kiro_crew/docs/settings-registry.generated.json',
)

// Valid tabs from SettingsPage.tsx (fork: KiroACP-only + de-Amazoned, so no
// provider/secretary/sync/tasks tabs).
const VALID_TABS = new Set([
  'overview', 'chat', 'voice', 'display', 'browser', 'skills', 'computer-use',
  'instances', 'security', 'secrets', 'notifications', 'channels', 'developer', 'about',
  'privacy', 'shortcuts',
])

describe('settingsRegistry.gen.ts — anti-stale guard', () => {
  it('checked-in registry matches live extraction (run `npm run gen:settings` if this fails)', () => {
    const { entries } = extractAll(SETTINGS_DIR)
    expect(entries).toEqual(SETTINGS_REGISTRY)
  })

  it('registry has at least 40 entries (minimum floor)', () => {
    expect(SETTINGS_REGISTRY.length).toBeGreaterThanOrEqual(40)
  })

  it('all registry entries have valid tab values', () => {
    for (const entry of SETTINGS_REGISTRY) {
      expect(VALID_TABS.has(entry.tab)).toBe(true)
    }
  })

  it('all registry entries have non-empty id and label', () => {
    for (const entry of SETTINGS_REGISTRY) {
      expect(entry.id.length).toBeGreaterThan(0)
      expect(entry.label.length).toBeGreaterThan(0)
    }
  })

  it('no duplicate ids', () => {
    const ids = SETTINGS_REGISTRY.map(e => e.id)
    expect(new Set(ids).size).toBe(ids.length)
  })
})

interface AgentEntry {
  id: string
  label: string
  tab: string
  route: string
  description?: string
  configKey?: string
}

/** The committed JSON as the agent reads it — parsed from disk, not regenerated,
 *  so these invariants hold for the bytes that actually ship. */
function agentEntries(): AgentEntry[] {
  return JSON.parse(fs.readFileSync(AGENT_REGISTRY_FILE, 'utf-8')).settings as AgentEntry[]
}

describe('settings-registry.generated.json — bundled agent registry', () => {
  it('checked-in JSON matches live extraction (run `npm run gen:settings` if this fails)', () => {
    const { entries } = extractAll(SETTINGS_DIR)
    expect(fs.readFileSync(AGENT_REGISTRY_FILE, 'utf-8')).toBe(generateAgentRegistryJson(entries))
  })

  it('every entry ships a route that opens its own tab', () => {
    for (const entry of agentEntries()) {
      const [, base, tab] = entry.route.split(/[/?]/)
      expect(base).toBe('settings')
      expect(tab).toBe(entry.tab)
    }
  })

  it('an entry needing a sub-selection carries it as a path segment', () => {
    // The one part of a route a reader cannot infer from `tab` + `id`. Pinned on
    // the UI registry's `params`, so a panel that gains a sub-selection fails
    // here rather than shipping links that open an empty tab.
    // Same key vocabulary and precedence as settingsRoute reads, imported rather
    // than restated: a fourth alias added there must not leave this green.
    const byId = new Map(agentEntries().map(e => [e.id, e]))
    for (const entry of SETTINGS_REGISTRY) {
      const params = entry.params ?? {}
      const sub = [SUBNAV_PARAM, ...SUBNAV_LEGACY_PARAMS].map(k => params[k]).find(v => v != null)
      if (sub == null) continue
      expect(byId.get(entry.id)?.route.split('?')[0]).toBe(`/settings/${entry.tab}/${sub}`)
    }
  })

  it('highlights by config key wherever the control exposes one', () => {
    // The `key:` form is a data-setting-key lookup and survives translation; the
    // id form resolves an English label against the DOM and cannot. Same rule
    // test_tips.py enforces for the curated tips' anchors.
    for (const entry of agentEntries()) {
      const highlight = decodeURIComponent(entry.route.split('highlight=')[1] ?? '')
      expect(highlight).toBe(entry.configKey ? `key:${entry.configKey}` : entry.id)
    }
  })

  it('exposes no field the frontend needs and the agent does not', () => {
    // A `labelKey` or `params` here invites a reader to rebuild the route itself
    // — the mistake the prebuilt `route` exists to remove.
    const allowed = new Set(['id', 'label', 'tab', 'route', 'description', 'configKey'])
    for (const entry of agentEntries()) {
      expect(Object.keys(entry).filter(k => !allowed.has(k))).toEqual([])
    }
  })
})
