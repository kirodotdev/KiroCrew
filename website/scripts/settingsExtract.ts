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
 * generated registry keeps holding real English text for command-palette
 * search. Its catalog key is retained separately so deep-link highlighting can
 * match the label rendered in the user's active locale.
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
import { SETTINGS_MANUAL } from '../src/components/commandPalette/settingsManual'
import type { ManualSettingEntry, SettingEntry, SettingPrimitiveType } from '../src/components/commandPalette/settingsTypes'

export type { SettingEntry, SettingPrimitiveType }

/** English catalogs, in load order (manual wins) — mirrors `src/i18n/enCatalog.ts`. */
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
 *  its panels need `channel=<key>`).
 *
 *  A panel may also map to SEVERAL targets. BotChannelPanel.tsx renders the
 *  same labels for multiple channels, so it emits one entry per channel per
 *  primitive. Each target's `labelSuffix` is appended to the label — and
 *  therefore the id — of entries that carry a labelKey, keeping the four
 *  otherwise-identical search results distinguishable. A literal-label entry
 *  keeps its label untouched: highlighting matches the RENDERED text via
 *  `data-setting-label`, and only a labelKey entry re-resolves the
 *  un-suffixed catalog string at highlight time (useSettingHighlight). */
type PanelTargetSingle = string | { tab: string; params: Record<string, string>; labelSuffix?: string }
type PanelTarget = PanelTargetSingle | PanelTargetSingle[]

/** Exported for the coverage gate (settingsCoverage.test.ts): a panel file
 *  that renders Settings* primitives but is absent from this map is silently
 *  dropped from search, so the gate cross-checks every panel file against it. */
export const PANEL_TAB_MAP: Record<string, PanelTarget> = {
  'OverviewPanel.tsx': 'overview',
  'ChatPanel.tsx': 'chat',
  'VoicePanel.tsx': 'voice',
  'DisplayPanel.tsx': 'display',
  'BrowserPanel.tsx': 'browser',
  'ComputerUsePanel.tsx': 'computer-use',
  'InstancesPanel.tsx': 'instances',
  'SecurityPanel.tsx': 'security',
  'NotificationsPanel.tsx': 'notifications',
  'ShortcutsPanel.tsx': 'shortcuts',
  // Added upstream (auto-skill generation) without a mapping here, so its two
  // settings were absent from command-palette search. Registered while
  // converting this file for i18n.
  'SkillsPanel.tsx': 'skills',
  // Privacy owns the two data-collection switches. Mapped so a SettingRef to
  // `telemetry.enabled` resolves to a deep link into the control rather than a
  // CLI command the user has to paste.
  'PrivacyPanel.tsx': 'privacy',
  // Every per-channel panel (standalone or fan-out) carries a labelSuffix:
  // it disambiguates same-label rows across channels ("Folder name (Teams)")
  // AND makes the derived ids order-stable — occurrence-numbered ids
  // (`channels.folder-name-2`) silently re-target a different channel's row
  // whenever a panel is added earlier in file order.
  'SlackPanel.tsx': { tab: 'channels', params: { channel: 'slack' }, labelSuffix: 'Slack' },
  'WebexPanel.tsx': { tab: 'channels', params: { channel: 'webex' }, labelSuffix: 'Webex' },
  // Standalone per-channel panels, mounted by ChannelsPanel behind their
  // channel= key exactly like WebexPanel.
  'TeamsPanel.tsx': { tab: 'channels', params: { channel: 'teams' }, labelSuffix: 'Teams' },
  'WeixinPanel.tsx': { tab: 'channels', params: { channel: 'weixin' }, labelSuffix: 'WeChat' },
  'IMessagePanel.tsx': { tab: 'channels', params: { channel: 'imessage' }, labelSuffix: 'iMessage' },
  'WhatsAppPanel.tsx': { tab: 'channels', params: { channel: 'whatsapp' }, labelSuffix: 'WhatsApp' },
  // The shared bot-token panel: one source file whose labels render for the
  // channels that mount it, so each extracted primitive fans out into one
  // entry per channel. Webex is deliberately absent: ChannelsPanel routes
  // channel=webex to the standalone WebexPanel (mapped above), so a webex
  // fan-out entry would deep-link to controls that panel never renders.
  'BotChannelPanel.tsx': [
    { tab: 'channels', params: { channel: 'discord' }, labelSuffix: 'Discord' },
    { tab: 'channels', params: { channel: 'telegram' }, labelSuffix: 'Telegram' },
    { tab: 'channels', params: { channel: 'wecom' }, labelSuffix: 'WeCom' },
  ],
  'DeveloperPanel.tsx': 'developer',
  'AboutPanel.tsx': 'about',
  'SttSettings.tsx': 'voice',
  // The `instances` tab mounts RemoteCrewPanel (SettingsPage.tsx), which also
  // renders InstancesPanel.tsx's AddInstanceForm — both files map to the same
  // tab so a primitive added to either lands on the right deep link.
  'RemoteCrewPanel.tsx': 'instances',
}

