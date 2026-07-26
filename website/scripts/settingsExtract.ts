/**
 * Settings registry extractor (Search Everywhere — Settings provider).
 *
 * Parses `src/pages/settings/*.tsx` for JSX usages of settings primitives
 * (SettingsToggle, SettingsSelect, SettingsInput, SettingsStepper,
 * SettingsButtonGroup) and extracts label + description string literals +
 * primitive type.
 *
 * Used by:
 *  - `scripts/gen-settings-registry.mjs` (gen script)
 *  - vitest anti-stale guard test
 */

import * as fs from 'fs'
import * as path from 'path'
import type { SettingEntry, SettingPrimitiveType } from '../src/components/commandPalette/settingsTypes'

export type { SettingEntry, SettingPrimitiveType }

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
  'InstancesPanel.tsx': 'instances',
  'SecurityPanel.tsx': 'security',
  'NotificationsPanel.tsx': 'notifications',
  'SlackPanel.tsx': { tab: 'channels', params: { channel: 'slack' } },
  'GeneralPanel.tsx': 'developer',
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

/** Extract string literal from a JSX prop. */
function extractStringProp(source: string, propName: string): string | undefined {
  const patterns = [
    new RegExp(`${propName}="([^"]*)"`, 'g'),
    new RegExp(`${propName}=\\{'([^']*)'\\}`, 'g'),
    new RegExp(`${propName}=\\{"([^"]*)"\\}`, 'g'),
  ]
  for (const re of patterns) {
    const m = re.exec(source)
    if (m) return m[1]
  }
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
