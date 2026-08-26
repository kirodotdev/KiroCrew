import { describe, it, expect } from 'vitest'
import * as fs from 'fs'
import * as path from 'path'

import { extractFromSource, EXTRACTABLE_PRIMITIVE_TAGS, PANEL_TAB_MAP } from '../../scripts/settingsExtract'
import { SETTINGS_MANUAL } from '../components/commandPalette/settingsManual'

/**
 * Settings-search coverage gate.
 *
 * The registry behind Search Everywhere's Settings tab is EXTRACTED from panel
 * source (scripts/settingsExtract.ts), and the extractor is blind in three
 * specific ways. Each blindness once cost real coverage silently — a panel's
 * settings simply never appeared in search, and nothing anywhere went red:
 *
 *  1. A panel file absent from PANEL_TAB_MAP is skipped wholesale.
 *     (WhatsAppPanel shipped six primitives that were dropped this way.)
 *  2. A bare control (Toggle / Input / SimpleSelect / native input…) instead
 *     of a Settings* primitive is invisible to the extractor.
 *     (SecurityPanel grew to 2300+ lines with one extractable primitive.)
 *  3. A primitive whose label is a dynamic expression — not a string literal
 *     or a literal-key i18nT()/t() call — is silently counted as "skipped".
 *     (NotificationsPanel's per-category sound selects.)
 *
 * This gate turns all three silences into failures with an explicit,
 * per-instance accounting. Adding a control to a settings panel now requires
 * one of: using a Settings* primitive (auto-indexed), adding a
 * settingsManual.ts entry, or updating the accounting below with a reason a
 * reviewer can judge. Counts are pinned exactly (===): an unexplained increase
 * is a coverage regression, and a decrease means the accounting is stale.
 *
 * Residual path this gate does NOT close: a NEW shared composite control
 * defined under components/ (TagListEditor's own history) matches neither the
 * extractor's primitive list nor BARE_CONTROL_TAGS below, so its settings are
 * unindexed with no red until someone teaches the extractor the tag AND adds
 * it to BARE_CONTROL_TAGS. When introducing a shared labeled control, do both.
 */

const SETTINGS_DIR = path.resolve(__dirname, '../pages/settings')

/** Panel sources (tests excluded), sorted for stable failure output. */
function panelFiles(): string[] {
  return fs
    .readdirSync(SETTINGS_DIR)
    .filter(f => f.endsWith('.tsx') && !f.includes('.test.'))
    .sort()
}

function readPanel(file: string): string {
  return fs.readFileSync(path.join(SETTINGS_DIR, file), 'utf-8')
}

/* ────────────────────────────────────────────────────────────────────────── */
/* 1. PANEL_TAB_MAP completeness                                              */
/* ────────────────────────────────────────────────────────────────────────── */

/**
 * Panel files deliberately NOT in PANEL_TAB_MAP, each with the reason a
 * reviewer needs. A new panel file must land either in the map or here —
 * an unlisted file fails, so the choice is always conscious.
 */
const UNMAPPED_PANELS: Record<string, string> = {
  'ChannelDisabledPanel.tsx': 'informational placeholder (locked/loading/error states), zero controls',
  'ChannelsPanel.tsx': 'list-detail shell routing to per-channel panels; carries no controls of its own',
  'DiscordPanel.tsx': 'thin BotChannelSpec wrapper; BotChannelPanel fans its entries out to channel=discord',
  'TelegramPanel.tsx': 'thin BotChannelSpec wrapper; BotChannelPanel fans its entries out to channel=telegram',
  'WeComPanel.tsx': 'thin BotChannelSpec wrapper; BotChannelPanel fans its entries out to channel=wecom',
  'ImportPanel.tsx': 'single action button launching the import wizard; no persistent settings',
  'InstanceFormFields.tsx': 'per-instance CRUD form fields (add/edit crew), not global settings',
  'McpManagement.tsx': 'mounted only on the standalone Developer page — a settings deep link would be dead',
  'MobileLoginCard.tsx': 'mint-a-sign-in-link action card; the link it returns is a one-time credential, not a persistent setting',
  'PostureDisclosure.tsx': "read-only disclosure rows for SecurityPanel's posture section (manual entry security.live-security-posture)",
  'ReleasesPanel.tsx': 'read-only changelog viewer, zero persistent settings',
  'ReportProblemCard.tsx': 'feedback action card, no settings',
  'SecretsPanel.tsx': 'CRUD list for stored secrets; add/delete forms are transient, no persistent knobs',
  'SettingsSearch.tsx': 'the settings search box itself — indexing it would be self-referential',
  'ThemeDroppedRulesNotice.tsx': 'informational notice, zero controls',
  'WebhooksPanel.tsx': 'status summary card; the real controls live on the /webhooks page',
}

