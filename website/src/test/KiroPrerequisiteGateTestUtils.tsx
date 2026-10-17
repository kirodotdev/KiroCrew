/**
 * The prerequisite-status and backend-probe fixtures the
 * KiroPrerequisiteGate*.test.tsx files share. Each returns a fresh object, so a
 * test's overrides never reach another test.
 */
import type { KiroPrerequisiteStatus } from '../api/client'
import type { AcpBackendProbe } from '../api/client'

export function probe(overrides: Partial<AcpBackendProbe> = {}): AcpBackendProbe {
  return {
    id: 'claude',
    policy_id: 'claude',
    selectable: true,
    independent_setup: true,
    installed: 'installed',
    missing_components: [],
    install_command: '',
    restart_required: false,
    ...overrides,
  }
}

export function status(overrides: Partial<KiroPrerequisiteStatus> = {}): KiroPrerequisiteStatus {
  return {
    platform: 'Linux',
    installed: false,
    authenticated: false,
    ready: false,
    initial_setup_complete: false,
    repair_required: false,
    docs_url: 'https://kiro.dev/cli/',
    login_command: 'kiro-cli login',
    sso_login_command: 'kiro-cli login --use-device-flow --license pro',
    bundled_cli: false,
    setup_allowed: true,
    sandbox_unavailable: false,
    sandbox_backend_available: true,
    sandbox_failure_kind: '',
    sandbox_detail: '',
    sandbox_remedy: '',
    missing_agent_specs: [],
    agent_spec_repair_error: '',
    ...overrides,
  }
}
