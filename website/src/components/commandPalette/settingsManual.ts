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
  {
    // Add-a-custom-deny-pattern card: a create-form composite (pattern + note
    // inputs + Add button) with no primitive shape, distinct from the built-in
    // rules table the section-level 'security.denied-commands' entry covers.
    id: 'security.your-custom-denies',
    labelKey: 'pages.settings.securityPanel.your_custom_denies',
    descriptionKey: 'pages.settings.securityPanel.add_your_own_deny_patterns_python_compatible_reg',
    tab: 'security',
    type: 'input',
    occurrence: 1,
    params: { section: 'rules' },
  },
  {
    // One entry covers both mutually-styled channel switchers (desktop shell
    // updater and gateway install) — same catalog label, same tab, and the
    // SegmentedControl they render as has no primitive shape.
    id: 'about.update-channel',
    labelKey: 'pages.settings.aboutPanel.update_channel',
    tab: 'about',
    type: 'buttonGroup',
    occurrence: 1,
  },
  {
    // Gateway auto-update / update-notification toggle. Its rendered label is a
    // ternary (self-update-capable installs read "Auto-update on restart"), which
    // the extractor cannot resolve; indexed under the notify wording so it does
    // not collide with the desktop SettingsToggle's generated entry.
    id: 'about.update-notifications',
    labelKey: 'pages.settings.aboutPanel.notify_when_an_update_is_available',
    tab: 'about',
    type: 'toggle',
    occurrence: 1,
    configKey: 'auto_update',
  },
  {
    // Per-channel notification mute + priority overrides: rows are fetched at
    // runtime (channel names are data), so one entry covers the Sources card.
    id: 'notifications.sources',
    labelKey: 'pages.settings.notificationsPanel.sources',
    descriptionKey: 'pages.settings.notificationsPanel.mute_notification_sources_or_override_their_prio',
    tab: 'notifications',
    type: 'toggle',
    occurrence: 1,
  },
  {
    // Range slider — no slider primitive exists; stepper is the nearest shape.
    id: 'notifications.volume',
    labelKey: 'pages.settings.notificationsPanel.volume',
    tab: 'notifications',
    type: 'stepper',
    occurrence: 1,
  },
  // Per-category sound selects render inside CATEGORY_ROWS.map with
  // label={i18nT(CATEGORY_LABEL_KEY[cat])} — a dynamic expression the extractor
  // skips. The category set is a closed union, so each row is indexed here as an
  // explicit entry (literal keys, not a template: the i18n added-lines gate
  // cannot statically verify a computed key). Highlighting works because
  // SettingsSelect renders the resolved label as its own data-setting-label.
  {
    id: 'notifications.sound-category-all',
    labelKey: 'pages.settings.notificationsPanel.category_all',
    descriptionKey: 'pages.settings.notificationsPanel.category_all_description',
    tab: 'notifications',
    type: 'select',
    occurrence: 1,
  },
  {
    id: 'notifications.sound-category-turn',
    labelKey: 'pages.settings.notificationsPanel.category_turn',
    descriptionKey: 'pages.settings.notificationsPanel.category_turn_description',
    tab: 'notifications',
    type: 'select',
    occurrence: 1,
  },
  {
    id: 'notifications.sound-category-cron',
    labelKey: 'pages.settings.notificationsPanel.category_cron',
    descriptionKey: 'pages.settings.notificationsPanel.category_cron_description',
    tab: 'notifications',
    type: 'select',
    occurrence: 1,
  },
  {
    id: 'notifications.sound-category-approval',
    labelKey: 'pages.settings.notificationsPanel.category_approval',
    descriptionKey: 'pages.settings.notificationsPanel.category_approval_description',
    tab: 'notifications',
    type: 'select',
    occurrence: 1,
  },
  {
    id: 'notifications.sound-category-hook',
    labelKey: 'pages.settings.notificationsPanel.category_hook',
    descriptionKey: 'pages.settings.notificationsPanel.category_hook_description',
    tab: 'notifications',
    type: 'select',
    occurrence: 1,
  },
  {
    id: 'notifications.sound-category-heartbeat',
    labelKey: 'pages.settings.notificationsPanel.category_heartbeat',
    descriptionKey: 'pages.settings.notificationsPanel.category_heartbeat_description',
    tab: 'notifications',
    type: 'select',
    occurrence: 1,
  },
  {
    id: 'notifications.sound-category-subagent',
    labelKey: 'pages.settings.notificationsPanel.category_subagent',
    descriptionKey: 'pages.settings.notificationsPanel.category_subagent_description',
    tab: 'notifications',
    type: 'select',
    occurrence: 1,
  },
  {
    id: 'notifications.sound-category-taskrunner',
    labelKey: 'pages.settings.notificationsPanel.category_taskrunner',
    descriptionKey: 'pages.settings.notificationsPanel.category_taskrunner_description',
    tab: 'notifications',
    type: 'select',
    occurrence: 1,
  },
  {
    // Playwright attach token: a credential field with Save/Clear semantics the
    // SettingsInput primitive has no shape for.
    id: 'browser.attach-token',
    labelKey: 'pages.settings.browserPanel.token_label',
    tab: 'browser',
    type: 'input',
    occurrence: 1,
  },
  {
    // Color-dot swatch row — circular color buttons don't fit
    // SettingsButtonGroup's text-button pattern (comment at the render site).
    id: 'display.default-for-new-sessions',
    labelKey: 'pages.settings.displayPanel.default_for_new_sessions',
    descriptionKey: 'pages.settings.displayPanel.none_auto_cycle_or_pick_a_fixed_color',
    tab: 'display',
    type: 'buttonGroup',
    occurrence: 1,
  },
  {
    // One-way enable action on the disabled-state gate card (writes
    // instances.enabled); not a toggle row, so no primitive fits.
    id: 'instances.enable-remote-crew-management',
    labelKey: 'pages.settings.instancesPanel.enable_remote_crew_management',
    tab: 'instances',
    type: 'toggle',
    occurrence: 1,
    configKey: 'instances.enabled',
  },
]
