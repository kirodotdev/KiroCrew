import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import type { CronJob } from '../types'
import { renderWithProviders } from '../test/helpers'
import { JobSecretsPanel } from './SchedulePage'

vi.mock('../api/client', () => ({
  api: {
    cronSecretsGrant: vi.fn(),
    cronScript: vi.fn(),
    secretsList: vi.fn(),
  },
}))

// SimpleSelect is Radix-backed on non-touch devices; driving its portal menu in
// jsdom tests the library, not this panel. A native stand-in keeps the test on
// the panel's own wiring (options in, value out).
vi.mock('../components/SimpleSelect', () => ({
  default: ({
    options,
    value,
    onChange,
    'aria-label': ariaLabel,
  }: {
    options: string[]
    value: string
    onChange: (v: string) => void
    'aria-label'?: string
  }) => (
    <select
      aria-label={ariaLabel ?? 'select'}
      value={value}
      onChange={e => onChange(e.target.value)}
    >
      <option value="" aria-label="empty" />
      {options.map(o => (
        <option key={o} value={o}>
          {o}
        </option>
      ))}
    </select>
  ),
}))

const BASE_JOB: CronJob = {
  id: 'abc12345',
  name: 'escalation-watch',
  message: 'args',
  enabled: true,
  schedule: 'every 3600s',
  last_status: 'ok',
  command: 'echo hi',
}

const SOURCE_A = {
  source: 'def run(ctx): pass\n',
  file: 'grantee.py',
  function: 'run',
  truncated: false,
  reviewable: true,
  sha256: 'a'.repeat(64),
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(api.secretsList).mockResolvedValue({ names: ['slack-sandbox', 'jira-token'] })
  vi.mocked(api.cronSecretsGrant).mockResolvedValue({ ok: true })
  vi.mocked(api.cronScript).mockResolvedValue(SOURCE_A)
})