/** Map component name → our type enum. */
const PRIMITIVE_MAP: Record<string, SettingPrimitiveType> = {
  SettingsToggle: 'toggle',
  SettingsSelect: 'select',
  // A searchable dropdown is still a select to every consumer of this registry:
  // nothing branches on `SettingEntry.type`, so a distinct value would be surface
  // with no reader. Give it a distinct one only when something renders it apart.
  SettingsCombobox: 'select',
  SettingsInput: 'input',
  SettingsStepper: 'stepper',
  SettingsButtonGroup: 'buttonGroup',
  // Shared composite controls that behave as settings rows and carry a `label`
  // prop: a credential field with reveal/replace/clear affordances, and a
  // string-list editor. Both render `data-setting-label` on their root, so
  // deep-link highlighting works exactly like the Settings* primitives. To
  // every consumer of this registry they are inputs (nothing branches on the
  // distinction), so they map onto 'input' rather than minting reader-less
  // type values.
  SecretField: 'input',
  TagListEditor: 'input',
}

const PRIMITIVES = Object.keys(PRIMITIVE_MAP)

/** The extractable tag names, exported (like PANEL_TAB_MAP) for the coverage
 *  gate: a hand-copied list there would silently miss a ninth primitive added
 *  here — the exact drift class the gate exists to catch. */
export const EXTRACTABLE_PRIMITIVE_TAGS: readonly string[] = PRIMITIVES

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
  // `{{productName}}` is resolved to its stock default here, mirroring what
  // i18next's `defaultVariables` renders at runtime. The registry is a SEARCH
  // corpus: leaving the raw placeholder in would make a user's query for the
  // product name stop matching these entries. The literal deliberately
  // duplicates DEFAULT_PRODUCT_NAME in `src/i18n/index.ts`: importing that
  // module here would drag i18next and every catalog into a build-time
  // script for one constant.
  if (tMatch) return getEnCatalog()[tMatch[1]]?.replaceAll('{{productName}}', 'Kiro Crew')
  return undefined
}

/** Return the catalog key when a JSX prop is a supported translation call. */
function extractTranslationKeyProp(source: string, propName: string): string | undefined {
  const tCall = new RegExp(
    `${propName}=\\{\\s*(?:i18nT|t)\\(\\s*['"]([^'"]+)['"]\\s*\\)\\s*\\}`,
    'g',
  )
  const match = tCall.exec(source)
  if (!match) return undefined
  return getEnCatalog()[match[1]] === undefined ? undefined : match[1]
}

/**
 * Walk source from `start` (just after `<Component`) to find the complete
 * props string, respecting brace depth so `>` inside `{...}` (arrow fns,
 * ternaries) doesn't terminate the match early.
 *
 * Returns the props text (between the tag name and the closing `/>` or `>`),
 * or null if we can't find a valid close within a reasonable window.
 */
function extractJsxProps(source: string, start: number): string | 'ceiling' | 'unbalanced' | null {
  let depth = 0
  let inString: '"' | "'" | '`' | null = null
  const max = Math.min(source.length, start + 4000) // safety ceiling
  for (let i = start; i < max; i++) {
    const ch = source[i]
    // Handle string state: skip characters inside quoted strings
    if (inString !== null) {
      if (ch === '\\') {
        i++ // skip escaped character
        continue
      }
      if (ch === inString) {
        // Template literal: only exit if not an interpolation ${...}
        // (We don't need to track interpolation depth because we only care
        // about the template ending backtick at the top level of the string.)
        inString = null
      }
      continue
    }
    // Not inside a string — check for string openers
    if (ch === '"' || ch === "'" || ch === '`') {
      inString = ch
      continue
    }
    if (ch === '{') {
      depth++
    } else if (ch === '}') {
      depth--
      if (depth < 0) return 'unbalanced'
    } else if (ch === '>' && depth === 0) {
      // Found the real closing `>` (or `/>` — the `/` is part of the preceding text)
      return source.slice(start, i)
    }
  }
  // Distinguish: hit the 4000-char ceiling vs hit EOF with unbalanced braces
  if (depth !== 0) return 'unbalanced'
  return 'ceiling'
}

/**
 * Extract settings entries from a single TSX source string.
 */
