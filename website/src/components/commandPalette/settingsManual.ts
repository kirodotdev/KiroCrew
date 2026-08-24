import type { ManualSettingEntry } from './settingsTypes'

/**
 * Hand-curated settings-registry entries (Search Everywhere — Settings
 * provider) for settings the regex extractor cannot see.
 *
 * The extractor only reads the five `Settings*` JSX primitives, so controls
 * built from raw markup — SecurityPanel's radiogroup, its bare `Toggle`, its
 * whole read-only sections — never reach the registry, and the file-level
 * PANEL_TAB_MAP cannot attach the `?section=` param SecurityPanel's
 * list-detail rail needs to mount anything at all.
 *
 * Merge semantics (`mergeManualEntries` in scripts/settingsExtract.ts):
 *  - an entry whose id matches a generated id REPLACES it (used to attach
 *    params the file-level map cannot scope);
 *  - any other entry is appended.
 * Generation validates that ids are unique within this list and that every
 * labelKey/descriptionKey resolves in the English catalogs, and throws
 * otherwise.
 *
 * Contract per entry: entries are KEY-ONLY (the i18n gate forbids English
 * prose literals in hand-written source). Generation resolves `label` (and
 * `description`) from the English catalogs — the registry is an English
 * search corpus — and SecurityPanel.tsx carries a
 * `data-setting-label={i18nT('<labelKey>')}` anchor on the section's wrapper
 * so deep-link highlighting (useSettingHighlight) finds the element in any
 * locale.
 */
export const SETTINGS_MANUAL: ManualSettingEntry[] = [
  {
    // Override of the one primitive the extractor DOES see in SecurityPanel:
    // without `section=apps` the deep link lands on the security rail with the
    // toggle's section unmounted, so the highlight silently no-ops.
    id: 'security.trust-every-third-party-app',
    labelKey: 'pages.settings.securityPanel.trustedApps.allow_all_label',
    descriptionKey: 'pages.settings.securityPanel.trustedApps.allow_all_description',
    tab: 'security',
    type: 'toggle',
    occurrence: 1,
    params: { section: 'apps' },
  },
  {
    id: 'security.how-long-auto-approve-stays-on',
    labelKey: 'pages.settings.securityPanel.yolo_duration_title',
    tab: 'security',
    // A radiogroup of duration presets — a button group in all but markup.
    type: 'buttonGroup',
    occurrence: 1,
    params: { section: 'approval' },
    configKey: 'agent.yolo_duration',
  },
  {
    id: 'security.trust-this-machine-s-tailnet-name',
    labelKey: 'pages.settings.securityPanel.tailnet_title',
    tab: 'security',
    type: 'toggle',
    occurrence: 1,
    params: { section: 'tailnet' },
  },
  {
    id: 'security.denied-commands',
    labelKey: 'pages.settings.securityPanel.denied_commands',
    tab: 'security',
    // The section is a table of per-rule enable switches.
    type: 'toggle',
    occurrence: 1,
    params: { section: 'rules' },
  },
  {
    id: 'security.governance-policy',
    labelKey: 'pages.settings.securityPanel.governance_policy',
    tab: 'security',
    // Read-only viewer of resolved policy scopes; nearest primitive shape is a
    // (disabled) select over enumerated values.
    type: 'select',
    occurrence: 1,
    params: { section: 'governance' },
  },
  {
    id: 'security.live-security-posture',
    labelKey: 'pages.settings.securityPanel.live_security_posture',
    tab: 'security',
    // Status rows with expandable disclosures; toggling a row open is the only
    // interaction, so 'toggle' is the closest primitive.
    type: 'toggle',
    occurrence: 1,
    params: { section: 'posture' },
  },
]
