/**
 * The remote-instance loading hint must name the settings screen that actually
 * exists — a drift guard for issue #7343.
 *
 * ## The defect this guard closes
 *
 * The loading tab shown while a remote instance connects tells the user where
 * to disconnect it: "… in Settings → Instances." That screen was renamed to
 * "Remote Instances" (#6950), but the hint kept the old name, so the
 * instruction pointed at a navigation entry that no longer exists. A plain
 * catalog-parity check cannot catch this: the key existed in every locale with
 * a well-formed value — the VALUE was simply stale relative to another key.
 *
 * The guard: in every shipped locale, the hint must contain `→ ` immediately
 * followed by that locale's actual instances tab label
 * (`settings.tabs.instances.label`, the string `SettingsPage.tsx` renders in
 * the nav rail). Anchoring at the arrow matters: a bare containment check is
 * one-directional — a rename that SHORTENS the label to a suffix of the stale
 * name (the exact reverse of #6950: "Remote Instances" → "Instances",
 * "Remote-Instanzen" → "Instanzen", "远程实例" → "实例") would still pass,
 * because the shorter label is a substring of the stale hint. `→ <label>`
 * fails that case in every locale, since the stale hint reads
 * `→ Remote Instances` and never `→ Instances`. (A rename to a strict PREFIX
 * of the old name remains uncovered; no realistic tab label is one.)
 *
 * A second assertion pins `LABEL_KEY` liveness: the nav rail in
 * `SettingsPage.tsx` must still resolve its instances tab label from that key,
 * so re-pointing the tab at a different key cannot leave this guard comparing
 * against a dead catalog entry.
 *
 * The generated pseudolocale is excluded: its values are accent-mangled and
 * bracket-wrapped per key, so cross-key containment does not survive the
 * transform. It regenerates from `en.json` anyway, so it cannot drift on its
 * own.
 */

import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { describe, it, expect } from 'vitest'

import { CATALOGS } from './catalogs'
import { SUPPORTED_LANGUAGES } from './languages'

const HINT_KEY = 'components.instancesViewport.this_tab_stays_until_you_disconnect_the_instance'
const LABEL_KEY = 'settings.tabs.instances.label'

const GENERATED = new Set(SUPPORTED_LANGUAGES.filter((l) => l.devOnly).map((l) => l.code))

/** Resolve a dotted key against a nested catalog object. */
function resolve(catalog: Record<string, unknown>, dotted: string): unknown {
  let node: unknown = catalog
  for (const part of dotted.split('.')) {
    if (typeof node !== 'object' || node === null) return undefined
    node = (node as Record<string, unknown>)[part]
  }
  return node
}

describe('remote-instance disconnect hint names the real settings screen (#7343)', () => {
  const shipped = Object.entries(CATALOGS).filter(([code]) => !GENERATED.has(code))

  it.each(shipped.map(([code]) => code))(
    "%s: the hint names that locale's instances tab label after the arrow",
    (code) => {
      const catalog = (CATALOGS[code] as { translation: Record<string, unknown> }).translation
      const hint = resolve(catalog, HINT_KEY)
      const label = resolve(catalog, LABEL_KEY)
      expect(typeof hint, `${code} is missing ${HINT_KEY}`).toBe('string')
      expect(typeof label, `${code} is missing ${LABEL_KEY}`).toBe('string')
      expect(
        (hint as string).includes(`→ ${label}`),
        `${code}: the disconnect hint must name the instances settings screen as the ` +
          `nav renders it ("→ ${label}"), got: "${hint}"`,
      ).toBe(true)
    },
  )

  it('the nav rail still labels the instances tab from LABEL_KEY', () => {
    // Guards the comparison target's liveness: if SettingsPage stops resolving
    // its instances tab label from LABEL_KEY, the per-locale assertions above
    // would keep passing against a dead catalog entry.
    const source = readFileSync(
      join(__dirname, '..', 'pages', 'SettingsPage.tsx'),
      'utf-8',
    )
    expect(source).toContain(`'${LABEL_KEY}'`)
  })
})