describe('settings coverage gate — PANEL_TAB_MAP completeness', () => {
  it('every panel file is either mapped or explicitly waived', () => {
    const unaccounted = panelFiles().filter(
      f => !(f in PANEL_TAB_MAP) && !(f in UNMAPPED_PANELS),
    )
    expect(
      unaccounted,
      'New settings panel file(s) with no PANEL_TAB_MAP entry. Map the file in ' +
      'scripts/settingsExtract.ts (so its Settings* primitives are indexed for ' +
      'search), or add it to UNMAPPED_PANELS here with the reason it holds no ' +
      'searchable settings.',
    ).toEqual([])
  })

  it('no waived (unmapped) panel renders extractable primitives', () => {
    // The WhatsAppPanel failure mode: primitives in an unmapped file are
    // dropped without any signal. A waived file that gains a primitive must
    // graduate into PANEL_TAB_MAP.
    const offenders: string[] = []
    for (const file of Object.keys(UNMAPPED_PANELS)) {
      if (!fs.existsSync(path.join(SETTINGS_DIR, file))) continue
      const source = readPanel(file)
      for (const tag of EXTRACTABLE_PRIMITIVE_TAGS) {
        if (new RegExp(`<${tag}\\b`).test(source)) offenders.push(`${file}: <${tag}>`)
      }
    }
    expect(
      offenders,
      'A panel waived as "no searchable settings" now renders an extractable ' +
      'primitive, which PANEL_TAB_MAP silently drops. Move the file from ' +
      'UNMAPPED_PANELS into PANEL_TAB_MAP in scripts/settingsExtract.ts.',
    ).toEqual([])
  })

  it('map and waiver list stay consistent with the directory', () => {
    const files = new Set(panelFiles())
    // A mapped file that no longer exists silently indexes nothing.
    expect(
      Object.keys(PANEL_TAB_MAP).filter(f => !files.has(f)),
      'PANEL_TAB_MAP names a panel file that no longer exists.',
    ).toEqual([])
    expect(
      Object.keys(UNMAPPED_PANELS).filter(f => !files.has(f)),
      'UNMAPPED_PANELS names a panel file that no longer exists — prune it.',
    ).toEqual([])
    // A file in both lists means the waiver reason is stale.
    expect(
      Object.keys(UNMAPPED_PANELS).filter(f => f in PANEL_TAB_MAP),
      'A panel is both mapped and waived — remove it from UNMAPPED_PANELS.',
    ).toEqual([])
  })
})

/* ────────────────────────────────────────────────────────────────────────── */
/* 2. Bare-control accounting                                                 */
/* ────────────────────────────────────────────────────────────────────────── */

/** Interactive control tags the extractor cannot see. `<Select` also matches
 *  the Radix compound select; native lowercase tags cover hand-rolled fields. */
const BARE_CONTROL_TAGS = [
  'Toggle', 'Switch', 'Checkbox', 'SimpleSelect', 'SearchableSelect',
  'SegmentedControl', 'Select', 'Input', 'input', 'select', 'textarea',
] as const

type BareCounts = Partial<Record<(typeof BARE_CONTROL_TAGS)[number], number>>

/**
 * Exact per-file accounting of bare controls, with the coverage story for
 * each. "manual: <id>" means search reaches the control through that
 * settingsManual.ts entry; "transient" means it is not a persistent setting.
 */
