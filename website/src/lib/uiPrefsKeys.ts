/**
 * Which `localStorage` keys the host-side settings backup holds. The sync that
 * reads and writes them, and the design decisions behind this list, live in
 * `uiPrefs.ts`, which re-exports `DURABLE_PREF_KEYS`. It imports nothing, so
 * the facade can import it without a cycle.
 */

/**
 * Durable, user-chosen preferences. Exact keys only.
 *
 * A key belongs here when a user would be annoyed to set it twice, and would
 * NOT be surprised to see it follow them to a new window. When in doubt leave
 * it out: a missing backup degrades to today's behaviour, while backing up
 * session-scoped state resurrects stale UI on an unrelated profile.
 *
 * Growing this list is safe ONLY because of the reconcile pass. A warm profile
 * never cold-hydrates (`needsHydrate` is false once `SYNCED_KEYS_KEY` exists),
 * so without it a newly added key's local DEFAULT -- often written by a hook on
 * mount -- would be flushed to the host before the host's own value was ever
 * read, overwriting a value another origin saved (growth-gap issue 9491).
 * `reconcileNewDurableKeys` runs before the first flush and reads the host copy
 * for keys this profile has never synced: an absent local key adopts the host
 * value, a present one is baselined so the first flush does not upload it. A
 * key counts as "new" when it is in neither the synced fingerprints nor the
 * reconciled roster (see `ROSTER_ENTRY`), so the pass costs one GET per
 * allowlist growth, not per boot.
 */
export const DURABLE_PREF_KEYS: readonly string[] = [
  // Chat preferences — one JSON blob holding ~16 settings (send-key mode,
  // timestamps, turn stats, content width, collapse-steps, stream mode, ...).
  // This single key is the most-reported loss; see issue #8705.
  'mc-chat-config',
  'mc-busy-send-mode',
  // Typography and zoom.
  //
  // NOT here: 'mc-zoom' and 'mc-font-scale'. hooks/useZoom.ts runs a one-time
  // migration that folds both legacy page-scaling keys into the native zoom
  // factor and then DELETES them, so backing them up would restore them on the
  // next cold profile and re-run the migration forever.
  'mc-font-family',
  // The Custom Font Family choice: the picked family name and the ligature
  // toggle. 'mc-font-family' above already persists that Custom is SELECTED, so
  // without these two a cold profile restores family=custom while the chosen
  // font and ligature preference silently revert to the Sans fallback / default
  // on. Carried by the same reconcile growth path documented above.
  'mc-custom-font',
  'mc-custom-font-ligatures',
  // Chat reading width (md | full) -- hooks/useReadingWidth.ts.
  'mc-reading-width',
  // Navigation and shell layout the user arranged by hand.
  'mc-nav',
  // Interface paradigm (chat | cli) -- hooks/useUIMode.tsx. Its provider
  // persists the current mode on MOUNT, so on a warm origin this key is always
  // present locally; the reconcile pass below is what keeps that mount-written
  // default from overwriting another origin's backup.
  'mc-ui',
  'mc-app-nav-order',
  // The rail rows the user unpinned (lib/appNavHidden.ts) or pinned
  // (lib/navPinned.ts). Written only by their clicks, through
  // savePrefExplicitly (issue #34741).
  'mc-app-nav-hidden',
  'mc-nav-pinned',
  'mc-apps-expanded',
  'mc-bottom-terminal',
  'mc-files-rail-open',
  'mc-crews-view',
  'mc-crew-switcher-pinned',
  'mc-crew-switcher-stable-order',
  'mc-filter-folders-shelved',
  'mc-flat-hidden-folders',
  'mc-input-height',
  // Sessions sidebar width (pages/chat-sidebar/resize.ts). The pre-board key
  // is the width to restore when the user leaves board view. Without it a
  // restored profile keeps the board width in list view.
  'mc-sidebar-width',
  'mc-sidebar-width-pre-board',
  // Board-view folder collapse overrides (utils/boardFolderCollapse.ts). The
  // overrides live one localStorage key per (column, folder) pair. They travel
  // as this one key because the host caps the key count. `readPreference`
  // projects the value from the per-key entries.
  'kc-board-folder-collapsed',
  // Settings > Display > Sidebar Colors (store/dashboardSlice.ts). The colour
  // picked for one session is stored with that session on the host instead.
  'mc-session-colors-palette',
  'mc-session-colors-intensity',
  'mc-session-colors-mode',
  'mc-session-default-color',
  // Diff and file viewer toggles.
  'mc-diff-plain',
  'mc-diff-split',
  'mc-file-linenums',
  'mc-file-wordwrap',
  'mc-file-collapse-unchanged',
  // Artifacts browser.
  'mc-artifacts-view',
  'mc-artifacts-sort',
  'mc-artifacts-pinned-only',
  'mc-artifacts-session-docs-collapsed',
  'mc-artifact-folders-expanded',
  // Misc opt-ins and acknowledgements the user should not have to repeat.
  //
  // NOT here: 'mc-yolo-ack'. Its mere PRESENCE makes ApprovalModePicker skip the
  // confirmation dialog and activate full auto-approval directly, and this backup
  // lives in the agent-writable data home — so restoring it would let an agent
  // that can write ~/.kiro/crew/ui-prefs.json pre-satisfy a human safety gate on
  // the user's next fresh origin. The rule this key is an instance of: a value
  // that GATES a safety confirmation must not be restorable from a file the agent
  // can write, however convenient re-acknowledging is.
  'mc-agent-scene',
  'mc-kb-graph-physics',
  // Notification sound settings (enabled/volume/per-category presets, a timed mute) --
  // hooks/useNotificationSound.ts. JSON blob, written only on an explicit save.
  'mc-notification-sound',
  'mc:notif:activeKinds:v2',
  'kirocrew:account-email-hidden',
  'kirocrew:comment-hint-dismissed',
  // The tip that names the image gesture, closed (components/useCommentHint.ts).
  'kirocrew:comment-hint-image-dismissed',
  // The composer's list-indent keyboard hint, dismissed (components/ChatInput.tsx).
  // Written only by the Dismiss click, through savePrefExplicitly.
  'kirocrew:list-indent-hint-dismissed',
  // The "hooks have no effect in this session" notice, dismissed
  // (pages/chat/SpecHooksNotice.tsx). Written only by its Dismiss click,
  // through savePrefExplicitly. It hides a notice and gates nothing.
  'kirocrew:spec-hooks-run-nowhere-notice-dismissed',
  // Settings > Shortcuts, and the push-to-talk key on Settings > Voice. Each
  // key is written only by an explicit user action, on its settings page or in
  // the shortcuts reference modal. No hook writes a default on mount. The
  // on/off switch and the macOS digit modifier hold '1' or '0'
  // (components/ShortcutsModal.tsx). The Search everywhere chord is a JSON
  // QuickSearchConfig (lib/quickSearchShortcut.ts). The panel toggle chords are
  // a JSON PanelToggleOverrides holding overrides only
  // (lib/panelToggleShortcuts.ts). The push-to-talk key is a JSON PttConfig
  // (lib/pushToTalk.ts). Its header states what a client inherits from another
  // machine's binding.
  //
  // NOT here: 'mc-mic-device-id' (hooks/mic.ts). The browser issues a
  // microphone id for the devices on that machine, so a restored one names no
  // device. Also NOT here: 'mc-shortcut-overrides'. lib/shortcutRegistry.ts
  // ships only its reader. The change that adds the writer must add the key
  // here.
  'mc-keyboard-shortcuts',
  'mc-mac-ctrl-digits',
  'mc-quicksearch-shortcut',
  'mc-panel-toggle-shortcuts',
  'mc-ptt-config',
  // Cloud launch defaults.
  'mc-cloud-profile',
  'mc-cloud-region',
  'mc-cloud-size',
  // Per-app preferences.
  'kc-cron-folders-collapsed',
  'kc:file-explorer:state:v2',
  'kc:issue-radar:ui-state',
  'telemetry:tab',
  'telemetry:spend-group',
  'mdnb-view',
  'mdnb-sort',
  'mdnb-list-view',
  'mdnb-full-width',
  'mdnb-panel-width',
  'mdnb-panel-open',
  'mdnb-auto-commit',
  'mdnb-auto-sync',
  'mdnb-auto-sync-mins',
  'mdnb-sync-shortcut',
  'ste_rail_w',
]

