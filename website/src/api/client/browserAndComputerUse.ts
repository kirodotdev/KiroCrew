/**
 * Agent-driven UI automation on the gateway host: the computer-use opt-in
 * config. (The agent's browser has its own status and install routes, owned
 * by the browser live harness; nothing here drives it.)
 */

import type { ClientTransport } from './transport'

/** ADVISORY macOS permission rows. Never a gate — macOS attributes a TCC grant
 * to the responsible parent process, so `missing` can coexist with a working
 * capture, and `unknown` means the probe could not be run. */
export interface ComputerUsePermissions {
  accessibility: string
  screen_recording: string
  responsible_hint: string
}

/** Computer-use config as returned by GET /api/computer-use/config.
 *
 * `enabled` comes from the keystone `computer_use.json`, not `config.json`; the
 * numeric fields are the config.json budgets. Computer use has no governance scope,
 * so an oss build never locks it; an LTS build ignores the local opt-in and
 * says so with `locked_by_policy`. An unsupported platform is the separate
 * `supported: false` branch. */
export interface ComputerUseConfigData {
  enabled: boolean
  /** True when this build ignores the local opt-in (LTS); the enable is greyed out. */
  locked_by_policy?: boolean
  supported: boolean
  platform: string
  reason: string
  max_tree_nodes: number
  max_tree_depth: number
  text_limit: number
  attach_screenshot: boolean
  screenshot_max_px: number
  screenshot_jpeg_quality: number
  /** Draw a visible cursor gliding to each real-pointer target. macOS only. */
  cursor_motion: boolean
  /** False off macOS, where there is no overlay to draw — the row is hidden. */
  cursor_motion_supported: boolean
  allowed_apps: string[]
  extra_denied_apps: string[]
  /** Non-empty ONLY when the keystone's policy could not be parsed. The two lists
   *  above are then empty because they were unreadable — not because no restriction
   *  is configured — and the panel must be able to tell those apart. The GET
   *  deliberately still succeeds in that case: a hand-edited keystone used to 500
   *  this endpoint, which made the only UI that can repair the file unreachable. */
  policy_error?: string
  permissions: ComputerUsePermissions
  limits: Record<string, [number, number]>
  /** Sessions restarted by the last PUT so kiro-cli re-reads the tool list.
   *  Only ever non-zero on a save that FLIPPED `enabled` (see the handler);
   *  absent on GET. */
  sessions_reset?: number
  /** Present when the enable changed; false when the agent spec was not rewritten. */
  agent_spec_rewritten?: boolean
}

/** Writable computer-use fields sent to PUT /api/computer-use/config. */
export interface ComputerUseConfigSave {
  enabled: boolean
  max_tree_nodes: number
  max_tree_depth: number
  text_limit: number
  attach_screenshot: boolean
  screenshot_max_px: number
  screenshot_jpeg_quality: number
  cursor_motion: boolean
  allowed_apps: string[]
  extra_denied_apps: string[]
}

/** Wire states of the setup document, as `GET /api/computer-use/setup` names them.
 *  The handler maps anything outside this list to `error`. */
export type ComputerUseSetupState =
  | 'disabled'
  | 'not_configured'
  | 'installing'
  | 'install_failed'
  | 'requesting_accessibility'
  | 'accessibility_denied'
  | 'requesting_screen_recording'
  | 'screen_recording_denied'
  | 'waiting_for_system_settings'
  | 'restart_required'
  | 'validating'
  | 'ready'
  | 'temporarily_unavailable'
  | 'permission_revoked'
  | 'unsupported'
  | 'error'

/** Actions `POST /api/computer-use/setup` accepts. None of them turns the feature
 *  on: that stays the keystone enable behind `PUT /api/computer-use/config`. */
export type ComputerUseSetupAction = 'install' | 'request_permissions' | 'recheck' | 'verify' | 'restart_driver' | 'reset' | 'use_builtin_driver'

export type ComputerUsePermissionValue = 'granted' | 'missing' | 'unknown' | 'not_applicable'

export interface ComputerUseSetupStep {
  id: 'install' | 'session' | 'accessibility' | 'screen_recording' | 'verify' | 'ready'
  status: 'done' | 'current' | 'pending' | 'failed' | 'not_applicable'
  action?: { kind: 'open_pane'; pane: 'accessibility' | 'screen_recording' } | { kind: 'post'; action: ComputerUseSetupAction }
}

/** The setup document. Every key is always present (the handler fills absent
 *  ones), so the UI never guards a missing branch. */
export interface ComputerUseSetupDoc {
  state: ComputerUseSetupState
  platform: 'macos' | 'windows' | 'linux' | 'unknown'
  driver: { kind: 'upstream' | 'native' | 'none'; version: string; installed: boolean; healthy: boolean }
  session: { kind: 'x11' | 'wayland' | 'windows_interactive' | 'session0' | 'aqua' | 'none'; detail: string }
  permissions: {
    accessibility: ComputerUsePermissionValue
    screen_recording: ComputerUsePermissionValue
    /** The app the user must turn on in Privacy & Security, exactly as listed there. */
    owner_name: string
    previously_granted: boolean
  }
  capabilities: {
    observe_tree: boolean
    screenshot: boolean
    background_input: boolean
    foreground_input: boolean
    window_capture: boolean
  }
  /** Plain sentences from the backend, shown as they are. */
  limitations: string[]
  steps: ComputerUseSetupStep[]
  /** One plain sentence saying what the user should do next, or empty. */
  user_action: string
  progress: { last_step: string }
  /** What is wrong now (failure states only); null otherwise. Failure reasons
   *  never travel in `limitations`, which holds standing platform limits. */
  problem?: { code: string; message: string; recoverable: boolean } | null
  /** How the system stopped the upstream driver at launch ("" when it did not). */
  blocked?: string
}

export function createBrowserAndComputerUseEndpoints({ get, put, post, j }: ClientTransport) {
  const hostAutomation = {
    // Computer use (desktop automation). The PUT returns the refreshed snapshot so
    // the panel re-renders from server truth rather than its optimistic guess.
    getComputerUseConfig: () => get('/api/computer-use/config').then(j) as Promise<ComputerUseConfigData>,
    saveComputerUseConfig: (body: Partial<ComputerUseConfigSave>) =>
      put('/api/computer-use/config', body).then(j) as Promise<ComputerUseConfigData>,
    // The setup document. Each POST runs one step and answers with the document
    // that step left behind, so the dialog renders from server truth.
    getComputerUseSetup: () => get('/api/computer-use/setup').then(j) as Promise<ComputerUseSetupDoc>,
    runComputerUseSetupAction: (action: ComputerUseSetupAction) =>
      post('/api/computer-use/setup', { action }).then(j) as Promise<ComputerUseSetupDoc>,
  }

  return { hostAutomation }
}
