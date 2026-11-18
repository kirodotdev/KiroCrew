// The setup form's provisioner choice, the launch form across the hand-off, the
// per-crew switch and a gateway enabled after it started; split from
// RemoteCrewPanel.test.tsx, whose mocks and fixtures this file repeats.

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from './helpers'
import { RemoteCrewPanel } from '../pages/settings/RemoteCrewPanel'
import { copyToClipboard } from '../utils/clipboard'
import { __resetErrorJournalForTests } from '../utils/errorReport'
import { __resetInstanceFailuresForTests } from '../utils/instanceFailureReport'

vi.mock('../utils/clipboard', () => ({ copyToClipboard: vi.fn() }))

// PARTIAL: the panel also reads the chain-refusal store from this module through
// useSyncExternalStore, so replacing the whole module would break rendering. Only the
// announce is spied, which is the one decision under test.
vi.mock('../lib/chainAnnounce', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../lib/chainAnnounce')>()),
  announceChainedCrew: vi.fn(),
}))

vi.mock('../api/client', () => {
  class ApiError extends Error {
    status: number
    // The real class carries the raw response body so a caller can read the
    // structured `code` the human message collapses away — the panel branches
    // on it to tell an unsupported-platform refusal from a load failure.
    body: string
    constructor(status: number, message: string, body = '') {
      super(message)
      this.status = status
      this.body = body
    }
  }
  return {
    ApiError,
    // Mirrors the real predicate: the panel drops its refresh button only for an
    // auth denial, so the mock must distinguish one from any other ApiError.
    isAuthExpiredError: (e: unknown) =>
      e instanceof ApiError && (e as { authRequired?: boolean }).authRequired === true,
    api: {
      listInstances: vi.fn(),
      addInstance: vi.fn(),
      connectInstance: vi.fn(),
      disconnectInstance: vi.fn(),
      removeInstance: vi.fn(),
      instanceStatus: vi.fn(),
      updateInstance: vi.fn(),
      patchConfig: vi.fn(),
      cloudLaunches: vi.fn(),
      cloudPreflight: vi.fn(),
      cloudProvisioners: vi.fn(),
      cloudIamPolicy: vi.fn(),
      cloudLaunch: vi.fn(),
      cloudIdentity: vi.fn(),
      cloudLaunchStatus: vi.fn(),
      cloudLaunchCancel: vi.fn(),
      cloudLaunchSignin: vi.fn(),
      cloudStop: vi.fn(),
      cloudStart: vi.fn(),
      cloudDestroy: vi.fn(),
    },
  }
})
import { api, ApiError } from '../api/client'
import {
  registerRemoteProvisionerRenderer,
  type RemoteProvisionerFormProps,
} from '../components/remoteProvisionerRenderers'

/** Open a crew row's overflow menu — Edit / Stop / Start / Delete live there. */
async function openRowMenu(u: ReturnType<typeof userEvent.setup>, name: RegExp = /details for/i) {
  const toggle = await screen.findByRole('button', { name })
  if (toggle.getAttribute('aria-expanded') !== 'true') await u.click(toggle)
}

/** Open every crew row's details: the host, the notes and the other actions live there. */
async function expandRows() {
  for (const b of await screen.findAllByRole('button', { name: /^Show details for/ })) fireEvent.click(b)
}