const WAIVED_BARE_CONTROLS: Record<string, { counts: BareCounts; reason: string }> = {
  'AboutPanel.tsx': {
    counts: { Toggle: 1, SegmentedControl: 2 },
    reason:
      'gateway auto-update Toggle has a ternary label (manual: about.update-notifications); ' +
      'the two channel SegmentedControls share manual: about.update-channel',
  },
  'BrowserPanel.tsx': {
    counts: { Input: 1 },
    reason: 'attach-token credential field with Save/Clear semantics (manual: browser.attach-token)',
  },
  'DisplayPanel.tsx': {
    counts: { SimpleSelect: 1, Input: 1 },
    reason: 'theme-install form (source picker + location) — transient install flow, not settings',
  },
  'InstanceFormFields.tsx': {
    counts: { SimpleSelect: 1, input: 9 },
    reason: 'per-instance add/edit CRUD form fields, not global settings',
  },
  'McpManagement.tsx': {
    counts: { Switch: 2 },
    reason: 'Developer-page-only surface, deliberately outside settings search',
  },
  'MobileLoginCard.tsx': {
    counts: { Input: 1 },
    reason: 'read-only display of the freshly minted sign-in link (select-on-focus for manual copy) — transient, not a persistent setting',
  },
  'NotificationsPanel.tsx': {
    counts: { Toggle: 1, Select: 1, input: 1 },
    reason:
      'per-channel mute Toggle + priority Select render runtime-fetched rows ' +
      '(manual: notifications.sources); the volume range input has no slider ' +
      'primitive (manual: notifications.volume)',
  },
  'RemoteCrewPanel.tsx': {
    counts: { input: 2 },
    reason:
      'setup-wizard AWS profile/region convenience fields (localStorage) behind a ' +
      'non-URL sub-tab a deep link cannot mount',
  },
  'SecretsPanel.tsx': {
    counts: { input: 2 },
    reason: 'add-secret name/value form — transient CRUD, not persistent knobs',
  },
  'SecurityPanel.tsx': {
    counts: { Toggle: 5, Checkbox: 2, Input: 3 },
    reason:
      'disable-all + per-rule + custom-rule Toggles are data-driven table rows ' +
      '(manual: security.denied-commands); Checkboxes are confirm-modal acks; ' +
      'Inputs are the rule search filter and the add-custom-deny form ' +
      '(manual: security.your-custom-denies)',
  },
  'SettingsSearch.tsx': {
    counts: { input: 1 },
    reason: 'the settings search box itself',
  },
  'SlackPanel.tsx': {
    counts: { Input: 1 },
    reason: "TagListEditor's internal draft input — part of a composite the extractor indexes whole",
  },
  'WhatsAppPanel.tsx': {
    counts: { SimpleSelect: 1 },
    reason: 'add-a-group action picker (value always empty) — an action menu, not a setting',
  },
}

function countBareControls(source: string): BareCounts {
  const counts: BareCounts = {}
  for (const tag of BARE_CONTROL_TAGS) {
    const matches = source.match(new RegExp(`<${tag}\\b`, 'g'))
    if (matches) counts[tag] = matches.length
  }
  return counts
}

describe('settings coverage gate — bare controls', () => {
  it('bare interactive controls in panel files match the accounting exactly', () => {
    const problems: string[] = []
    for (const file of panelFiles()) {
      const actual = countBareControls(readPanel(file))
      const expected = WAIVED_BARE_CONTROLS[file]?.counts ?? {}
      const tags = new Set([...Object.keys(actual), ...Object.keys(expected)]) as Set<
        (typeof BARE_CONTROL_TAGS)[number]
      >
      for (const tag of tags) {
        const a = actual[tag] ?? 0
        const e = expected[tag] ?? 0
        if (a !== e) problems.push(`${file}: <${tag}> ×${a} (accounted ×${e})`)
      }
    }
    expect(
      problems,
      'Bare-control count drifted from WAIVED_BARE_CONTROLS. A bare control is ' +
      'invisible to settings search — for a persistent setting use a Settings* ' +
      'primitive from components/settings.tsx (auto-indexed), or add a ' +
      'settingsManual.ts entry plus a data-setting-label anchor; then update the ' +
      'accounting here with a reason. A count DECREASE means stale accounting — ' +
      'shrink the waiver so it cannot mask a later regression.',
    ).toEqual([])
  })
})