export function extractFromSource(
  source: string,
  fileName: string,
): { entries: SettingEntry[]; skipped: number } {
  const mapped = PANEL_TAB_MAP[path.basename(fileName)]
  if (!mapped) return { entries: [], skipped: 0 }
  const targets = Array.isArray(mapped) ? mapped : [mapped]

  const entries: SettingEntry[] = []
  let skipped = 0

  for (const primitiveName of PRIMITIVES) {
    // Match the opening tag and capture all props until the self-closing `/>`
    // or closing `>`. The naive `[^>]*` breaks when props contain `>` inside
    // JSX expression braces (e.g. `onChange={v => f(v)}`). Instead, we find
    // the tag start and then walk forward respecting brace depth to locate the
    // true end of the opening tag.
    const tagStartRe = new RegExp(`<${primitiveName}\\b`, 'g')
    let tagStartMatch: RegExpExecArray | null
    while ((tagStartMatch = tagStartRe.exec(source)) !== null) {
      const propsStart = tagStartMatch.index + tagStartMatch[0].length
      const props = extractJsxProps(source, propsStart)
      if (props === 'ceiling' || props === 'unbalanced') {
        const snippet = source.slice(propsStart, propsStart + 60).replace(/\n/g, ' ')
        console.warn(
          `[settingsExtract] SKIP (${props}) in ${fileName}: <${primitiveName} ${snippet}…`,
        )
        skipped++
        continue
      }
      const label = extractStringProp(props, 'label')
      if (!label) {
        skipped++
        continue
      }
      const labelKey = extractTranslationKeyProp(props, 'label')
      const description = extractStringProp(props, 'description')
      const configKey = extractStringProp(props, 'configKey')
      for (const target of targets) {
        const tab = typeof target === 'string' ? target : target.tab
        const params = typeof target === 'string' ? undefined : target.params
        const suffix = typeof target === 'string' ? undefined : target.labelSuffix
        // Suffix only labelKey entries: highlighting resolves labelKey to the
        // rendered (un-suffixed) label, so DOM lookup still works. A literal
        // label IS the rendered text and must stay byte-identical. The raw
        // suffix rides along as `labelSuffix` so a localized display can
        // re-append it to the resolved (un-suffixed) catalog string.
        const displayLabel = suffix && labelKey ? `${label} (${suffix})` : label
        entries.push({
          id: '',
          label: displayLabel,
          ...(labelKey ? { labelKey } : {}),
          ...(suffix && labelKey ? { labelSuffix: suffix } : {}),
          description: description || undefined,
          tab,
          type: PRIMITIVE_MAP[primitiveName],
          occurrence: 1,
          ...(params ? { params } : {}),
          ...(configKey ? { configKey } : {}),
        })
      }
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

  // Manual entries merge AFTER id assignment so an override can address a
  // generated entry by its final id, and hand-assigned ids never participate
  // in the dedup counters above.
  allEntries = mergeManualEntries(allEntries, SETTINGS_MANUAL)

  // Sort deterministically
  allEntries.sort((a, b) => a.tab.localeCompare(b.tab) || a.label.localeCompare(b.label))

  return { entries: allEntries, skipped: totalSkipped }
}

/**
 * Merge hand-curated entries (settingsManual.ts) into the generated list.
 *
 * A manual entry whose id equals a generated id REPLACES that entry — the
 * escape hatch for attaching deep-link params the file-level PANEL_TAB_MAP
 * cannot scope (e.g. SecurityPanel's `?section=` sub-selection). Any other
 * manual entry is appended. Callers sort afterwards, so order here is
 * irrelevant to the final registry.
 *
 * Validation throws at generation time rather than shipping a broken registry:
 * a duplicate manual id would silently shadow one of the two entries, and an
 * unresolvable labelKey would make deep-link highlighting query the DOM for a
 * label no locale ever renders.
 */
export function mergeManualEntries(generated: SettingEntry[], manual: ManualSettingEntry[]): SettingEntry[] {
  const seen = new Set<string>()
  const cat = getEnCatalog()
  const resolved: SettingEntry[] = []
  for (const m of manual) {
    if (seen.has(m.id)) {
      throw new Error(`settingsManual: duplicate manual entry id '${m.id}' — every manual entry needs a unique id`)
    }
    seen.add(m.id)
    if (cat[m.labelKey] === undefined) {
      throw new Error(
        `settingsManual: labelKey '${m.labelKey}' (entry '${m.id}') does not resolve in the English catalogs — use a key that exists in en.json/en.manual.json`,
      )
    }
    if (m.descriptionKey && cat[m.descriptionKey] === undefined) {
      throw new Error(
        `settingsManual: descriptionKey '${m.descriptionKey}' (entry '${m.id}') does not resolve in the English catalogs — use a key that exists in en.json/en.manual.json`,
      )
    }
    // Manual entries are key-only (the i18n gate forbids English prose
    // literals in hand-written source); materialize the English strings here
    // so the generated registry stays a plain-English search corpus.
    const { descriptionKey, ...rest } = m
    resolved.push({
      ...rest,
      label: cat[m.labelKey],
      ...(descriptionKey ? { description: cat[descriptionKey] } : {}),
    })
  }
  const byId = new Map(resolved.map(m => [m.id, m]))
  const generatedIds = new Set(generated.map(g => g.id))
  const merged = generated.map(g => byId.get(g.id) ?? g)
  for (const m of resolved) {
    if (!generatedIds.has(m.id)) merged.push(m)
  }
  return merged
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