const CLOUD_INSTANCE = {
  id: 'kc1',
  name: 'Kiro Crew Cloud (kc-3f9a)',
  connection_method: 'ssm' as const,
  ssm_target: 'i-0abc123456789def0',
  ssh_host: '',
  aws_profile: '',
  aws_region: 'us-east-1',
  ssm_run_as: '',
  provisioner_id: 'aws_ec2',
  remote_port: 5476,
  local_port: 0,
  ttl: '20h',
  remote_bin: '',
  was_connected: true,
  status: { instance_id: 'i-0abc123456789def0', state: 'connected' as const },
}
const MANUAL_INSTANCE = {
  id: 'm1',
  name: 'dev-box-1',
  connection_method: 'ssh' as const,
  ssm_target: '',
  ssh_host: 'dev-box-1',
  aws_profile: '',
  aws_region: '',
  ssm_run_as: '',
  remote_port: 5476,
  local_port: 0,
  ttl: '20h',
  remote_bin: '',
  was_connected: false,
  status: { instance_id: 'm1', state: 'disconnected' as const },
}
const DONE_JOB = {
  id: 'j-done', tag: 'kc-3f9a', instance_id: 'i-0abc123456789def0', profile: '', region: 'us-east-1',
  provider_id: 'aws_ec2',
  size_key: 'balanced', status: 'done' as const, steps: [], signin: null, created_at: 0, updated_at: 0,
}
const RUNNING_JOB = {
  id: 'j-run', tag: 'kc-4d10', profile: '', region: 'us-east-1', size_key: 'light',
  provider_id: 'aws_ec2',
  status: 'running' as const, signin: null, created_at: 0, updated_at: 0,
  steps: [
    { key: 'preflight', label: 'Checked your AWS setup', state: 'done' as const },
    { key: 'provision', label: 'Created the instance', state: 'done' as const },
    { key: 'install', label: 'Installing Kiro Crew', state: 'active' as const },
    { key: 'connect', label: 'Connect', state: 'pending' as const },
  ],
}
const PREFLIGHT_OK = {
  reachable: true, account: '1234•••7890', arn: 'arn:aws:iam::x:user/dev',
  ec2_reachable: true, cloudformation_reachable: true, ssm_reachable: true,
  session_manager_plugin: true, note: '', detail: '',
}

/** The one row the stock gateway offers. Its kind is drawn by the panel itself. */
const AWS_EC2_ROW = {
  id: 'aws_ec2',
  kind: 'aws_ec2',
  label: 'AWS EC2 in your own account',
  posix_only: true,
  steps: [
    { key: 'preflight', label: 'Check your AWS setup' },
    { key: 'provision', label: 'Create the instance' },
    { key: 'signin', label: 'Sign in to Kiro' },
    { key: 'connect', label: 'Connect' },
  ],
}

// localStorage is cleared too: the panel now persists the AWS profile/region, so a
// test that seeds them would otherwise dictate what later tests probe.
beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(copyToClipboard).mockResolvedValue(true)
  localStorage.clear()
  sessionStorage.clear()
  __resetErrorJournalForTests()
  __resetInstanceFailuresForTests()
  // Every test that reaches the setup tab needs this to settle before the
  // preflight is enabled (the preflight probes AWS, so it waits until the
  // selected provisioner is known to be the built-in one). The stock single-row
  // answer is the default; a test that cares overrides it.
  vi.mocked(api.cloudProvisioners).mockResolvedValue({ provisioners: [AWS_EC2_ROW] })
  vi.mocked(api.cloudIdentity).mockResolvedValue({ identity: { account_type: "BuilderId" }, suggested_target: { license: "", start_url: "", region: "" } })
})