describe('JobSecretsPanel', () => {
  it('renders the pending request and approves through the grant endpoint', async () => {
    const onSaved = vi.fn()
    renderWithProviders(
      <JobSecretsPanel
        job={{ ...BASE_JOB, secret_env_pending: { MY_SANDBOX_TOKEN: 'slack-sandbox' } }}
        onSaved={onSaved}
      />,
    )
    // The banner shows names only — env key and vault name.
    expect(screen.getByText(/MY_SANDBOX_TOKEN/)).toBeInTheDocument()
    expect(screen.getByText('Agent requested secrets')).toBeInTheDocument()
    // Approval is gated on the script the request would run being SHOWN:
    // the button stays disabled until the source has rendered.
    expect(screen.getByText('Approve').closest('button')).toBeDisabled()
    await waitFor(() => expect(screen.getByText('Approve').closest('button')).toBeEnabled())
    // CodeBlock renders the text twice while staging its highlighter (plain
    // stand-in + highlighted swap), so assert presence, not uniqueness.
    expect(screen.getAllByText(/def run\(ctx\): pass/).length).toBeGreaterThan(0)
    await userEvent.click(screen.getByText('Approve'))
    await waitFor(() =>
      expect(api.cronSecretsGrant).toHaveBeenCalledWith('abc12345', {
        approve_pending: true,
        expected_secret_env: { MY_SANDBOX_TOKEN: 'slack-sandbox' },
        expected_ts: undefined,
        // The digest of exactly the source rendered in the banner.
        expected_source_sha256: SOURCE_A.sha256,
      }),
    )
    await waitFor(() => expect(onSaved).toHaveBeenCalled())
  })

  it('never enables approval when the source cannot be shown', async () => {
    vi.mocked(api.cronScript).mockRejectedValue(new Error('script unreadable'))
    renderWithProviders(
      <JobSecretsPanel
        job={{ ...BASE_JOB, secret_env_pending: { MY_SANDBOX_TOKEN: 'slack-sandbox' } }}
        onSaved={vi.fn()}
      />,
    )
    await waitFor(() => expect(screen.getByRole('alert')).toBeInTheDocument())
    expect(screen.getByText('Approve').closest('button')).toBeDisabled()
    // A truncated source is equally unreviewable.
    vi.mocked(api.cronScript).mockResolvedValue({ ...SOURCE_A, truncated: true, reviewable: false })
    renderWithProviders(
      <JobSecretsPanel
        job={{ ...BASE_JOB, id: 'trunc0001', secret_env_pending: { X: 'slack-sandbox' } }}
        onSaved={vi.fn()}
      />,
    )
    await waitFor(() => expect(screen.getAllByRole('alert').length).toBe(2))
    expect(screen.getAllByText('Approve')[1].closest('button')).toBeDisabled()
    // ...as is a source the server could not render verbatim (redaction
    // masked a span, or undecodable bytes): the digest would cover code the
    // operator cannot read, so the button stays disabled and says why.
    vi.mocked(api.cronScript).mockResolvedValue({ ...SOURCE_A, reviewable: false })
    renderWithProviders(
      <JobSecretsPanel
        job={{ ...BASE_JOB, id: 'masked001', secret_env_pending: { Y: 'slack-sandbox' } }}
        onSaved={vi.fn()}
      />,
    )
    await waitFor(() => expect(screen.getAllByRole('alert').length).toBe(3))
    expect(screen.getByText(/cannot be shown as written/)).toBeInTheDocument()
    expect(screen.getAllByText('Approve')[2].closest('button')).toBeDisabled()
    expect(api.cronSecretsGrant).not.toHaveBeenCalled()
  })

  it('reloads the source when the pending request is re-issued', async () => {
    const job: CronJob = {
      ...BASE_JOB,
      secret_env_pending: { MY_SANDBOX_TOKEN: 'slack-sandbox' },
      secret_env_pending_ts: 1,
    }
    const { rerender } = renderWithProviders(<JobSecretsPanel job={job} onSaved={vi.fn()} />)
    await waitFor(() => expect(screen.getByText('Approve').closest('button')).toBeEnabled())
    expect(api.cronScript).toHaveBeenCalledTimes(1)
    // The agent rewrote the script and re-issued the request: the job refresh
    // carries a new timestamp, and the banner must fetch the NEW source
    // rather than keep showing the old one under the new request.
    const SOURCE_B = { ...SOURCE_A, source: 'def run(ctx): return "B"\n', sha256: 'b'.repeat(64) }
    vi.mocked(api.cronScript).mockResolvedValue(SOURCE_B)
    rerender(<JobSecretsPanel job={{ ...job, secret_env_pending_ts: 2 }} onSaved={vi.fn()} />)
    await waitFor(() => expect(api.cronScript).toHaveBeenCalledTimes(2))
    // The approve call carries the NEW revision's digest — proof that the
    // banner re-bound to the re-issued request's source, not the cached one.
    await waitFor(() => expect(screen.getByText('Approve').closest('button')).toBeEnabled())
    await userEvent.click(screen.getByText('Approve'))
    await waitFor(() =>
      expect(api.cronSecretsGrant).toHaveBeenCalledWith(
        'abc12345',
        expect.objectContaining({ expected_ts: 2, expected_source_sha256: SOURCE_B.sha256 }),
      ),
    )
  })

  it('denies a pending request through the grant endpoint', async () => {
    renderWithProviders(
      <JobSecretsPanel
        job={{ ...BASE_JOB, secret_env_pending: { MY_SANDBOX_TOKEN: 'slack-sandbox' } }}
        onSaved={vi.fn()}
      />,
    )
    await userEvent.click(screen.getByText('Deny'))
    await waitFor(() =>
      expect(api.cronSecretsGrant).toHaveBeenCalledWith('abc12345', {
        deny_pending: true,
        expected_secret_env: { MY_SANDBOX_TOKEN: 'slack-sandbox' },
      }),
    )
  })

  it('shows the active grant read-only and revokes it whole after arming', async () => {
    renderWithProviders(
      <JobSecretsPanel job={{ ...BASE_JOB, secret_env: { OLD_TOKEN: 'jira-token' } }} onSaved={vi.fn()} />,
    )
    expect(screen.queryByText('Agent requested secrets')).not.toBeInTheDocument()
    await userEvent.click(screen.getByText('Vault secrets'))
    expect(screen.getByText('OLD_TOKEN')).toBeInTheDocument()
    // The arrow alone carried the direction; the caption states it in words.
    expect(screen.getByText('environment variable ← vault secret')).toBeInTheDocument()
    // Direct grant editing is gone: the request->approve flow is the only
    // mint path, so the panel offers display + whole-grant revoke only.
    expect(screen.queryByText('Save grants')).not.toBeInTheDocument()
    // Revoke is arm-then-confirm: the first click only relabels the button
    // (recovery needs a fresh agent request and a re-approval), the second
    // performs the revoke.
    await userEvent.click(screen.getByText('Revoke all secrets'))
    expect(api.cronSecretsGrant).not.toHaveBeenCalled()
    await userEvent.click(screen.getByText('Revoke all?'))
    await waitFor(() =>
      expect(api.cronSecretsGrant).toHaveBeenCalledWith('abc12345', { secret_env: {} }),
    )
  })

  it('tells a first-time user where a grant comes from when none exists', async () => {
    renderWithProviders(<JobSecretsPanel job={BASE_JOB} onSaved={vi.fn()} />)
    await userEvent.click(screen.getByText('Vault secrets'))
    // Grants are minted only through an agent request, so the empty state
    // names that path instead of ending at "No secrets granted."
    expect(screen.getByText(/Ask the agent to request secrets for this job\./)).toBeInTheDocument()
    expect(screen.queryByText('Revoke all secrets')).not.toBeInTheDocument()
  })
})
