/**
 * Settings registry extractor (Search Everywhere — Settings provider).
 *
 * Parses `src/pages/settings/*.tsx` for JSX usages of settings primitives
 * (SettingsToggle, SettingsSelect, SettingsInput, SettingsStepper,
 * SettingsButtonGroup) and extracts label + description + primitive type.
 *
 * A label/description is read from EITHER form:
 *   - a string literal          `label="Zoom Level"`
 *   - a translation call        `label={t('settings.display.language.label')}`
 *
 * The `t()` form is resolved against `src/i18n/locales/en.json`, so the
 * generated registry keeps holding real English text. This matters because the
 * registry powers command-palette settings SEARCH: if a translated label were
 * skipped (as a plain "dynamic label" is), that setting would silently vanish
 * from search — the control still renders, but nobody can find it. Resolving
 * the key instead of skipping it is what keeps i18n conversion invisible to the
 * search index.
 *
 * Search is indexed in English on purpose: `SETTINGS_REGISTRY` is generated at
 * build time and cannot vary per user language. Localizing it would mean
 * generating one registry per language and selecting at runtime — deliberately
 * out of scope, and tracked as a follow-up rather than half-built here.
 *
 * Used by:
 *  - `scripts/gen-settings-registry.mjs` (gen script)
 *  - vitest anti-stale guard test
 */

import * as fs from 'fs'
import * as path from 'path'
import type { SettingEntry, SettingPrimitiveType } from '../src/components/commandPalette/settingsTypes'

export type { SettingEntry, SettingPrimitiveType }

/** English catalogs, in load order (manual wins) — mirrors `src/i18n/index.ts`. */
const EN_CATALOG_PATHS = [
  path.resolve(__dirname, '../src/i18n/locales/en.json'),
  path.resolve(__dirname, '../src/i18n/locales/en.manual.json'),
]

/** Flatten a nested catalog into dotted leaf paths, matching i18next lookup. */
function flattenCatalog(obj: unknown, prefix = ''): Record<string, string> {
  const out: Record<string, string> = {}
  if (obj === null || typeof obj !== 'object') return out
  for (const [key, value] of Object.entries(obj as Record<string, unknown>)) {
    const dotted = prefix ? `${prefix}.${key}` : key
    if (value !== null && typeof value === 'object') {
      Object.assign(out, flattenCatalog(value, dotted))
    } else {
      out[dotted] = String(value)
    }
  }
  return out
}

/** Lazily-read, memoized English catalog (flattened). */
let enCatalog: Record<string, string> | null = null

function getEnCatalog(): Record<string, string> {
  if (enCatalog === null) {
    const merged: Record<string, string> = {}
    for (const file of EN_CATALOG_PATHS) {
      try {
        Object.assign(merged, flattenCatalog(JSON.parse(fs.readFileSync(file, 'utf-8'))))
      } catch {
        // Missing/malformed catalog: degrade to literal-only extraction rather
        // than failing the build. A genuinely missing key is caught by the
        // anti-stale guard test, which compares against the committed registry.
      }
    }
    enCatalog = merged
  }
  return enCatalog
}

/** Reset the memoized catalog. Test-only seam. */
export function __resetCatalogCache(): void {
  enCatalog = null
}

/** Panel file → tab key mapping (derived from SettingsPage.tsx switch).
 *  Only panels that actually render inside a Settings tab are mapped — the
 *  fork is KiroACP-only and de-Amazoned, so upstream's Provider / Secretary /
 *  Sync / TaskKeeper panels are absent, and SharedMcpGatewayToggle /
 *  McpPoolableServers live on the standalone Developer page (not a Settings
 *  tab), so they are intentionally excluded to avoid dead deep-links.
 *
 *  Entries may carry `params` — extra query params the deep link needs for
 *  the panel to actually mount (the Channels tab is a list-detail view, so
 *  its panels need `channel=<key>`). BotChannelPanel.tsx is intentionally
 *  UNMAPPED: its labels render for four different channels (Discord,
 *  Telegram, Webex, WeCom), so a registry entry would be ambiguous — there
 *  is no single `channel` value to attach. */
type PanelTarget = string | { tab: string; params: Record<string, string> }

const PANEL_TAB_MAP: Record<string, PanelTarget> = {
  'OverviewPanel.tsx': 'overview',
  'ChatPanel.tsx': 'chat',
  'VoicePanel.tsx': 'voice',
  'DisplayPanel.tsx': 'display',
  'BrowserPanel.tsx': 'browser',
  'ComputerUsePanel.tsx': 'computer-use',
  'InstancesPanel.tsx': 'instances',
  'SecurityPanel.tsx': 'security',
  'NotificationsPanel.tsx': 'notifications',
  // Added upstream (auto-skill generation) without a mapping here, so its two
  // settings were absent from command-palette search. Registered while
  // converting this file for i18n.
  'SkillsPanel.tsx': 'skills',
  'SlackPanel.tsx': { tab: 'channels', params: { channel: 'slack' } },
  'DeveloperPanel.tsx': 'developer',
  'AboutPanel.tsx': 'about',
  'SttSettings.tsx': 'voice',
}

/** Map component name → our type enum. */
const PRIMITIVE_MAP: Record<string, SettingPrimitiveType> = {
  SettingsToggle: 'toggle',
  SettingsSelect: 'select',
  SettingsInput: 'input',
  SettingsStepper: 'stepper',
  SettingsButtonGroup: 'buttonGroup',
}