describe('RemoteCrewPanel setup form', () => {
  describe('which provisioner draws the setup tab', () => {
    // The tab used to hardcode the EC2 launcher. The gateway now says which
    // provisioners it offers, and the frontend seam says which of those it can
    // draw — but the stock answer (one built-in row) must leave this tab exactly
    // as it was, because that is every OSS user's experience.
    const DEVSPACE_ROW = {
      id: 'devspace_pdx',
      kind: 'seam_test_panel_devspace',
      label: 'Amazon DevSpace (PDX)',
      posix_only: false,
      steps: [{ key: 'provision', label: 'Claim a pool host' }],
    }
    const DEVSPACE_ROW_2 = { ...DEVSPACE_ROW, id: 'devspace_iad', label: 'Amazon DevSpace (IAD)' }

    /** The edition's form. Registered once for the whole file — the registry is a
     *  module singleton and a second registration of one kind is a collision. */
    const DevSpaceForm = ({ provisioner, launch, launching }: RemoteProvisionerFormProps) => (
      <div>
        <span>Pool: {provisioner.id}</span>
        <button type="button" onClick={() => launch({ size_key: 'pool-small' })}>
          Claim a host
        </button>
        {/* A form that DOES ask for a region, so the panel's fallback can be shown
            not to override one the reader chose in the provisioner's own form. */}
        <button
          type="button"
          onClick={() => launch({ size_key: 'pool-small', region: 'eu-west-2' })}
        >
          Pick Ireland and claim
        </button>
        {launching ? <span>Claiming…</span> : null}
      </div>
    )
    registerRemoteProvisionerRenderer({ kind: DEVSPACE_ROW.kind, component: DevSpaceForm })

    it('shows no selector and the built-in form when EC2 is all the gateway offers', async () => {
      vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [] })
      vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] })
      vi.mocked(api.cloudPreflight).mockResolvedValue(PREFLIGHT_OK)
      const u = userEvent.setup()
      renderWithProviders(<RemoteCrewPanel />)

      await u.click(await screen.findByRole('button', { name: 'Add Crew' })); await u.click(await screen.findByRole('button', { name: /Set up a new Crew/ }))

      // The built-in prerequisites card and size ladder, unchanged.
      expect(await screen.findByText(/Before you start/i)).toBeInTheDocument()
      expect(await screen.findByRole('button', { name: /^Launch$/ })).toBeInTheDocument()
      // One choice is not a choice: no selector, and nothing asking the question.
      expect(screen.queryByText(/Where should the new crew run/i)).not.toBeInTheDocument()
      expect(
        screen.queryByRole('button', { name: /AWS EC2 in your own account/i }),
      ).not.toBeInTheDocument()
    })

    it('offers both rows of a registered kind and posts provider_id for the one picked', async () => {
      vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [] })
      vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] })
      vi.mocked(api.cloudPreflight).mockResolvedValue(PREFLIGHT_OK)
      vi.mocked(api.cloudLaunch).mockResolvedValue({ ...RUNNING_JOB, provider_id: 'devspace_iad' })
      vi.mocked(api.cloudProvisioners).mockResolvedValue({
        provisioners: [AWS_EC2_ROW, DEVSPACE_ROW, DEVSPACE_ROW_2],
      })
      const u = userEvent.setup()
      renderWithProviders(<RemoteCrewPanel />)

      await u.click(await screen.findByRole('button', { name: 'Add Crew' })); await u.click(await screen.findByRole('button', { name: /Set up a new Crew/ }))

      // Every renderable row is offered by its SERVER-authored label; two rows may
      // share one kind, so the selector is per row, not per renderer.
      expect(await screen.findByText(/Where should the new crew run/i)).toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'AWS EC2 in your own account' })).toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'Amazon DevSpace (PDX)' })).toBeInTheDocument()
      const second = screen.getByRole('button', { name: 'Amazon DevSpace (IAD)' })

      await u.click(second)

      // The registered form replaces the EC2 cards, and knows which row it is on.
      expect(await screen.findByText('Pool: devspace_iad')).toBeInTheDocument()
      expect(screen.queryByText(/Before you start/i)).not.toBeInTheDocument()
      // Its own launch reaches the shared mutation, carrying the row's id.
      const preflightsBefore = vi.mocked(api.cloudPreflight).mock.calls.length
      await u.click(screen.getByRole('button', { name: /Claim a host/i }))
      await waitFor(() =>
        expect(api.cloudLaunch).toHaveBeenCalledWith({
          provider_id: 'devspace_iad',
          profile: '',
          // The PANEL's region, inherited. This form asks for none of its own, and
          // every provisioner's preflight refuses an empty one -- reported as
          // `region='' is not a region`, which reads as a malformed value rather
          // than an absent one. So a form that does not ask inherits the region
          // the reader already chose here rather than posting nothing.
          region: 'us-east-1',
          size_key: 'pool-small',
        }),
      )
      // And nothing re-probed AWS on the way: the preflight belongs to the EC2
      // form that was on screen before the switch.
      expect(vi.mocked(api.cloudPreflight).mock.calls.length).toBe(preflightsBefore)
    })

    it('keeps a region the provisioner form supplied rather than the panel one', async () => {
      // The fallback fills a gap; it must not overrule a choice. A form that asks
      // for a region is the reader saying where this crew goes, and the panel's
      // own selection is not that answer.
      vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [] })
      vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] })
      vi.mocked(api.cloudPreflight).mockResolvedValue(PREFLIGHT_OK)
      vi.mocked(api.cloudLaunch).mockResolvedValue({ ...RUNNING_JOB, provider_id: 'devspace_pdx' })
      vi.mocked(api.cloudProvisioners).mockResolvedValue({
        provisioners: [AWS_EC2_ROW, DEVSPACE_ROW],
      })
      const u = userEvent.setup()
      renderWithProviders(<RemoteCrewPanel />)

      await u.click(await screen.findByRole('button', { name: 'Add Crew' })); await u.click(await screen.findByRole('button', { name: /Set up a new Crew/ }))
      await u.click(await screen.findByRole('button', { name: 'Amazon DevSpace (PDX)' }))
      await u.click(await screen.findByRole('button', { name: /Pick Ireland and claim/i }))

      await waitFor(() =>
        expect(api.cloudLaunch).toHaveBeenCalledWith({
          provider_id: 'devspace_pdx',
          profile: '',
          region: 'eu-west-2',
          size_key: 'pool-small',
        }),
      )
    })

    it('never probes AWS when the remembered provisioner is not the AWS one', async () => {
      // The preflight shells out to the AWS CLI on the gateway. A provisioner with
      // no AWS involvement must not trigger it, so the query stays disabled until
      // the selected kind is known to be the built-in one.
      localStorage.setItem('mc-cloud-provisioner', 'devspace_pdx')
      vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [] })
      vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] })
      vi.mocked(api.cloudPreflight).mockResolvedValue(PREFLIGHT_OK)
      vi.mocked(api.cloudProvisioners).mockResolvedValue({
        provisioners: [AWS_EC2_ROW, DEVSPACE_ROW],
      })
      const u = userEvent.setup()
      renderWithProviders(<RemoteCrewPanel />)

      await u.click(await screen.findByRole('button', { name: 'Add Crew' })); await u.click(await screen.findByRole('button', { name: /Set up a new Crew/ }))

      expect(await screen.findByText('Pool: devspace_pdx')).toBeInTheDocument()
      await waitFor(() => expect(api.cloudProvisioners).toHaveBeenCalled())
      expect(api.cloudPreflight).not.toHaveBeenCalled()
    })

    it('falls back to the first renderable row when the remembered one is gone', async () => {
      // A choice the gateway no longer offers must not leave the tab drawing
      // nothing — it resolves exactly as an unset choice does.
      localStorage.setItem('mc-cloud-provisioner', 'devspace_retired')
      vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [] })
      vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] })
      vi.mocked(api.cloudPreflight).mockResolvedValue(PREFLIGHT_OK)
      vi.mocked(api.cloudProvisioners).mockResolvedValue({
        provisioners: [AWS_EC2_ROW, DEVSPACE_ROW],
      })
      const u = userEvent.setup()
      renderWithProviders(<RemoteCrewPanel />)

      await u.click(await screen.findByRole('button', { name: 'Add Crew' })); await u.click(await screen.findByRole('button', { name: /Set up a new Crew/ }))

      // The built-in row is first, so that is what shows.
      expect(await screen.findByText(/Before you start/i)).toBeInTheDocument()
      expect(
        screen.getByRole('button', { name: 'AWS EC2 in your own account' }),
      ).toHaveAttribute('aria-pressed', 'true')
      // And it says so: a silent swap would put the user in front of a different
      // form, and a different bill, than the one they picked.
      expect(screen.getByRole('status')).toHaveTextContent(/no longer offered/i)
      expect(screen.getByRole('status')).toHaveTextContent('AWS EC2 in your own account')
    })

    it('names a second aws_ec2-kind lane in the built-in form launch body', async () => {
      // The built-in form draws EVERY row of the aws_ec2 kind. An edition may
      // register a second one behind its own engine (a different account, a
      // different template); launching it with no provider_id would let the
      // server default to the built-in and provision on the wrong lane.
      const secondEc2 = { ...AWS_EC2_ROW, id: 'ec2_gov', label: 'AWS EC2 (GovCloud account)' }
      vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [] })
      vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] })
      vi.mocked(api.cloudPreflight).mockResolvedValue(PREFLIGHT_OK)
      vi.mocked(api.cloudLaunch).mockResolvedValue({ ...RUNNING_JOB, provider_id: 'ec2_gov' })
      vi.mocked(api.cloudProvisioners).mockResolvedValue({ provisioners: [AWS_EC2_ROW, secondEc2] })
      const u = userEvent.setup()
      renderWithProviders(<RemoteCrewPanel />)

      await u.click(await screen.findByRole('button', { name: 'Add Crew' })); await u.click(await screen.findByRole('button', { name: /Set up a new Crew/ }))
      await u.click(await screen.findByRole('button', { name: 'AWS EC2 (GovCloud account)' }))

      // Same built-in prerequisites and size form, different lane.
      expect(await screen.findByText(/Before you start/i)).toBeInTheDocument()
      const launch = await screen.findByRole('button', { name: /^Launch$/ })
      await waitFor(() => expect(launch).not.toBeDisabled())
      await u.click(launch)
      await waitFor(() =>
        expect(api.cloudLaunch).toHaveBeenCalledWith({
          provider_id: 'ec2_gov', profile: '', region: 'us-east-1', size_key: 'balanced',
        }),
      )
    })

    it('says nothing about a stale choice when the remembered row is still offered', async () => {
      localStorage.setItem('mc-cloud-provisioner', 'aws_ec2')
      vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [] })
      vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] })
      vi.mocked(api.cloudPreflight).mockResolvedValue(PREFLIGHT_OK)
      vi.mocked(api.cloudProvisioners).mockResolvedValue({
        provisioners: [AWS_EC2_ROW, DEVSPACE_ROW],
      })
      const u = userEvent.setup()
      renderWithProviders(<RemoteCrewPanel />)

      await u.click(await screen.findByRole('button', { name: 'Add Crew' })); await u.click(await screen.findByRole('button', { name: /Set up a new Crew/ }))

      expect(await screen.findByText(/Before you start/i)).toBeInTheDocument()
      expect(screen.queryByRole('status')).not.toBeInTheDocument()
    })

    it('shows a notice, not the EC2 form, when the gateway offers only lanes it cannot draw', async () => {
      // A successful answer with zero drawable rows is different from an unknown
      // answer: the EC2 form's Launch would post the absent default and be refused
      // with `unknown_provisioner`, so there is nothing to launch and the tab says
      // why instead of offering a dead button. No AWS probe either.
      vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [] })
      vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] })
      vi.mocked(api.cloudPreflight).mockResolvedValue(PREFLIGHT_OK)
      vi.mocked(api.cloudProvisioners).mockResolvedValue({
        provisioners: [
          { ...DEVSPACE_ROW, id: 'nobody', kind: 'seam_test_no_renderer', label: 'Nobody draws me' },
        ],
      })
      const u = userEvent.setup()
      renderWithProviders(<RemoteCrewPanel />)

      await u.click(await screen.findByRole('button', { name: 'Add Crew' })); await u.click(await screen.findByRole('button', { name: /Set up a new Crew/ }))

      expect(await screen.findByText(/no way to create a crew that this dashboard can draw/i)).toBeInTheDocument()
      expect(screen.queryByText(/Before you start/i)).not.toBeInTheDocument()
      expect(screen.queryByRole('button', { name: /^Launch$/ })).not.toBeInTheDocument()
      // (With no remembered lane the AWS probe fires before the list arrives, on
      // purpose: a stock user must not wait a round-trip for a constant answer.
      // The probe is a read; what this case guards is that no Launch is offered.)
    })

    it('offers EC2 lifecycle controls only for crews an EC2 launch created', async () => {
      // The Stop/Start/Delete menu items call the EC2 routes. A job from another
      // lane that happens to share an instance id shape must not unlock them; the
      // row reads as a hand-added machine with a plain Remove instead.
      const otherLane = {
        ...CLOUD_INSTANCE, id: 'other-lane', name: 'Other lane', ssm_target: 'i-0aaaaaaaaaaaaaaaa',
        status: { instance_id: 'i-0aaaaaaaaaaaaaaaa', state: 'connected' as const },
      }
      vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [otherLane] })
      vi.mocked(api.cloudLaunches).mockResolvedValue({
        jobs: [{ ...DONE_JOB, id: 'j-other', provider_id: 'devspace_pdx', instance_id: 'i-0aaaaaaaaaaaaaaaa', tag: 'kc-other' }],
      })
      const u = userEvent.setup()
      renderWithProviders(<RemoteCrewPanel />)

      expect(await screen.findByText('Other lane')).toBeInTheDocument()
      expect(screen.queryByText('Launched by Kiro Crew')).not.toBeInTheDocument()
      await openRowMenu(u)
      expect(screen.queryByRole('button', { name: /^Stop/i })).not.toBeInTheDocument()
      expect(screen.getByRole('button', { name: /^Remove/i })).toBeInTheDocument()
    })

    it('drops a row whose kind no renderer claims', async () => {
      vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [] })
      vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] })
      vi.mocked(api.cloudPreflight).mockResolvedValue(PREFLIGHT_OK)
      vi.mocked(api.cloudProvisioners).mockResolvedValue({
        provisioners: [
          AWS_EC2_ROW,
          { ...DEVSPACE_ROW, id: 'nobody', kind: 'seam_test_no_renderer', label: 'Nobody draws me' },
        ],
      })
      const u = userEvent.setup()
      renderWithProviders(<RemoteCrewPanel />)

      await u.click(await screen.findByRole('button', { name: 'Add Crew' })); await u.click(await screen.findByRole('button', { name: /Set up a new Crew/ }))

      // One renderable row is left, so there is no selector and no unpickable card.
      expect(await screen.findByText(/Before you start/i)).toBeInTheDocument()
      expect(screen.queryByText('Nobody draws me')).not.toBeInTheDocument()
      expect(screen.queryByText(/Where should the new crew run/i)).not.toBeInTheDocument()
    })

    it('falls back to the built-in form when the provisioners endpoint fails', async () => {
      // The list is presentation, not permission: a gateway too old to answer, or
      // one that errors, must still be able to launch an EC2 crew.
      vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [] })
      vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] })
      vi.mocked(api.cloudPreflight).mockResolvedValue(PREFLIGHT_OK)
      vi.mocked(api.cloudLaunch).mockResolvedValue(RUNNING_JOB)
      vi.mocked(api.cloudProvisioners).mockRejectedValue(new ApiError(404, 'not found'))
      const u = userEvent.setup()
      renderWithProviders(<RemoteCrewPanel />)

      await u.click(await screen.findByRole('button', { name: 'Add Crew' })); await u.click(await screen.findByRole('button', { name: /Set up a new Crew/ }))

      expect(await screen.findByText(/Before you start/i)).toBeInTheDocument()
      expect(screen.queryByText(/Where should the new crew run/i)).not.toBeInTheDocument()
      // The failure is said, not swallowed: an ErrorNotice above the form names
      // it and offers the agent hand-off, while the form itself stays usable.
      expect(await screen.findByRole('alert')).toHaveTextContent(/not found|Could not read which ways/i)
      const launch = await screen.findByRole('button', { name: /^Launch$/ })
      await waitFor(() => expect(launch).not.toBeDisabled())
      await u.click(launch)
      // Byte-identical to the pre-seam body: no provider_id, so the server keeps
      // defaulting it.
      await waitFor(() =>
        expect(api.cloudLaunch).toHaveBeenCalledWith({
          profile: '', region: 'us-east-1', size_key: 'balanced',
        }),
      )
    })
  })

  describe('the launch form survives the hand-off', () => {
    // Every exit from this panel unmounts it, the agent hand-off's navigation
    // included, so a picked size held only in the component silently reverts to the
    // recommended default — a launch the user did not ask for. Persisted like the
    // sibling profile/region fields, which already work this way.
    it('remembers a picked x86 size and re-opens the section that holds it', async () => {
      localStorage.clear()
      ;vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [] } as never)
      const u = userEvent.setup()
      const first = renderWithProviders(<RemoteCrewPanel />)

      await u.click(await screen.findByRole('button', { name: 'Add Crew' })); await u.click(await screen.findByRole('button', { name: /Set up a new Crew/ }))
      // The x86 tiers live behind a disclosure; open it and pick one. Several cards
      // match, so address the list rather than expecting a unique name.
      await u.click(await screen.findByRole('button', { name: /Smaller and x86_64 sizes/i }))
      const choices = await screen.findAllByRole('button', { name: /· x86_64/i })
      const picked = choices[choices.length - 1]
      const pickedName = picked.getAttribute('aria-label') || ''
      await u.click(picked)
      first.unmount()

      renderWithProviders(<RemoteCrewPanel />)
      await u.click(await screen.findByRole('button', { name: 'Add Crew' })); await u.click(await screen.findByRole('button', { name: /Set up a new Crew/ }))
      // Visible WITHOUT touching the disclosure: a remembered size whose card is
      // hidden would drive the launch with nothing on screen saying so.
      const back = await screen.findAllByRole('button', { name: /· x86_64/i })
      const same = back.find(c => c.getAttribute('aria-label') === pickedName)
      expect(same).toBeTruthy()
      expect(same).toHaveAttribute('aria-pressed', 'true')
    })

    it('falls back to the default when the remembered size names no tier', async () => {
      // A value written by an older build must not leave every card unselected while
      // the launch still carries the stale id.
      localStorage.clear()
      localStorage.setItem('mc-cloud-size', 'tier-that-no-longer-exists')
      ;vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [] } as never)
      const u = userEvent.setup()
      renderWithProviders(<RemoteCrewPanel />)

      await u.click(await screen.findByRole('button', { name: 'Add Crew' })); await u.click(await screen.findByRole('button', { name: /Set up a new Crew/ }))
      const arm = await screen.findAllByRole('button', { name: /· arm64/i })
      expect(arm.some(c => c.getAttribute('aria-pressed') === 'true')).toBe(true)
    })
  })
})

