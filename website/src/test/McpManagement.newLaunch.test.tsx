// MCP Management: a launch added beside an approved one is not a changed command.
//
// Contract under test:
// - a `changed_needs_reapproval` refusal whose live launches still include every
//   approved command/env pair, plus at least one more, reads
//   "Paused · new launch to approve", and its Details says the approved launch
//   did not change
// - an approved launch that really changed keeps "Paused · command changed",
//   with or without another launch beside it
// - a refusal that cannot show both sides in full (no approved env displays, a
//   partial launch, no identity, an approved launch shown with a redaction tag)
//   never reads as a new launch
// - the row keeps the same Approve update button, pinned to the refusal's own
//   identity, and the same Before/After Details
//
// Every label is read from the catalog, so a wording change never breaks a test.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { api } from '../api/client'
import { i18nT } from '../i18n/t'
import { McpManagement } from '../pages/settings/McpManagement'
import { changedHintKey, stateLabelKey, type LaunchRefusal } from '../pages/settings/mcpManagementModel'
import { answerBatchPreviewsPerName } from './McpManagementTestUtils'

/** The page's own English copy. */
const M = (key: string, vars?: Record<string, unknown>) => i18nT(`pages.mcpManagement.${key}`, vars)

/** A row control's accessible name: its visible label, then the row's server name. */
const rowName = (labelKey: string, server: string) => `${M(labelKey)} ${server}`

/** A launch block's text as drawn: the tokens are separate spans inside one <pre>. */
const pres = (root: HTMLElement) => Array.from(root.querySelectorAll('pre')).map(p => p.textContent)

const KEY = (key: string) => `pages.mcpManagement.${key}`

/** A complete changed-launch refusal whose approved launch is `/opt/a --one` with no env. */
function refusal(over: Partial<LaunchRefusal> = {}): LaunchRefusal {
  return {
    reason: 'changed_needs_reapproval',
    complete: true,
    expected_launch: 'pinned-identity',
    commands: [['/opt/a', '--one']],
    envs: [[]],
    approved_commands: [['/opt/a', '--one']],
    approved_envs: [[]],
    ...over,
  }
}

/** The approved launch still runs, and a second agent added its own. */
const ADDED = refusal({
  commands: [
    ['/opt/a', '--one'],
    ['/opt/a', '--two'],
  ],
  envs: [[], []],
})

/** Two approved launches, as after one Approve update on a row two agents declared. */
const TWO_APPROVED: Partial<LaunchRefusal> = {
  approved_commands: [
    ['/opt/a', '--one'],
    ['/opt/a', '--two'],
  ],
  approved_envs: [[], []],
}

describe('which refusal reads as a new launch', () => {
  it('names a launch added beside the approved one', () => {
    expect(stateLabelKey('needs_reapproval', ADDED)).toBe(KEY('state_new_launch'))
    expect(changedHintKey(ADDED)).toBe(KEY('new_launch_hint'))
  })

  it('names a launch added beside two approved ones', () => {
    const added = refusal({
      ...TWO_APPROVED,
      commands: [
        ['/opt/a', '--one'],
        ['/opt/a', '--two'],
        ['/opt/a', '--three'],
      ],
      envs: [[], [], []],
    })
    expect(stateLabelKey('needs_reapproval', added)).toBe(KEY('state_new_launch'))
    expect(changedHintKey(added)).toBe(KEY('new_launch_hint'))
  })

  it.each<[string, Partial<LaunchRefusal>, string]>([
    ['the approved launch changed', { commands: [['/opt/a', '--two']] }, 'command_changed_hint_command'],
    [
      'the approved launch changed and another was added',
      { commands: [['/opt/a', '--two'], ['/opt/a', '--three']], envs: [[], []] },
      'command_changed_hint_command',
    ],
    [
      'one of two approved launches changed',
      {
        ...TWO_APPROVED,
        commands: [
          ['/opt/a', '--one'],
          ['/opt/a', '--three'],
        ],
        envs: [[], []],
      },
      'command_changed_hint_command',
    ],
    [
      "the approved launch's settings changed beside an added launch",
      {
        commands: [['/opt/a', '--one'], ['/opt/a', '--two']],
        envs: [['MODE=new'], []],
        approved_envs: [['MODE=old']],
      },
      'command_changed_hint_command',
    ],
    ['nothing was added', {}, 'command_changed_hint'],
    ['the approved env displays are missing', { ...ADDED, approved_envs: undefined }, 'command_changed_hint_command'],
    ['the live env displays do not pair up', { ...ADDED, envs: [[]] }, 'command_changed_hint_command'],
    // Two different values read as the same redaction tag, so these displays
    // cannot show that the approved launch did not change.
    [
      'the approved launch shows a redacted credential',
      {
        commands: [
          ['/opt/a', '--token=[REDACTED: credential]'],
          ['/opt/a', '--two'],
        ],
        envs: [[], []],
        approved_commands: [['/opt/a', '--token=[REDACTED: credential]']],
      },
      'command_changed_hint_command',
    ],
    [
      "the approved launch's env shows a redacted URL",
      {
        ...ADDED,
        envs: [['API_URL=[REDACTED: suspicious URL to example.invalid]'], []],
        approved_envs: [['API_URL=[REDACTED: suspicious URL to example.invalid]']],
      },
      'command_changed_hint_command',
    ],
  ])('keeps "command changed" when %s', (_case, over, hint) => {
    const changed = refusal(over)
    expect(stateLabelKey('needs_reapproval', changed)).toBe(KEY('state_needs_reapproval'))
    expect(changedHintKey(changed)).toBe(KEY(hint))
  })

  it.each<[string, Partial<LaunchRefusal>]>([
    ['a partial refusal', { complete: false }],
    ['a refusal without an identity', { expected_launch: undefined }],
  ])('never calls %s a new launch', (_case, over) => {
    expect(stateLabelKey('needs_reapproval', { ...ADDED, ...over })).toBe(KEY('state_no_stub'))
  })

  it('leaves a server never approved here as not approved yet', () => {
    const unapproved: LaunchRefusal = { ...ADDED, reason: 'added_outside_dashboard' }
    expect(stateLabelKey('needs_reapproval', unapproved)).toBe(KEY('state_not_approved_yet'))
  })
})