const PRIMITIVES = Object.keys(PRIMITIVE_MAP)

/** Convert a label to a kebab-case id segment. */
function toKebab(s: string): string {
  return s
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-|-$/g, '')
}

/**
 * Extract a JSX prop's text, from a literal or a translation call.
 *
 * Literal forms:  `p="X"`, `p={'X'}`, `p={"X"}`
 * Translated:     `p={i18nT('key')}`, `p={t('key')}` (either helper)
 *
 * Both call names are accepted: converted files use the codemod's collision-free
 * `i18nT`, while code written by hand inside a component may use
 * `useTranslation()`'s `t`. Matching only one silently drops every setting using
 * the other — which is exactly how this regression happened (the extractor knew
 * `t(` while the codemod emitted `i18nT(`, and the registry generated 0 entries,
 * taking ALL of settings search down).
 *
 * A key absent from the catalog returns `undefined` (treated as dynamic and
 * skipped) rather than emitting the raw key as a searchable label — a search hit
 * reading `settings.display.language.label` would be worse than the setting
 * simply not being indexed.
 */
function extractStringProp(source: string, propName: string): string | undefined {
  const literalPatterns = [
    new RegExp(`${propName}="([^"]*)"`, 'g'),
    new RegExp(`${propName}=\\{'([^']*)'\\}`, 'g'),
    new RegExp(`${propName}=\\{"([^"]*)"\\}`, 'g'),
  ]
  for (const re of literalPatterns) {
    const m = re.exec(source)
    if (m) return m[1]
  }
  // `i18nT('key')` / `t("key")` — either helper, optional inner whitespace.
  const tCall = new RegExp(
    `${propName}=\\{\\s*(?:i18nT|t)\\(\\s*['"]([^'"]+)['"]\\s*\\)\\s*\\}`,
    'g',
  )
  const tMatch = tCall.exec(source)
  if (tMatch) return getEnCatalog()[tMatch[1]]
  return undefined
}

/**
 * Extract settings entries from a single TSX source string.
 */
export function extractFromSource(
  source: string,
  fileName: string,
): { entries: SettingEntry[]; skipped: number } {
  const target = PANEL_TAB_MAP[path.basename(fileName)]
  if (!target) return { entries: [], skipped: 0 }
  const tab = typeof target === 'string' ? target : target.tab
  const params = typeof target === 'string' ? undefined : target.params

  const entries: SettingEntry[] = []
  let skipped = 0

  for (const primitiveName of PRIMITIVES) {
    const tagRe = new RegExp(`<${primitiveName}\\b([^>]*(?:\\n[^>]*)*)/?\\s*>`, 'g')
    let tagMatch: RegExpExecArray | null
    while ((tagMatch = tagRe.exec(source)) !== null) {
      const props = tagMatch[1]
      const label = extractStringProp(props, 'label')
      if (!label) {
        skipped++
        continue
      }
      const description = extractStringProp(props, 'description')
      entries.push({
        id: '',
        label,
        description: description || undefined,
        tab,
        type: PRIMITIVE_MAP[primitiveName],
        occurrence: 1,
        ...(params ? { params } : {}),
      })
    }
  }

  return { entries, skipped }
}

/**
 * Extract settings from all panel files in the given directory.
 * Files are sorted alphabetically before processing so dedup suffix assignment
 * is deterministic cross-platform (Fix #3: readdirSync order is OS-dependent).
 */
export function extractAll(settingsDir: string): { entries: SettingEntry[]; skipped: number } {
  const files = fs.readdirSync(settingsDir).filter(f => f.endsWith('.tsx')).sort()
  let allEntries: SettingEntry[] = []
  let totalSkipped = 0

  for (const file of files) {
    const source = fs.readFileSync(path.join(settingsDir, file), 'utf-8')
    const { entries, skipped } = extractFromSource(source, file)
    allEntries = allEntries.concat(entries)
    totalSkipped += skipped
  }

  // Assign ids with dedup and explicit occurrence field.
  // Occurrence order matches JSX source order within a panel (alphabetical file
  // iteration + top-to-bottom regex scan within each file).
  const idCounts = new Map<string, number>()
  for (const entry of allEntries) {
    const base = `${entry.tab}.${toKebab(entry.label)}`
    const count = (idCounts.get(base) ?? 0) + 1
    idCounts.set(base, count)
    entry.id = count === 1 ? base : `${base}-${count}`
    entry.occurrence = count
  }

  // Sort deterministically
  allEntries.sort((a, b) => a.tab.localeCompare(b.tab) || a.label.localeCompare(b.label))

  return { entries: allEntries, skipped: totalSkipped }
}

/**
 * Generate the TypeScript source for settingsRegistry.gen.ts.
 */
export function generateRegistrySource(entries: SettingEntry[]): string {
  const lines = [
    '// AUTO-GENERATED by scripts/gen-settings-registry.mjs — DO NOT EDIT',
    '// Re-generate with: npm run gen:settings',
    '',
    "import type { SettingEntry } from './settingsTypes'",
    '',
    'export const SETTINGS_REGISTRY: SettingEntry[] = ',
    JSON.stringify(entries, null, 2),
    '',
  ]
  return lines.join('\n')
}
