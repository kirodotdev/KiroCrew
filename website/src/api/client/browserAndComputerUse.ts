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
 * so an oss build never locks it; an enterprise build ignores the local opt-in and
 * says so with `locked_by_policy`. An unsupported platform is the separate
 * `supported: false` branch. */
export interface ComputerUseConfigData {
  enabled: boolean
  /** True when this build ignores the local opt-in (enterprise); the enable is greyed out. */
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

/** One TCC grant as the agent-desktop status reports it. */
export type AgentDesktopPermission = 'granted' | 'denied' | 'unknown'

/** Which computer-use driver the gateway is running right now. */
export type AgentDesktopDriver = 'agent-desktop' | 'native' | 'unsupported'

/** The gateway-owned agent-desktop install job. `idle` means none has run. */
export interface AgentDesktopJob {
  state: 'idle' | 'running' | 'done' | 'failed'
  /** The installer's last output lines, redacted by the gateway. */
  log_tail: string
  error: string | null
}

/** Answer of GET /api/computer-use/agent-desktop/status (and of the install POST). */
export interface AgentDesktopStatus {
  installed: boolean
  version: string | null
  /** The version this build is pinned to install. */
  pinned: string
  path: string | null
  driver_active: AgentDesktopDriver
  permissions: {
    accessibility: AgentDesktopPermission
    screen_recording: AgentDesktopPermission
  }
  job: AgentDesktopJob
}

export function createBrowserAndComputerUseEndpoints({ get, post, put, j }: ClientTransport) {
  const hostAutomation = {
    // Computer use (desktop automation). The PUT returns the refreshed snapshot so
    // the panel re-renders from server truth rather than its optimistic guess.
    getComputerUseConfig: () => get('/api/computer-use/config').then(j) as Promise<ComputerUseConfigData>,
    saveComputerUseConfig: (body: Partial<ComputerUseConfigSave>) =>
      put('/api/computer-use/config', body).then(j) as Promise<ComputerUseConfigData>,
    // agent-desktop driver install. The install POST starts a gateway-owned job
    // and the card polls the status while `job.state` is `running`.
    getAgentDesktopStatus: () =>
      get('/api/computer-use/agent-desktop/status').then(j) as Promise<AgentDesktopStatus>,
    installAgentDesktop: () =>
      post('/api/computer-use/agent-desktop/install', {}).then(j) as Promise<AgentDesktopStatus>,
    // Opens the OS permission guide on the gateway host. The body is not read.
    openAgentDesktopPermissionsGuide: () =>
      post('/api/computer-use/agent-desktop/permissions-guide', {}).then(j) as Promise<unknown>,
  }

  return { hostAutomation }
}