/* ────────────────────────────────────────────────────────────────────────── */
/* 3. Dynamic-label skip accounting                                           */
/* ────────────────────────────────────────────────────────────────────────── */

/**
 * Primitives whose label the extractor cannot resolve, per file, with where
 * the coverage lives instead. gen-settings prints these as "N dynamic labels
 * skipped"; this pins each one so a new silent skip fails the build.
 */
const EXPECTED_DYNAMIC_SKIPS: Record<string, { count: number; reason: string }> = {
  'BotChannelPanel.tsx': {
    count: 14,
    reason:
      'labels arrive through BotChannelSpec props (per-channel copy decided by the ' +
      'mounting wrapper); the static-label primitives in the same file fan out per channel',
  },
  'NotificationsPanel.tsx': {
    count: 1,
    reason:
      'per-category sound SettingsSelect renders label={i18nT(CATEGORY_LABEL_KEY[cat])} ' +
      'inside a map over a closed union — indexed via manual entries ' +
      'notifications.sound-category-*',
  },
}

describe('settings coverage gate — dynamic-label skips', () => {
  it('every dynamic label the extractor skips is accounted for', () => {
    const problems: string[] = []
    for (const file of panelFiles()) {
      if (!(file in PANEL_TAB_MAP)) continue
      const { skipped } = extractFromSource(readPanel(file), file)
      const expected = EXPECTED_DYNAMIC_SKIPS[file]?.count ?? 0
      if (skipped !== expected) problems.push(`${file}: skipped ${skipped} (accounted ${expected})`)
    }
    expect(
      problems,
      'A Settings* primitive with a dynamic label is silently dropped from search. ' +
      'Make the label a literal i18nT(\'key\') on the primitive itself, or add a ' +
      'settingsManual.ts entry for it and record the skip in EXPECTED_DYNAMIC_SKIPS ' +
      'with a reason.',
    ).toEqual([])
  })

  it('skip accounting names only mapped, existing files', () => {
    const files = new Set(panelFiles())
    expect(
      Object.keys(EXPECTED_DYNAMIC_SKIPS).filter(f => !files.has(f) || !(f in PANEL_TAB_MAP)),
      'EXPECTED_DYNAMIC_SKIPS entry for a file that is missing or unmapped — prune it.',
    ).toEqual([])
  })
})

/* ────────────────────────────────────────────────────────────────────────── */
/* 4. Manual entries stay anchored to real panel source                       */
/* ────────────────────────────────────────────────────────────────────────── */

describe('settings coverage gate — manual entries anchor to panel source', () => {
  it("every manual entry's labelKey appears in a panel file mapped to its tab", () => {
    // A settingsManual entry deep-links by resolving its labelKey and querying
    // the DOM for a matching data-setting-label anchor. Nothing else ties the
    // entry to the panel, so a refactor that renames or drops the key silently
    // kills both the highlight and (eventually) the row itself while the
    // registry keeps offering the ghost entry. Requiring the key to appear in
    // the tab's panel source (as an anchor attribute or a rendered label)
    // fails the build the moment they drift apart.
    const filesByTab = new Map<string, string[]>()
    for (const [file, target] of Object.entries(PANEL_TAB_MAP)) {
      const targets = Array.isArray(target) ? target : [target]
      for (const t of targets) {
        const tab = typeof t === 'string' ? t : t.tab
        filesByTab.set(tab, [...(filesByTab.get(tab) ?? []), file])
      }
    }
    const sourceCache = new Map<string, string>()
    const src = (f: string) => {
      if (!sourceCache.has(f)) sourceCache.set(f, readPanel(f))
      return sourceCache.get(f)!
    }
    const orphans: string[] = []
    for (const entry of SETTINGS_MANUAL) {
      const candidates = filesByTab.get(entry.tab) ?? []
      if (!candidates.some(f => src(f).includes(entry.labelKey))) {
        orphans.push(`${entry.id}: labelKey '${entry.labelKey}' not found in any ${entry.tab}-tab panel`)
      }
    }
    expect(
      orphans,
      "A settingsManual entry's labelKey no longer appears in its tab's panel " +
      'source — the deep-link anchor is gone or renamed. Restore the ' +
      'data-setting-label anchor (or update/remove the manual entry).',
    ).toEqual([])
  })
})
