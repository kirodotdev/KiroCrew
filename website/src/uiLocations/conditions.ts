/**
 * The closed vocabulary a UI location's prerequisites may use, as pure data.
 *
 * One table for the descriptor types (`./descriptors.ts`) and the generator
 * (`scripts/gen-ui-index.mjs`, which copies the descriptions into the index so
 * find_ui can say what each id means). Adding a condition or a reveal state is
 * one entry here; nothing else lists them. No imports: the generator loads this
 * module on its own.
 */

/** Runtime facts a location can need. Each description says what must hold. */
export const UI_CONDITIONS = {
  has_open_sessions: 'at least one open session is listed',
  app_enabled: 'the app that provides this page is installed and enabled',
  full_dashboard: 'the full dashboard is open, not an embedded chat or sessions view',
  no_active_session: 'no session is open in the chat pane',
  no_schedules: 'no scheduled job exists yet',
  has_schedules: 'at least one scheduled job exists',
  search_bar_unclaimed: 'no enabled app has claimed the top-bar search (an app that does turns it into its own launcher)',
  session_open: 'a session is open in the chat pane',
  no_response_running: 'the open session is not answering right now (the control is disabled while a response runs)',
  empty_session: 'the open session has no messages yet',
  not_on_sessions_page: 'the open page is not Sessions (on a phone, Sessions has its own drawer instead)',
  // Declared ahead of the wave-3 area batches, so no batch edits this file
  // (it has one owner). A batch that finds none of these fits stops and asks.
  // Sessions sidebar
  has_closed_sessions: 'at least one closed session is in the history',
  older_sessions_collapsed: 'the Older Sessions list is collapsed',
  sessions_board_view: 'the sessions list is in Board view, with sessions in columns (switch it from the sessions ⋯ menu)',
  sessions_list_view: 'the sessions list is in List view (the default)',
  pointer_on_session_row: 'the pointer or keyboard focus is on a session row of this machine that is not being renamed (the control is drawn on that row)',
  // Shell and notifications
  terminal_enabled: 'the terminal is turned on in Settings',
  developer_mode: 'developer mode is on',
  phone_connect_available: 'pairing a phone is available on this machine',
  kiro_account_entry: 'the menu offers a Kiro account row (always when this installation runs on Kiro; with another backend, only once an account usage reading has arrived)',
  has_notifications: 'at least one notification is listed',
  has_unread_notifications: 'at least one notification is unread',
  notification_selected: 'a notification is open in the detail panel (select it in the list)',
  notification_read: 'the open notification is already read (opening one marks it read; an unread one shows Mark read here instead)',
  // Crewmates
  crewmate_selected: 'a crewmate is selected in the list',
  no_crewmates: 'no crewmate exists yet',
  has_crewmates: 'at least one crewmate exists',
  crewmate_editor_open: "a crewmate's editor is open (choose Edit on that crewmate)",
  // Apps
  has_installed_apps: 'at least one app is installed',
  app_details_open: "an app's detail page is open (in Discover, click the app's card)",
  app_tile_menu_open: "the app card's ⋯ (More actions) menu is open (in Library, point at the app's card to show ⋯, then click it; clicking the card itself opens the app instead)",
  // Schedule, artifacts, skills
  job_open: "a scheduled job's detail panel is open (select the job in the list)",
  job_running: 'the open job is running now',
  job_secret_request_pending: 'the open job is waiting for a secret approval',
  job_details_tab: "the job panel's Details tab is selected (the default; the Logs tab hides this)",
  schedule_list_view: 'the Schedule page shows its List view (the default; the Calendar and Executions views hide this)',
  jobs_checked: 'one or more jobs are checked in the list',
  one_job_checked: "exactly one job is checked in the list (tick that job's own box)",
  skill_selected: 'a skill is selected in the list',
  // Composer
  message_typed: 'something is ready to send: text in the message box, an attached file, or a referenced session',
  // Added by the wave-3 integration, one per state a batch found it needed and
  // checked against its render site.
  // Sessions sidebar
  board_missing_state_lanes: 'one of the Board view’s default status columns was removed (this appears only then)',
  // Composer
  response_running: 'the open session is answering right now',
  message_box_empty: 'nothing is waiting to send: no text in the message box and no attached file (with either, this place holds Queue or Steer instead)',
  mouse_input: 'the device is used with a mouse or trackpad, not a touchscreen',
  touch_input: 'the device is used by touch (a tablet or touchscreen), so the message box shows its touch controls',
  goal_loop_running: 'a goal loop is running in the open session (its panel then offers Pause)',
  monitor_running: 'a monitor is watching for the open session and has not finished (its panel then offers Stop monitor)',
  screen_capture_available: 'taking a screenshot is supported here (the desktop app, or a Mac)',
  context_usage_reported: 'the open session has reported how full its context is (after its first answer)',
  // Notifications
  new_channel_prompt: 'the first notification from a new channel is asking whether to keep or mute that channel',
  // Crewmates
  crewmate_danger_zone_open: "the crewmate editor shows its Danger zone section (choose Danger zone in the editor's section list)",
  crewmate_panel_not_docked: "the crewmate's details panel is not already docked open beside the chat",
  // Apps (the app whose detail page is open, not the app that provides the page)
  open_app_not_installed: 'the open app is not installed yet and can be installed from the dashboard',
  open_app_enabled: 'the open app is a regular installed app (not a built-in or independently managed one) and is turned on',
  open_app_syncable: 'the open app is a regular installed app with no newer version waiting; Sync reloads its installed files and does not install a new release',
  open_app_update_available: 'the open app is a regular installed app and a newer version is waiting (Update stands where Sync otherwise is)',
  open_app_removable: 'the open app is a regular installed app (not a built-in or independently managed one) that is not locked against removal',
  // Schedule
  job_not_running: 'the open job is not running now (while it runs, Cancel Run stands in this place)',
  // Shell
  nav_rail_expanded: 'the navigation rail is expanded, showing labels',
  // Artifacts
  cloud_deploy_available: "this installation can publish to a public cloud URL (the built-in AWS destination is offered)",
  // Added with the demand-gap batch (each checked against its render site).
  // Composer
  voice_input_supported: 'this browser can record from a microphone (the mic button is not drawn otherwise)',
  // Schedule
  job_enabled: 'the open job is active (not paused)',
  job_paused: 'the open job is paused',
  // Crewmates
  crewmate_place_pane_open: "the crewmate editor shows its Workspace · Memory section (choose it in the editor's section list)",
  crewmate_model_pane_open: "the crewmate editor shows its Model section (choose it in the editor's section list)",
  crewmate_memory_manageable: 'the crewmate keeps a private memory of its own, or is the default crewmate, whose memory is the shared global one (with any other binding there is nothing to manage here)',
  // Artifacts
  artifact_open: "an artifact's own page is open (click the artifact in the library)",
  // Shell: the docked terminal panel (BottomTerminalPanel)
  terminal_not_popped_out: 'the terminal is docked in the dashboard, not popped out to its own window',
  terminal_docked_bottom: 'the terminal panel sits below the chat (the default)',
  terminal_docked_right: 'the terminal panel sits to the right of the chat, side by side with it',
} as const

