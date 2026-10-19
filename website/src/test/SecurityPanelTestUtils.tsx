/**
 * Fixtures shared by SecurityPanel.test.tsx and the suites split from it
 * (SecurityPanel.deniedCommands, .registryTrust and .rail): the rule descriptions and
 * patterns, the denied-commands, posture and no-policy governance snapshots, and the
 * rail's tailnet and file-delivery defaults. Each suite keeps its own vi.mock of the api
 * client, so nothing here imports `api`.
 */
import type { DeniedCommandsData } from '../api/client'
import type { GovernancePolicyData, SecurityPostureData } from '../api/client'

export const PINNED_DESC = 'Blocks EC2 instance termination'
export const TOGGLE_DESC = 'Blocks CloudFormation stack deletion'
export const USER_PATTERN = 'rm -rf /tmp/mine'

/** A second user rule, this one carrying a `note`.
 *
 * `note` is optional on `DeniedUserRule` — rules added before the field existed,
 * and rules added without one, omit it. Keeping BOTH shapes in the one shared
 * snapshot means the present and absent branches are asserted against the same
 * render, so "renders the note" and "renders no note" cannot both pass on a row
 * that unconditionally emits the paragraph. */
export const NOTED_PATTERN = 'curl .* \\| (ba)?sh'
export const USER_NOTE = 'Fetch it, read it, then run it — never pipe a download into a shell.'

/** Default payload for the rail's tailnet read.
 *
 * The suites that import this resolve `api.tailnetStatus` with it before each test. It must
 * RESOLVE rather than merely exist: a bare `vi.fn()` returns undefined, which
 * react-query rejects ("Query data cannot be undefined") and reports once per
 * test, since the rail reads it on mount in all of them. `off` is the right
 * default here — those suites cover the rest of the panel, and the section's own
 * states are covered in SecurityPanel.tailnet.test.tsx.
 */
export const TAILNET_OFF = {
  enabled: false,
  governance_pinned: false,
  host: '',
  origin: '',
  resolved_at: 0,
  state: 'off',
} as const

/**
 * Nothing confirmed, for the same reason TAILNET_OFF exists: the rail reads the
 * flagged-file-delivery state on mount in every test of those suites, so the mock must
 * RESOLVE rather than merely exist. "Not confirmed" is the right default for
 * suites that cover the rest of the panel; the section's own states are covered in
 * SecurityPanel.fileDelivery.test.tsx.
 */
export const CONSENT_NONE = {
  ok: true,
  grantable: ['owner_dashboard'],
  never_grantable: ['channel_upload', 'slack_upload'],
  labels: { owner_dashboard: "This machine's outbox and my own dashboard" },
  grants: { owner_dashboard: null },
} as const

export function snapshot(overrides: Partial<DeniedCommandsData> = {}): DeniedCommandsData {
  return {
    builtins: [
      {
        id: 'aws-destructive-cfn-delete-stack',
        pattern: 'aws.*cloudformation.*delete-stack.*',
        category: 'aws-destructive',
        description: TOGGLE_DESC,
        enabled: true,
        pinned: false,
      },
      {
        id: 'aws-destructive-ec2-terminate-instances',
        pattern: 'aws.*ec2.*terminate-instances.*',
        category: 'aws-destructive',
        description: PINNED_DESC,
        enabled: true,
        pinned: true,
      },
    ],
    user_added: [
      { id: 'user-1', pattern: USER_PATTERN, enabled: true },
      { id: 'user-2', pattern: NOTED_PATTERN, enabled: true, note: USER_NOTE },
    ],
    disable_all: false,
    effective_count: 129,
    governance_locked: false,
    ...overrides,
  }
}

/** A posture snapshot with one short control (no filter box) and one long enough
 *  to trigger both the filter box and the "Show N more" truncation. */
export function posture(overrides: Partial<SecurityPostureData> = {}): SecurityPostureData {
  const many = Array.from({ length: 30 }, (_, i) => ({
    label: `~/.secret-${i}`,
    detail: i === 7 ? 'Needle detail' : 'Third-party credential store',
  }))
  return {
    controls: [
      {
        key: 'redaction_paths',
        label: 'Output redaction',
        unit: 'output paths',
        summary: 'Every boundary where agent output reaches a human.',
        source: 'src/kiro_crew/security.py',
        count: 2,
        items: [
          { label: 'Dashboard live stream', detail: 'chat_runner.py — StreamRedactor' },
          { label: 'Slack messages', detail: 'handler.py — final pass' },
        ],
        unavailable: false,
      },
      {
        key: 'sensitive_paths',
        label: 'Sensitive path blocking',
        unit: 'credential paths',
        summary: 'Paths the agent cannot read or write.',
        source: 'src/kiro_crew/security.py',
        count: many.length,
        items: many,
        unavailable: false,
      },
      {
        key: 'denied_commands',
        label: 'Denied commands',
        unit: 'built-in rules',
        summary: 'Destructive shell operations blocked at the gate.',
        source: 'src/kiro_crew/security.py',
        count: 137,
        items: [{ label: 'Blocks stack deletion', detail: 'aws-destructive' }],
        unavailable: false,
      },
    ],
    counts: { redaction_paths: 2, sensitive_paths: 30, denied_commands: 137 },
    ...overrides,
  }
}

/** No-policy (standalone) governance snapshot: every scope ungoverned. */
export function govNoPolicy(overrides: Partial<GovernancePolicyData> = {}): GovernancePolicyData {
  return {
    version: null,
    has_policy: false,
    profile: null,
    unavailable: false,
    scopes: [
      { scope: 'tools', archetype: 'ruleset', governed: false, source: 'ungoverned', detail: {} },
      { scope: 'commands', archetype: 'ruleset', governed: false, source: 'ungoverned', detail: {} },
    ],
    ...overrides,
  }
}