function server(over: Record<string, unknown> = {}) {
  return {
    name: 'alpha-mcp',
    stub: true,
    can_stub: true,
    in_allowlist: true,
    entry_poolable: false,
    agents: ['kirocrew', 'second-agent'],
    transport: 'stdio',
    denylisted: false,
    ...over,
  }
}

const status = (over: Record<string, unknown> = {}) => ({
  enabled: true,
  stub: ['alpha-mcp'],
  stub_count: 1,
  running: false,
  ping_ok: false,
  supported: true,
  ...over,
})

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  return render(
    <MemoryRouter>
      <QueryClientProvider client={qc}>
        <McpManagement />
      </QueryClientProvider>
    </MemoryRouter>,
  )
}

async function openDetails(name = 'alpha-mcp') {
  const toggle = await screen.findByRole('button', { name: M('details_aria', { name }) }, { timeout: 5000 })
  fireEvent.click(toggle)
  const id = toggle.getAttribute('aria-controls') as string
  await waitFor(() => expect(document.getElementById(id)).not.toBeNull(), { timeout: 5000 })
  return document.getElementById(id) as HTMLElement
}

beforeEach(() => {
  vi.restoreAllMocks()
  vi.spyOn(api, 'mcpGatewayFootprint').mockResolvedValue({
    using_now_bytes: null,
    with_sharing_bytes: null,
    open_chats: 0,
    stale_chats: [],
  } as never)
  answerBatchPreviewsPerName()
})
afterEach(cleanup)

describe('a row paused by an added launch', () => {
  it('reads as a new launch to approve and approves the refused identity', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(
      status({
        launch_refused: {
          'alpha-mcp': {
            ...ADDED,
            commands: [
              ['/opt/alpha', '--one'],
              ['/opt/alpha', '--two'],
            ],
            approved_commands: [['/opt/alpha', '--one']],
          },
        },
      }) as never,
    )
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server()] } as never)
    const preview = vi.spyOn(api, 'mcpGatewayLaunchPreview')
    const setStub = vi.spyOn(api, 'mcpGatewaySetStub').mockResolvedValue({ ok: true, name: 'alpha-mcp', stub: true } as never)

    mount()
    const approve = await screen.findByRole('button', { name: rowName('new_command_button', 'alpha-mcp') }, { timeout: 5000 })
    expect(screen.getAllByText(M('state_new_launch')).length).toBeGreaterThan(0)
    expect(screen.queryByText(M('state_needs_reapproval'))).toBeNull()

    const details = await openDetails()
    expect(within(details).getByText(M('new_launch_hint'))).toBeTruthy()
    expect(within(details).queryByText(M('command_changed_hint_command'))).toBeNull()
    // The same Before/After the page shows for any changed launch.
    expect(within(details).getByText(M('details_approved'))).toBeTruthy()
    expect(within(details).getByText(M('details_new'))).toBeTruthy()
    expect(pres(details)).toEqual(['/opt/alpha --one', '/opt/alpha --one\n/opt/alpha --two'])

    fireEvent.click(approve)
    await waitFor(() => expect(setStub).toHaveBeenCalledWith('alpha-mcp', true, 'pinned-identity'), { timeout: 5000 })
    expect(setStub).toHaveBeenCalledTimes(1)
    expect(preview).not.toHaveBeenCalled()
  })

  it('keeps "command changed" for a row whose approved launch changed', async () => {
    vi.spyOn(api, 'mcpGatewayStatus').mockResolvedValue(
      status({
        launch_refused: {
          'alpha-mcp': refusal({
            commands: [['/opt/alpha', '--two']],
            approved_commands: [['/opt/alpha', '--one']],
          }),
        },
      }) as never,
    )
    vi.spyOn(api, 'mcpGatewayServers').mockResolvedValue({ servers: [server()] } as never)

    mount()
    await screen.findByRole('button', { name: rowName('new_command_button', 'alpha-mcp') }, { timeout: 5000 })
    expect(screen.getAllByText(M('state_needs_reapproval')).length).toBeGreaterThan(0)
    expect(screen.queryByText(M('state_new_launch'))).toBeNull()
    const details = await openDetails()
    expect(within(details).getByText(M('command_changed_hint_command'))).toBeTruthy()
  })
})
