import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from './helpers'
import { RemoteCrewPanel } from '../pages/settings/RemoteCrewPanel'
import { __resetErrorJournalForTests, __resetNavSeamForTests, consumeChatHandoff, installSoftNavigate } from '../utils/errorReport'

// Bug Bash row 52: an SSM connect IAM refused reads as one plain sentence; AWS's
// own text (ARNs and account IDs, never redacted) is folded under it.
vi.mock('../api/client', () => {
  class ApiError extends Error {
    status: number
    body: string
    constructor(status: number, message: string, body = '') {
      super(message)
      this.status = status
      this.body = body
    }
  }
  return {
    ApiError,
    isAuthExpiredError: () => false,
    api: {
      listInstances: vi.fn(),
      connectInstance: vi.fn(),
      cloudLaunches: vi.fn(),
      cloudProvisioners: vi.fn(),
      cloudIdentity: vi.fn(),
    },
  }
})
import { api, ApiError } from '../api/client'

const RAW = 'IAM denied ssm:StartSession for this target: aws: [ERROR]: An error occurred (AccessDeniedException) '
  + 'when calling the StartSession operation: User: arn:aws:sts::000000000000:assumed-role/no-ssm/s is not '
  + 'authorized to perform: ssm:StartSession on resource: arn:aws:ec2:us-east-1:000000000000:instance/i-0123456789abcdef0'
const SUMMARY = /This AWS role isn't allowed to start an SSM session on this instance/

const SSM_CREW = {
  id: 's1',
  name: 'ssm-box',
  connection_method: 'ssm' as const,
  ssm_target: 'i-0123456789abcdef0',
  ssh_host: '',
  aws_profile: 'no-ssm',
  aws_region: 'us-east-1',
  ssm_run_as: '',
  remote_port: 5476,
  local_port: 0,
  ttl: '20h',
  remote_bin: '',
  was_connected: false,
}

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  sessionStorage.clear()
  vi.mocked(api.cloudProvisioners).mockResolvedValue({ provisioners: [] })
  vi.mocked(api.cloudIdentity).mockResolvedValue({
    identity: { account_type: 'BuilderId' },
    suggested_target: { license: '', start_url: '', region: '' },
  })
  vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] })
  __resetErrorJournalForTests()
  __resetNavSeamForTests()
  installSoftNavigate(() => {})
})

function expectFolded(scope: HTMLElement) {
  const raw = within(scope).getByTestId('remote-crew-raw-error')
  expect(raw.tagName).toBe('DETAILS')
  expect(raw).not.toHaveAttribute('open')
  expect(raw).toHaveTextContent('arn:aws:sts::000000000000:assumed-role/no-ssm/s')
}

describe('RemoteCrewPanel: an SSM connect IAM refused (row 52)', () => {
  it("the row's Why says it plainly and folds the AWS text", async () => {
    vi.mocked(api.listInstances).mockResolvedValue({
      active: true,
      warm_set_cap: 5,
      instances: [{ ...SSM_CREW, status: { instance_id: 's1', state: 'error', error: RAW, error_code: 'ssm_start_session_denied' } }],
    })
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await u.click(await screen.findByRole('button', { name: /details for ssm-box/i }))
    const row = document.querySelector('[data-crew-id="s1"]') as HTMLElement
    expect(within(row).getByText(SUMMARY)).toBeInTheDocument()
    expectFolded(row)
    // Nothing on this page journaled the failure (no Connect was pressed), and the
    // hand-off still gives the agent AWS's own words, not only the sentence.
    await u.click(within(row).getByRole('button', { name: /Ask the agent/ }))
    expect(consumeChatHandoff()).toContain('arn:aws:sts::000000000000:assumed-role/no-ssm/s')
  })

  it('the Connect failure banner says it plainly and folds the AWS text', async () => {
    vi.mocked(api.listInstances).mockResolvedValue({
      active: true,
      warm_set_cap: 5,
      instances: [{ ...SSM_CREW, status: { instance_id: 's1', state: 'disconnected' } }],
    })
    vi.mocked(api.connectInstance).mockRejectedValue(
      new ApiError(502, RAW, JSON.stringify({ error: RAW, error_code: 'ssm_start_session_denied', code: 'instance_connect_failed' })),
    )
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await u.click(await screen.findByRole('button', { name: /^Connect$/i }))
    const summary = await screen.findByText(/Connect to s1 failed: This AWS role isn't allowed/)
    const notice = summary.closest('[role="alert"]') as HTMLElement
    expect(summary).not.toHaveTextContent('AccessDeniedException')
    expectFolded(notice)
  })

  it('a failure with no code still shows the raw text as before', async () => {
    vi.mocked(api.listInstances).mockResolvedValue({
      active: true,
      warm_set_cap: 5,
      instances: [{ ...SSM_CREW, status: { instance_id: 's1', state: 'error', error: 'ssh exited 255: boom' } }],
    })
    const u = userEvent.setup()
    renderWithProviders(<RemoteCrewPanel />)
    await u.click(await screen.findByRole('button', { name: /details for ssm-box/i }))
    const row = document.querySelector('[data-crew-id="s1"]') as HTMLElement
    expect(within(row).getByText('ssh exited 255: boom')).toBeInTheDocument()
    expect(within(row).queryByTestId('remote-crew-raw-error')).toBeNull()
  })
})