export type UiConditionId = keyof typeof UI_CONDITIONS

/**
 * States a reveal step is conditional on: `shown_by` with `when` means "do this
 * first only while <state>; otherwise the target is already visible".
 */
export const UI_REVEAL_STATES = {
  sessions_sidebar_collapsed: 'the sessions sidebar is collapsed',
  sessions_drawer_closed: 'the sessions drawer is closed',
  nav_rail_collapsed: 'the navigation rail is collapsed to icons',
  composer_collapsed: 'the message box is collapsed to a single bar (the choice is remembered after a reload)',
  side_panel_closed: "the chat's side panel is closed",
  terminal_panel_closed: 'the terminal panel is closed (the Terminal row in the navigation rail opens it)',
  crewmate_roster_folded: 'a crewmate chat is open on a wide screen, which folds the roster column away',
  crewmate_chat_open_phone: 'a crewmate chat fills the phone screen, so the roster is behind it',
  crewmate_profile_closed: "the crewmate's profile card is closed (pressing the crewmate's name in the chat header opens it)",
} as const

export type UiRevealState = keyof typeof UI_REVEAL_STATES

/**
 * Reveal scopes for the reveal STATES: a container whose owner reports, at
 * runtime, whether it is open (`<GuideRevealScope id open>` in
 * `src/guide/GuideRevealScope.tsx`). Each maps to the reveal state that holds
 * while it is CLOSED, so a `ui.show` reveal step conditional on that state
 * completes the moment the scope reports open. Every reveal state a
 * `shown_by` uses must have one (the generator refuses otherwise); a scope
 * whose owner is not mounted reads "unknown" and the step keeps its `reach`
 * fallback. Menus, tabs and unconditional reveals are compiled by the
 * generator (`menu:<id>`, `tab:<id>`, `reveal:<id>`), so they are not listed
 * here; a declared id never has a colon.
 */
export const UI_REVEAL_SCOPES = {
  'chat.sessions-sidebar': 'sessions_sidebar_collapsed',
  'chat.sessions-drawer': 'sessions_drawer_closed',
  'shell.nav-rail': 'nav_rail_collapsed',
  'composer.box': 'composer_collapsed',
  'chat.side-panel': 'side_panel_closed',
  'shell.terminal-panel': 'terminal_panel_closed',
  'members.roster': 'crewmate_roster_folded',
  'members.roster-phone': 'crewmate_chat_open_phone',
  'members.profile': 'crewmate_profile_closed',
} as const satisfies Record<string, UiRevealState>

export type UiRevealScopeId = keyof typeof UI_REVEAL_SCOPES