/**
 * The allowlist as it stood BEFORE the reconcile mechanism shipped, frozen
 * forever -- never append to this list; new keys go in `DURABLE_PREF_KEYS`
 * only.
 *
 * This is the reconcile baseline for a profile that carries no roster entry: a
 * legacy profile predates the roster, and every key its builds ever knew is in
 * this list, so "durable now but not in here" is exactly "added after this
 * profile could have known it". Without the frozen baseline, "no fingerprint"
 * had to stand in for "newly added", and the two are not the same: a key the
 * profile simply NEVER HELD (most of them, for most users) has no fingerprint
 * either, and reconciling those would bulk-import another origin's layout onto
 * a warm profile -- the cross-origin sync that design decision 1 in uiPrefs.ts
 * rules out.
 */
export const PRE_ROSTER_KEYS: readonly string[] = [
  'mc-chat-config',
  'mc-busy-send-mode',
  'mc-font-family',
  'mc-nav',
  'mc-app-nav-order',
  'mc-apps-expanded',
  'mc-bottom-terminal',
  'mc-files-rail-open',
  'mc-crews-view',
  'mc-crew-switcher-pinned',
  'mc-crew-switcher-stable-order',
  'mc-filter-folders-shelved',
  'mc-flat-hidden-folders',
  'mc-input-height',
  'mc-diff-plain',
  'mc-diff-split',
  'mc-file-linenums',
  'mc-file-wordwrap',
  'mc-file-collapse-unchanged',
  'mc-artifacts-view',
  'mc-artifacts-sort',
  'mc-artifacts-pinned-only',
  'mc-artifacts-session-docs-collapsed',
  'mc-artifact-folders-expanded',
  'mc-dev-mode',
  'mc-agent-scene',
  'mc-kb-graph-physics',
  'mc:notif:activeKinds:v2',
  'kirocrew:account-email-hidden',
  'kirocrew:comment-hint-dismissed',
  'mc-cloud-profile',
  'mc-cloud-region',
  'mc-cloud-size',
  'kc-cron-folders-collapsed',
  'kc:file-explorer:state:v2',
  'kc:issue-radar:ui-state',
  'telemetry:tab',
  'telemetry:spend-group',
  'mdnb-view',
  'mdnb-sort',
  'mdnb-list-view',
  'mdnb-full-width',
  'mdnb-panel-width',
  'mdnb-panel-open',
  'mdnb-auto-commit',
  'mdnb-auto-sync',
  'mdnb-auto-sync-mins',
  'mdnb-sync-shortcut',
  'ste_rail_w',
]