describe('RemoteCrewPanel per-crew enable toggle', () => {
  beforeEach(() => {
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] })
    vi.mocked(api.updateInstance).mockResolvedValue({ ...MANUAL_INSTANCE } as never)
    vi.mocked(api.connectInstance).mockResolvedValue({ instance_id: 'm1', state: 'connected' } as never)
  })

  it('turns an enabled crew off without connecting it', async () => {
    vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [MANUAL_INSTANCE] })
    renderWithProviders(<RemoteCrewPanel />)
    await expandRows()
    const sw = await screen.findByRole('switch', { name: 'dev-box-1 enabled' })
    expect(sw).toHaveAttribute('aria-checked', 'true')
    await userEvent.setup().click(sw)
    await waitFor(() => expect(api.updateInstance).toHaveBeenCalledWith('m1', { disabled: true }))
    expect(api.connectInstance).not.toHaveBeenCalled()
  })

  it('gives a disabled crew one control, its switch, and one status', async () => {
    vi.mocked(api.listInstances).mockResolvedValue({
      active: true, warm_set_cap: 5, instances: [{ ...MANUAL_INSTANCE, disabled: true }],
    })
    renderWithProviders(<RemoteCrewPanel />)
    await expandRows()
    const sw = await screen.findByRole('switch', { name: 'dev-box-1 enabled' })
    expect(sw).toHaveAttribute('aria-checked', 'false')
    expect(screen.queryByRole('button', { name: /^Connect/ })).toBeNull()
    expect(screen.queryByRole('button', { name: /Turn on/ })).toBeNull()
    expect(screen.queryByText('Disconnected')).toBeNull()
  })

  it('turns a disabled crew back on from its switch, which reconnects', async () => {
    vi.mocked(api.listInstances).mockResolvedValue({
      active: true, warm_set_cap: 5, instances: [{ ...MANUAL_INSTANCE, disabled: true }],
    })
    renderWithProviders(<RemoteCrewPanel />)
    await expandRows()
    await userEvent.setup().click(await screen.findByRole('switch', { name: 'dev-box-1 enabled' }))
    await waitFor(() => expect(api.connectInstance).toHaveBeenCalledWith('m1'))
    expect(api.updateInstance).toHaveBeenCalledWith('m1', { disabled: false })
  })
})