/**
 * Conditions the page can evaluate live (`src/guide/guidePredicates.ts` holds
 * one evaluator per id; TypeScript refuses a missing one). A condition on a
 * REVEAL control travels with that guide step as a predicate: unmet, the guide
 * shows a blocker instead of pointing. A reveal control needing a condition
 * not listed here leaves its location search-only (no `ui.show` plan).
 */
export const UI_RUNTIME_PREDICATES = [
  'has_open_sessions',
  'full_dashboard',
  'not_on_sessions_page',
  'schedule_list_view',
  'has_schedules',
  'has_crewmates',
  'crewmate_danger_zone_open',
  'crewmate_place_pane_open',
  'crewmate_model_pane_open',
  'crewmate_memory_manageable',
  'no_response_running',
  'mouse_input',
  'phone_connect_available',
  'goal_loop_running',
  'monitor_running',
] as const satisfies readonly UiConditionId[]

export type UiRuntimePredicateId = typeof UI_RUNTIME_PREDICATES[number]

/**
 * Runtime predicates the owner of a control's opener reports (the goal and
 * monitor button knows whether a loop or monitor runs). The generator moves
 * one on a location onto its opener's step, and the guide does not point at
 * that opener while it is unmet: it says there is nothing to stop instead of
 * opening an empty panel. Mirrors `OPENER_FACT_PREDICATES` in
 * `scripts/lib/ui-index.mjs`.
 */
export const UI_OPENER_FACT_PREDICATES = ['goal_loop_running', 'monitor_running'] as const satisfies readonly UiRuntimePredicateId[]

/**
 * Selection scopes: a condition that holds once the person picks one entity
 * from a registered picker (a list on the same page). A location needing one
 * gets a `select` step pointing at `picker` ("Choose the <entity> you
 * want..."); it completes only when the page that owns the picker reports the
 * selection fact (`useGuideSelection` in `src/guide/guidePredicates.ts`), and
 * an empty picker is a blocker, never a reason to point at a create button.
 * Nothing about WHICH entity was chosen leaves the page. Only conditions with
 * one stable picker are here: the session list (one sidebar in both layouts),
 * the Crewmates roster, the Customize > Crewmates list (picking one opens its
 * editor), the Schedule job table and the Library's app cards (opening a
 * card's ⋯ menu picks that app). A notification has two
 * pickers (the page and the bell sheet), so it is not.
 */
export const UI_SELECTION_SCOPES = {
  session_open: { picker: 'sessions.list', entity: 'session' },
  crewmate_selected: { picker: 'members.roster-list', entity: 'crewmate' },
  job_open: { picker: 'schedule.job-list', entity: 'job' },
  one_job_checked: { picker: 'schedule.job-list', entity: 'tickjob' },
  crewmate_editor_open: { picker: 'agents.crew-list', entity: 'crewmate' },
  // Library: opening one app card's ⋯ menu picks that app (its menu holds
  // Details and Uninstall); the card itself opens the app instead.
  app_tile_menu_open: { picker: 'apps.library.app-list', entity: 'app' },
  // Artifacts: the picker is the library's card gallery, but the pick is made
  // by opening the card, which lands on the artifact's own page under the
  // same route. That page reports the selection with the artifact's name, so
  // the steps after the select step stay bound to it there; opening another
  // artifact is the selection changing, which goes back to choosing.
  artifact_open: { picker: 'artifacts.list', entity: 'artifact' },
} as const satisfies Partial<Record<UiConditionId, { picker: string; entity: string }>>

export type UiSelectionId = keyof typeof UI_SELECTION_SCOPES

/**
 * Gates: a condition the person turns on in Settings (developer mode, the
 * terminal). A location needing one gets a `gate` step: met, it passes at
 * once; off, the guide pauses with a blocker naming the setting and resumes by
 * itself once it is on. The guide never flips it. `setting` is the settings
 * registry id that turns it on, or null when there is none to name. Preview
 * flags are gates too (`preview_flag:<flag>`, with the enabler in
 * `PREVIEW_FLAG_ENABLERS`), compiled by the generator.
 */
export const PREVIEW_GATE_PREFIX = 'preview_flag:'
export const UI_GATES = {
  developer_mode: { setting: 'developer.developer-mode' },
  terminal_enabled: { setting: null },
} as const satisfies Partial<Record<UiConditionId, { setting: string | null }>>

export type UiGateId = keyof typeof UI_GATES

/**
 * Condition pairs that can never hold together: a placement needing both is a
 * contradiction the generator refuses.
 */
export const UI_CONDITION_OPPOSITES = [
  ['has_schedules', 'no_schedules'],
  ['has_crewmates', 'no_crewmates'],
  ['session_open', 'no_active_session'],
  ['response_running', 'no_response_running'],
  ['job_running', 'job_not_running'],
  ['job_enabled', 'job_paused'],
  ['sessions_board_view', 'sessions_list_view'],
  ['mouse_input', 'touch_input'],
  ['terminal_docked_bottom', 'terminal_docked_right'],
] as const satisfies readonly (readonly [UiConditionId, UiConditionId])[]