describe('RemoteCrewPanel enabled after the gateway started', () => {
  it('says enabled-but-not-active and mutes Connect until the restart', async () => {
    vi.mocked(api.listInstances).mockResolvedValue({ active: false, warm_set_cap: 5, instances: [MANUAL_INSTANCE] })
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] })
    renderWithProviders(<RemoteCrewPanel />)

    expect(await screen.findByText('dev-box-1')).toBeInTheDocument()
    const banner = screen.getByText(/not active yet/).closest('[role="status"]') as HTMLElement
    // #22104: say where to run the command and what works after; no "tunnel manager".
    expect(banner.textContent).toBe(
      'Remote crew management is on, but not active yet. Run kirocrew restart in a terminal on the computer this dashboard runs on, not on a remote crew. Then you can connect to your remote crews.',
    )
    expect(screen.queryByText(/Disabled in config/)).not.toBeInTheDocument()

    // #23692: the button must agree with the banner. While not active yet, Connect
    // is disabled (so a click cannot fail) and says why, instead of looking ready.
    const connect = screen.getByRole('button', { name: /^Connect$/i })
    expect(connect).toBeDisabled()
    expect(connect).toHaveAttribute('title', expect.stringMatching(/restart/i))
    expect(api.connectInstance).not.toHaveBeenCalled()

    // The banner stays the one place the restart command appears.
    expect(screen.getAllByText(/not active yet/)).toHaveLength(1)
    expect(screen.getAllByText('kirocrew restart')).toHaveLength(1)
  })

  it('maps a 503 instances_manager_unavailable Connect failure to the restart hint', async () => {
    // Distinct from the muted-while-not-active case above: the list reports
    // active:true (so Connect is enabled and clickable), but the gateway's
    // instance manager is gone, so the connect route rejects 503. The row must
    // show the keyed restart hint, not the raw "instances manager not running".
    vi.mocked(api.listInstances).mockResolvedValue({ active: true, warm_set_cap: 5, instances: [MANUAL_INSTANCE] })
    vi.mocked(api.cloudLaunches).mockResolvedValue({ jobs: [] })
    vi.mocked(api.connectInstance).mockRejectedValue(
      new ApiError(
        503,
        'instances manager not running: restart the gateway (kirocrew restart) to start remote crews',
        JSON.stringify({ error: 'instances manager not running', code: 'instances_manager_unavailable' }),
      ),
    )
    renderWithProviders(<RemoteCrewPanel />)

    expect(await screen.findByText('dev-box-1')).toBeInTheDocument()
    const connect = screen.getByRole('button', { name: /^Connect$/i })
    expect(connect).toBeEnabled()
    await userEvent.setup().click(connect)

    const alert = await screen.findByText('Connect to m1 failed: the gateway needs a restart first. See the note below.')
    expect(alert.textContent).not.toContain('instances manager not running')
  })
})
