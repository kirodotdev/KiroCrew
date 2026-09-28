/**
 * Your crew in the cloud: the panel's own guards and its five states.
 *
 * The helpers are asserted DIRECTLY as well as through the rendered panel: each
 * one is a decision with a wrong answer that is invisible on screen (an
 * unreachable machine offered as reachable looks identical to a reachable one;
 * a stale attempt described as the current one reads exactly like the real
 * one), and a pure call cannot pass by finding a healthy sibling row.
 *
 * The render tests pin one claim per state, plus the claim that binds them: a
 * failed read and "no launch yet" must never render the same thing. Saying
 * "your crew runs only on this computer" when the truth is "we could not find
 * out" tells a reader their crew is not deployed, which is the one wrong answer
 * this panel can give that a reader would act on.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'
import DeployMyCrewDialog, {
  deployAge,
  deployHasShellTarget,
  deployRunsAsTask,
  deployStepLabel,
  earlierReachableLaunch,
  deployLiveness,
  awsConsoleUrl,
  deployIsReachable,
  deployProgress,
  deployView,
  newestLaunch,
} from './DeployMyCrew'
import { ApiError, type LaunchJob, type LaunchJobStatus, type LaunchStep } from '../../api/client'

const cloudLaunches = vi.fn()
const listInstances = vi.fn()
const cloudLaunchTask = vi.fn()
vi.mock('../../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/client')>()
  return {
    ...actual,
    api: {
      cloudLaunches: (...a: unknown[]) => cloudLaunches(...a),
      listInstances: (...a: unknown[]) => listInstances(...a),
      cloudLaunchTask: (...a: unknown[]) => cloudLaunchTask(...a),
    },
  }
})

const navigateSpy = vi.fn()
vi.mock('react-router-dom', async (importOriginal) => {
  const actual = await importOriginal<typeof import('react-router-dom')>()
  return { ...actual, useNavigate: () => navigateSpy }
})

/** A `done` job carrying a machine, so a test that wants anything else has to
 *  say which field it changed. */
function job(over: Partial<LaunchJob> = {}): LaunchJob {
  return {
    id: 'j1',
    provider_id: 'aws_ec2',
    profile: 'dev',
    region: 'us-west-2',
    size_key: 'small',
    tag: 'crew-alpha',
    status: 'done',
    steps: [],
    instance_id: 'i-0abc123def4567890',
    created_at: 1_700_000_000,
    updated_at: 1_700_000_000,
    ...over,
  }
}

/** The registry as the EC2 launcher leaves it: the fixture's machine, registered
 *  by its instance id as the SSM target. */
const REGISTERED = [{ ssm_target: 'i-0abc123def4567890', ssh_host: '' }]
/** The registry after a Settings delete confirmed the stack was gone. */
const EMPTY_REGISTRY: { ssm_target: string; ssh_host: string }[] = []

function step(state: LaunchStep['state']): LaunchStep {
  return { key: state, label: state, state }
}

const MEMBERS = [{ name: 'Ada' }, { name: 'Grace' }]

describe('deployIsReachable', () => {
  it('is true only for a finished launch that carries a machine', () => {
    expect(deployIsReachable(job())).toBe(true)
  })

  it('is false while the launch is still working', () => {
    for (const status of ['pending', 'running', 'awaiting_signin'] as LaunchJobStatus[]) {
      expect(deployIsReachable(job({ status })), status).toBe(false)
    }
  })

  // The case worth the test: a failed or cancelled attempt can still carry the
  // instance_id of the machine it got as far as creating. Offering that as
  // reachable sends the reader at something half-built or already gone, and
  // every field except `status` reads exactly like the healthy row.
  it('is false for a failed or cancelled launch even when it kept its machine', () => {
    for (const status of ['failed', 'cancelled'] as LaunchJobStatus[]) {
      const j = job({ status })
      expect(j.instance_id, 'fixture must keep the machine or this proves nothing').toBeTruthy()
      expect(deployIsReachable(j), status).toBe(false)
    }
  })

  it('is false for a finished launch with no machine recorded', () => {
    expect(deployIsReachable(job({ instance_id: undefined }))).toBe(false)
  })
})

describe('deployHasShellTarget', () => {
  it('is true for a finished EC2 launch that kept its machine', () => {
    expect(deployHasShellTarget(job())).toBe(true)
  })

  it('treats a record with no provisioner id as EC2, the only kind that predates the field', () => {
    expect(deployHasShellTarget(job({ provider_id: undefined as unknown as string }))).toBe(true)
  })

  // The Fargate lane writes the task ARN into instance_id. Session Manager
  // does not accept an ARN, so that launch is reachable yet has no address.
  it('is false for a finished Fargate launch even though it is reachable', () => {
    const j = job({ provider_id: 'aws_fargate', instance_id: 'arn:aws:ecs:us-west-2:1:task/c/t' })
    expect(deployIsReachable(j)).toBe(true)
    expect(deployHasShellTarget(j)).toBe(false)
  })

  it('is false whenever the launch is not reachable', () => {
    expect(deployHasShellTarget(job({ status: 'failed' }))).toBe(false)
    expect(deployHasShellTarget(job({ instance_id: undefined }))).toBe(false)
  })
})

describe('deployRunsAsTask', () => {
  it('is true for the Fargate lane', () => {
    expect(deployRunsAsTask(job({ provider_id: 'aws_fargate' }))).toBe(true)
  })

  it('is false for EC2, and for a record that predates the provisioner field', () => {
    expect(deployRunsAsTask(job())).toBe(false)
    expect(deployRunsAsTask(job({ provider_id: undefined as unknown as string }))).toBe(false)
  })

  // Keyed on the Fargate id, not on "anything but EC2": a provisioner this
  // panel does not know is drawn verbatim in Details and gets no sentence,
  // because "runs as a container task" could be false of it.
  it('is false for a provisioner the panel does not know', () => {
    expect(deployRunsAsTask(job({ provider_id: 'some_future_lane' }))).toBe(false)
  })

  // Not the negation of deployHasShellTarget: an EC2 launch with no machine has
  // no target either, but "runs as a container task" would be false of it.
  it('stays false for an EC2 launch that recorded no machine', () => {
    const j = job({ instance_id: undefined })
    expect(deployHasShellTarget(j)).toBe(false)
    expect(deployRunsAsTask(j)).toBe(false)
  })
})

describe('newestLaunch', () => {
  it('is undefined for no launches', () => {
    expect(newestLaunch([])).toBeUndefined()
  })

  // The API returns newest first today; the panel must not depend on it. A list
  // handed over oldest-first would otherwise make a stale attempt the story.
  it('picks by created_at, not by list order', () => {
    const older = job({ id: 'old', created_at: 100 })
    const newer = job({ id: 'new', created_at: 200 })
    expect(newestLaunch([older, newer])?.id).toBe('new')
    expect(newestLaunch([newer, older])?.id).toBe('new')
  })
})

describe('deployView', () => {
  it('is none with no launches', () => {
    expect(deployView([], REGISTERED)).toEqual({ kind: 'none' })
  })

  it('is deploying for a pending or running launch', () => {
    for (const status of ['pending', 'running'] as LaunchJobStatus[]) {
      expect(deployView([job({ status })], REGISTERED).kind, status).toBe('deploying')
    }
  })

  // Split from the other moving statuses on purpose: waiting achieves nothing
  // here, because the launch is waiting on the user.
  it('is signin, not deploying, while the launch waits for the user', () => {
    expect(deployView([job({ status: 'awaiting_signin' })], REGISTERED).kind).toBe('signin')
  })

  it('is deployed for a finished launch the registry still holds, and failed for a failed or cancelled one', () => {
    expect(deployView([job()], REGISTERED).kind).toBe('deployed')
    expect(deployView([job({ status: 'failed' })], REGISTERED).kind).toBe('failed')
    expect(deployView([job({ status: 'cancelled' })], REGISTERED).kind).toBe('failed')
  })

  // The record alone never earns the present tense: a teardown unregisters the
  // machine but leaves the launch `done`, so `done` without the registry's word
  // is a past event, not a state.
  it('is finished, not deployed, when the registry does not confirm the machine', () => {
    expect(deployView([job()], EMPTY_REGISTRY)).toMatchObject({ kind: 'finished', liveness: 'gone' })
    expect(deployView([job()], undefined)).toMatchObject({ kind: 'finished', liveness: 'unknown' })
  })

  // The Fargate lane never reaches the registry-keyed states: its truth is ECS,
  // read by its own view, whatever the launch's status and whatever the
  // registry holds.
  it('hands a Fargate launch to the task view in every status', () => {
    const fargate = job({ provider_id: 'aws_fargate', instance_id: 'arn:aws:ecs:us-west-2:1:task/c/1' })
    expect(deployView([fargate], REGISTERED)).toMatchObject({ kind: 'task' })
    expect(deployView([fargate], undefined)).toMatchObject({ kind: 'task' })
    expect(deployView([job({ provider_id: 'aws_fargate', status: 'running', instance_id: '' })], REGISTERED)).toMatchObject({ kind: 'task' })
    expect(deployView([job({ provider_id: 'aws_fargate', status: 'failed', instance_id: '' })], REGISTERED)).toMatchObject({ kind: 'task' })
    // A lane this panel does not know stays on the registry-keyed states.
    expect(deployView([job({ provider_id: 'some_future_lane' })], undefined)).toMatchObject({ kind: 'finished' })
  })

  it('describes the NEWEST launch even when an older one finished', () => {
    const done = job({ id: 'done', created_at: 100 })
    const failed = job({ id: 'failed', status: 'failed', created_at: 200 })
    const view = deployView([done, failed], REGISTERED)
    expect(view.kind).toBe('failed')
    expect(view.kind === 'failed' && view.job.id).toBe('failed')
  })
})

describe('deployLiveness', () => {
  it('is live only when the registry holds the finished EC2 machine', () => {
    expect(deployLiveness(job(), REGISTERED)).toBe('live')
    expect(deployLiveness(job(), EMPTY_REGISTRY)).toBe('gone')
    expect(deployLiveness(job(), [{ ssm_target: 'i-other', ssh_host: '' }])).toBe('gone')
  })

  it('accepts a legacy row that registered the machine as its host', () => {
    expect(deployLiveness(job(), [{ ssm_target: '', ssh_host: 'i-0abc123def4567890' }])).toBe('live')
  })

  it('is unknown, never gone, when the registry could not be read', () => {
    expect(deployLiveness(job(), undefined)).toBe('unknown')
  })

  it('is unknown for every launch the registry cannot speak to', () => {
    // The Fargate lane registers nothing by design; an EC2 launch with no machine
    // recorded has nothing to look up; a launch that is not done has no machine
    // to be live.
    expect(deployLiveness(job({ provider_id: 'aws_fargate', instance_id: 'arn:aws:ecs:x' }), REGISTERED)).toBe('unknown')
    expect(deployLiveness(job({ instance_id: undefined }), REGISTERED)).toBe('unknown')
    expect(deployLiveness(job({ status: 'failed' }), REGISTERED)).toBe('unknown')
  })
})

describe('awsConsoleUrl', () => {
  it('links the console home of a region that parses as one', () => {
    expect(awsConsoleUrl('us-east-1')).toBe('https://us-east-1.console.aws.amazon.com/console/home?region=us-east-1')
    expect(awsConsoleUrl('ap-southeast-2')).toBe('https://ap-southeast-2.console.aws.amazon.com/console/home?region=ap-southeast-2')
    expect(awsConsoleUrl('us-gov-west-1')).toBe('https://us-gov-west-1.console.aws.amazon.com/console/home?region=us-gov-west-1')
  })

  // The value comes off a stored record and becomes a hostname, so anything
  // that is not a region id gets no link at all.
  it('links nothing for a missing region or one that does not parse', () => {
    for (const bad of [undefined, '', 'us-east', 'US-EAST-1', 'evil.example.com', 'us-east-1/x', 'us-east-1?x=1']) {
      expect(awsConsoleUrl(bad), String(bad)).toBeUndefined()
    }
  })
})

describe('deployProgress', () => {
  it('counts finished steps plus the one under way, over the recorded total', () => {
    expect(deployProgress({ steps: [step('done'), step('active'), step('pending')] }))
      .toEqual({ current: 2, total: 3 })
  })

  it('falls back to the four steps every launch has when none were recorded', () => {
    expect(deployProgress({ steps: [] })).toEqual({ current: 1, total: 4 })
  })

  it('never reports a step past the total', () => {
    expect(deployProgress({ steps: [step('done'), step('done')] })).toEqual({ current: 2, total: 2 })
  })
})

describe('deployStepLabel', () => {
  it('names the active step, in the record\'s own words', () => {
    const j = job({ steps: [{ key: 'a', label: 'Check your AWS setup', state: 'done' }, { key: 'b', label: 'Create the instance', state: 'active' }, { key: 'c', label: 'Connect', state: 'pending' }] })
    expect(deployStepLabel(j)).toBe('Create the instance')
  })

  // The active marker wins over position: a skipped step is not done either,
  // but it is not what is under way.
  it('prefers the step marked active over an earlier skipped one', () => {
    const j = job({ steps: [{ key: 'a', label: 'Check your AWS setup', state: 'skipped' }, { key: 'b', label: 'Create the instance', state: 'active' }] })
    expect(deployStepLabel(j)).toBe('Create the instance')
  })

  it('falls back to the first step not yet done when none is marked active', () => {
    const j = job({ steps: [{ key: 'a', label: 'Check your AWS setup', state: 'done' }, { key: 'b', label: 'Create the instance', state: 'pending' }] })
    expect(deployStepLabel(j)).toBe('Create the instance')
  })

  it('is empty for a record with no steps, so the counter stands alone', () => {
    expect(deployStepLabel(job({ steps: [] }))).toBe('')
  })
})

describe('earlierReachableLaunch', () => {
  it('names the newest older launch whose machine the registry still holds when the newest is not live', () => {
    const live = job({ id: 'live', created_at: 1_000, region: 'eu-west-1', instance_id: 'i-live' })
    const older = job({ id: 'older', created_at: 500, instance_id: 'i-older' })
    const retry = job({ id: 'retry', created_at: 2_000, status: 'failed', instance_id: undefined })
    const registry = [{ ssm_target: 'i-live', ssh_host: '' }, { ssm_target: 'i-older', ssh_host: '' }]
    expect(earlierReachableLaunch([older, retry, live], registry)?.id).toBe('live')
  })

  // A re-deploy that finished tears nothing down either, so a second registered
  // machine is named behind a live headline too: that is the headline a reader
  // has least reason to look past.
  it('names another registered machine even when the newest launch is itself live', () => {
    const old = job({ id: 'old', created_at: 500, instance_id: 'i-old', region: 'eu-west-1' })
    const live = job({ id: 'new', created_at: 900, instance_id: 'i-new' })
    const both = [{ ssm_target: 'i-old', ssh_host: '' }, { ssm_target: 'i-new', ssh_host: '' }]
    expect(earlierReachableLaunch([old, live], both)?.id).toBe('old')
    // Only the newest machine registered: the older one is gone, nothing to name.
    expect(earlierReachableLaunch([old, live], [{ ssm_target: 'i-new', ssh_host: '' }])).toBeUndefined()
  })

  it('does not name an older record of the newest launch\'s own machine', () => {
    // Two records, one instance id: one machine, not another.
    expect(earlierReachableLaunch([job({ id: 'old', created_at: 500 }), job({ id: 'new', created_at: 900 })], REGISTERED)).toBeUndefined()
  })

  // This line's whole claim is that a machine runs NOW, so a `done` record the
  // registry does not hold (torn down) or cannot speak to (unreadable) is not
  // named: the record alone would name a deleted machine as billing forever.
  it('is undefined when no earlier launch is confirmed live', () => {
    const done = job({ id: 'done', created_at: 500 })
    const retry = job({ id: 'retry', created_at: 900, status: 'failed', instance_id: undefined })
    expect(earlierReachableLaunch([done, retry], EMPTY_REGISTRY)).toBeUndefined()
    expect(earlierReachableLaunch([done, retry], undefined)).toBeUndefined()
    const failedOld = job({ id: 'old', created_at: 500, status: 'failed' })
    expect(earlierReachableLaunch([failedOld, retry], REGISTERED)).toBeUndefined()
    expect(earlierReachableLaunch([], REGISTERED)).toBeUndefined()
  })

  it('does not name a finished launch behind a newer finished one the registry cannot confirm', () => {
    // Newest is a Fargate launch (unknown liveness); the older EC2 one is live.
    const ec2 = job({ id: 'ec2', created_at: 500 })
    const fargate = job({ id: 'fargate', created_at: 900, provider_id: 'aws_fargate', instance_id: 'arn:aws:ecs:x' })
    expect(earlierReachableLaunch([ec2, fargate], REGISTERED)?.id).toBe('ec2')
  })
})

describe('deployAge', () => {
  const NOW = 1_700_000_000_000

  it('renders the unknown glyph for a missing or future timestamp, never a garbage age', () => {
    expect(deployAge(0, NOW)).toBe('\u2013')
    expect(deployAge(Number.NaN, NOW)).toBe('\u2013')
    expect(deployAge(NOW / 1000 + 3600, NOW)).toBe('\u2013')
  })

  it('shows days and hours past a day, hours and minutes past an hour, minutes below', () => {
    const h = 3600
    expect(deployAge(NOW / 1000 - (2 * 24 * h + 3 * h), NOW)).toMatch(/2.*3/)
    expect(deployAge(NOW / 1000 - (5 * h + 10 * 60), NOW)).toMatch(/5.*10/)
    expect(deployAge(NOW / 1000 - 7 * 60, NOW)).toMatch(/7/)
  })

  it('drops a zero part rather than printing "2d 0h"', () => {
    expect(deployAge(NOW / 1000 - 2 * 24 * 3600, NOW)).not.toMatch(/0/)
  })
})

describe('the panel', () => {
  beforeEach(() => {
    cloudLaunches.mockReset()
    listInstances.mockReset()
    listInstances.mockResolvedValue({ instances: REGISTERED, active: true })
    navigateSpy.mockReset()
  })
  afterEach(() => { vi.useRealTimers() })

  it('reports a read failure as unknown, and never as "runs only here"', async () => {
    cloudLaunches.mockRejectedValue(new Error('boom'))
    renderWithProviders(<DeployMyCrewDialog open onClose={() => {}} members={MEMBERS} />)

    await waitFor(() => expect(screen.getByTestId('deploy-error')).toBeTruthy())
    // The load-bearing half: the not-deployed sentence must be ABSENT, because a
    // reader who sees it concludes their crew is not deployed.
    expect(screen.queryByTestId('deploy-state-none')).toBeNull()
    // Through the shared error surface, so the agent hand-off is on it.
    expect(within(screen.getByTestId('deploy-error')).getByRole('alert')).toBeTruthy()
    expect(screen.getByTestId('deploy-retry')).toBeTruthy()
  })

  it('offers the deploy action when nothing is launched, and leads into Settings', async () => {
    cloudLaunches.mockResolvedValue({ jobs: [] })
    const onClose = vi.fn()
    renderWithProviders(<DeployMyCrewDialog open onClose={onClose} members={MEMBERS} />)

    await waitFor(() => expect(screen.getByTestId('deploy-state-none')).toBeTruthy())
    expect(screen.queryByTestId('deploy-error')).toBeNull()
    expect(screen.queryByTestId('deploy-details')).toBeNull()
    // The button only opens Settings, and its label says so; the line under
    // it says the one thing the label cannot: nothing is created until the
    // steps there are confirmed.
    expect(screen.getByTestId('deploy-action-deploy').textContent).toContain('Settings')
    expect(screen.getByTestId('deploy-action-hint').textContent).toContain('Nothing is created')

    fireEvent.click(screen.getByTestId('deploy-action-deploy'))
    expect(navigateSpy).toHaveBeenCalledWith('/settings/instances')
    // Closed before the navigation, so the dialog is not what a reader comes
    // back to.
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('shows the step count while deploying and offers nothing to press', async () => {
    cloudLaunches.mockResolvedValue({
      jobs: [job({ status: 'running', steps: [step('done'), step('active'), step('pending'), step('pending')] })],
    })
    renderWithProviders(<DeployMyCrewDialog open onClose={() => {}} members={MEMBERS} />)

    await waitFor(() => expect(screen.getByTestId('deploy-state-deploying')).toBeTruthy())
    expect(screen.getByTestId('deploy-progress').textContent).toContain('2')
    expect(screen.getByTestId('deploy-progress').textContent).toContain('4')
    // No duration estimate: nothing in the repo measures one, and only numbers
    // the record attests are shown.
    expect(screen.getByTestId('deploy-state-deploying').textContent).not.toMatch(/minute/i)
    expect(screen.queryByTestId('deploy-action-deploy')).toBeNull()
    expect(screen.queryByTestId('deploy-action-signin')).toBeNull()
    expect(screen.queryByTestId('deploy-address')).toBeNull()
    // The one door out: a muted link into Settings, where the launch row and
    // its Cancel live; it names the navigation and the destination.
    expect(screen.getByTestId('deploy-action-manage').textContent).toBe('Open Remote Crew in Settings')
    // The deploy runs in the gateway, so Close does not cancel it, and the
    // dialog says so rather than leaving the reader to babysit the window.
    expect(screen.getByTestId('deploy-close-hint').textContent).toContain('window')
    // The step under way is named beside the counter, in the gateway's words.
    expect(screen.getByTestId('deploy-step-label').textContent).toContain('active')
  })

  it('re-reads the launches while one is moving, and not once it has settled', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    cloudLaunches.mockResolvedValue({ jobs: [job({ status: 'running' })] })
    renderWithProviders(<DeployMyCrewDialog open onClose={() => {}} members={MEMBERS} />)
    await waitFor(() => expect(screen.getByTestId('deploy-state-deploying')).toBeTruthy())
    expect(cloudLaunches).toHaveBeenCalledTimes(1)

    await vi.advanceTimersByTimeAsync(4_100)
    expect(cloudLaunches).toHaveBeenCalledTimes(2)

    // The second read says it finished: no third read follows.
    cloudLaunches.mockResolvedValue({ jobs: [job()] })
    await vi.advanceTimersByTimeAsync(4_100)
    await waitFor(() => expect(screen.getByTestId('deploy-state-deployed')).toBeTruthy())
    const settled = cloudLaunches.mock.calls.length
    await vi.advanceTimersByTimeAsync(8_500)
    expect(cloudLaunches).toHaveBeenCalledTimes(settled)
  })

  it('sends the reader to finish the sign-in when the launch is waiting on them', async () => {
    cloudLaunches.mockResolvedValue({ jobs: [job({ status: 'awaiting_signin' })] })
    const onClose = vi.fn()
    renderWithProviders(<DeployMyCrewDialog open onClose={onClose} members={MEMBERS} />)

    await waitFor(() => expect(screen.getByTestId('deploy-state-signin')).toBeTruthy())
    // Names the navigation and the destination, like the deploy button.
    expect(screen.getByTestId('deploy-action-signin').textContent).toContain('Settings')
    // The same close reassurance as the deploying state.
    expect(screen.getByTestId('deploy-close-hint').textContent).toContain('window')
    fireEvent.click(screen.getByTestId('deploy-action-signin'))
    expect(navigateSpy).toHaveBeenCalledWith('/settings/instances')
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('shows the two honest numbers and the copyable address once deployed', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    // jsdom exposes `navigator.clipboard` as a getter, so it is redefined rather
    // than assigned, and put back afterwards.
    const realClipboard = Object.getOwnPropertyDescriptor(navigator, 'clipboard')
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    try {
      cloudLaunches.mockResolvedValue({ jobs: [job()] })
      renderWithProviders(<DeployMyCrewDialog open onClose={() => {}} members={MEMBERS} />)

      await waitFor(() => expect(screen.getByTestId('deploy-state-deployed')).toBeTruthy())
      expect(screen.getByTestId('deploy-stat-region').textContent).toBe('us-west-2')
      // The fixture's launch is years old, so the age is a real number, not the
      // unknown glyph.
      expect(screen.getByTestId('deploy-stat-since').textContent).not.toBe('\u2013')
      expect(screen.getByTestId('deploy-address-value').textContent).toBe('i-0abc123def4567890')

      fireEvent.click(screen.getByTestId('deploy-address-copy'))
      expect(writeText).toHaveBeenCalledWith('i-0abc123def4567890')
      await waitFor(() => expect(screen.getByTestId('deploy-address-copy').textContent).toContain('Copied'))
      // A copy that landed shows no failure notice.
      expect(screen.queryByTestId('deploy-address-copy-error')).toBeNull()

      // The technical line survives, collapsed, with the raw identifiers.
      const details = screen.getByTestId('deploy-details')
      expect((details as HTMLDetailsElement).open).toBe(false)
      expect(within(details).getByTestId('deploy-launch').textContent).toContain('aws_ec2')
      expect(within(details).getByTestId('deploy-launch').textContent).toContain('crew-alpha')
    } finally {
      if (realClipboard) Object.defineProperty(navigator, 'clipboard', realClipboard)
    }
  })

  it('says so when the copy fails, and never paints Copied over an unchanged clipboard', async () => {
    // Both layers refuse: the async API rejects (a denied permission) and there
    // is no execCommand fallback, which is jsdom's shape and a plain-HTTP remote
    // dashboard's everyday one.
    const writeText = vi.fn().mockRejectedValue(new Error('denied'))
    const realClipboard = Object.getOwnPropertyDescriptor(navigator, 'clipboard')
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    try {
      cloudLaunches.mockResolvedValue({ jobs: [job()] })
      renderWithProviders(<DeployMyCrewDialog open onClose={() => {}} members={MEMBERS} />)
      await waitFor(() => expect(screen.getByTestId('deploy-state-deployed')).toBeTruthy())

      fireEvent.click(screen.getByTestId('deploy-address-copy'))
      const notice = await screen.findByTestId('deploy-address-copy-error')
      expect(notice.textContent).toContain('Copy failed. Select the text and copy it manually.')
      expect(screen.getByTestId('deploy-address-copy').textContent).not.toContain('Copied')
      // And the value is still there to select by hand.
      expect(screen.getByTestId('deploy-address-value').textContent).toBe('i-0abc123def4567890')
      // The hand-off is on: nothing here is unsaved, and the agent has a remedy
      // the message does not (read the id off the record, open the session).
      expect(within(notice).getByRole('button', { name: /ask the agent/i })).toBeInTheDocument()
    } finally {
      if (realClipboard) Object.defineProperty(navigator, 'clipboard', realClipboard)
    }
  })

  it('draws the region as the unknown glyph when the record has none', async () => {
    cloudLaunches.mockResolvedValue({ jobs: [job({ region: '' })] })
    renderWithProviders(<DeployMyCrewDialog open onClose={() => {}} members={MEMBERS} />)
    await waitFor(() => expect(screen.getByTestId('deploy-state-deployed')).toBeTruthy())
    expect(screen.getByTestId('deploy-stat-region').textContent).toBe('\u2013')
  })

  it('offers no address, and no present-tense state, for a finished launch that recorded no machine', async () => {
    cloudLaunches.mockResolvedValue({ jobs: [job({ instance_id: undefined })] })
    renderWithProviders(<DeployMyCrewDialog open onClose={() => {}} members={MEMBERS} />)
    await waitFor(() => expect(screen.getByTestId('deploy-state-finished')).toBeTruthy())
    expect(screen.queryByTestId('deploy-state-deployed')).toBeNull()
    expect(screen.queryByTestId('deploy-address')).toBeNull()
    expect(screen.getByTestId('deploy-check-console')).toBeInTheDocument()
  })

  it('hands a finished Fargate launch to its own view, which reads the task from ECS', async () => {
    const arn = 'arn:aws:ecs:us-west-2:123456789012:task/crew/0f1e2d3c'
    cloudLaunches.mockResolvedValue({ jobs: [job({ id: 'jf', provider_id: 'aws_fargate', instance_id: arn })] })
    cloudLaunchTask.mockResolvedValue({
      job_id: 'jf', task_arn: arn, read_at: 1_700_000_000,
      task: { task_arn: arn, cluster: 'crew', task_id: '0f1e2d3c', last_status: 'RUNNING', desired_status: 'RUNNING', started_at: 1_699_999_000, stopped_at: null, stopped_reason: '' },
    })
    renderWithProviders(<DeployMyCrewDialog open onClose={() => {}} members={MEMBERS} />)
    const state = await screen.findByTestId('deploy-task-state-running')
    // None of the registry-keyed EC2 states render for this lane, in either
    // direction: not "deployed" (the registry never held the task) and not
    // "finished" (that sentence belongs to the lane the registry speaks for).
    expect(screen.queryByTestId('deploy-state-deployed')).toBeNull()
    expect(screen.queryByTestId('deploy-state-finished')).toBeNull()
    expect(screen.queryByTestId('deploy-address')).toBeNull()
    expect(state.textContent).toContain('running as a container task')
    expect(cloudLaunchTask).toHaveBeenCalledWith('jf')
    // The ARN is still there for the owner who needs it, in the Details line.
    expect(within(screen.getByTestId('deploy-details')).getByTestId('deploy-launch').textContent).toContain(arn)
  })

  // The task view's truth is ECS, so the registry read must neither hide it
  // (a failed read is an EC2 and earlier-launch concern) nor delay it.
  it('shows the task view beside a failed registry read instead of behind it', async () => {
    const arn = 'arn:aws:ecs:us-west-2:123456789012:task/crew/0f1e2d3c'
    cloudLaunches.mockResolvedValue({ jobs: [job({ id: 'jf', provider_id: 'aws_fargate', instance_id: arn })] })
    cloudLaunchTask.mockResolvedValue({ job_id: 'jf', task_arn: arn, read_at: 1_700_000_000, task: null })
    listInstances.mockRejectedValue(new ApiError(500, 'registry unavailable'))
    renderWithProviders(<DeployMyCrewDialog open onClose={() => {}} members={MEMBERS} />)
    await screen.findByTestId('deploy-task-state-missing')
    // The registry failure is still reported: it is what silences the
    // earlier-launch line, and Try again re-reads the registry.
    await waitFor(() => expect(screen.getByTestId('deploy-error')).toBeTruthy(), { timeout: 8000 })
    expect(screen.getByTestId('deploy-task-state-missing')).toBeInTheDocument()
  }, 15000)

  it('does not wait for the registry before showing the task view', async () => {
    const arn = 'arn:aws:ecs:us-west-2:123456789012:task/crew/0f1e2d3c'
    cloudLaunches.mockResolvedValue({ jobs: [job({ id: 'jf', provider_id: 'aws_fargate', instance_id: arn })] })
    cloudLaunchTask.mockResolvedValue({ job_id: 'jf', task_arn: arn, read_at: 1_700_000_000, task: null })
    listInstances.mockReturnValue(new Promise(() => {})) // never answers
    renderWithProviders(<DeployMyCrewDialog open onClose={() => {}} members={MEMBERS} />)
    await screen.findByTestId('deploy-task-state-missing')
    expect(screen.queryByTestId('deploy-loading')).toBeNull()
  })

  it('shows the recorded error through the error surface after a failed launch, and the deploy action', async () => {
    cloudLaunches.mockResolvedValue({
      jobs: [job({ status: 'failed', error: 'stack rolled back: the task role could not be assumed' })],
    })
    const onClose = vi.fn()
    renderWithProviders(<DeployMyCrewDialog open onClose={onClose} members={MEMBERS} />)

    await waitFor(() => expect(screen.getByTestId('deploy-state-failed')).toBeTruthy())
    const notice = screen.getByTestId('deploy-launch-error')
    expect(notice.getAttribute('role')).toBe('alert')
    expect(notice.textContent).toContain('the task role could not be assumed')
    // Never offered as reachable: the failed fixture still carries a machine.
    expect(screen.queryByTestId('deploy-address')).toBeNull()
    // The same label and where-it-leads line as the not-deployed state, for
    // the same button.
    expect(screen.getByTestId('deploy-action-deploy').textContent).toContain('Settings')
    expect(screen.getByTestId('deploy-action-hint').textContent).toContain('Nothing is created')

    fireEvent.click(screen.getByTestId('deploy-action-deploy'))
    expect(navigateSpy).toHaveBeenCalledWith('/settings/instances')
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('renders no error surface for a cancelled launch that recorded no error', async () => {
    cloudLaunches.mockResolvedValue({ jobs: [job({ status: 'cancelled', error: undefined })] })
    renderWithProviders(<DeployMyCrewDialog open onClose={() => {}} members={MEMBERS} />)
    await waitFor(() => expect(screen.getByTestId('deploy-state-failed')).toBeTruthy())
    expect(screen.queryByTestId('deploy-launch-error')).toBeNull()
  })

  it('leads with the crew\'s faces, and folds a large roster into a count', async () => {
    cloudLaunches.mockResolvedValue({ jobs: [] })
    const many = Array.from({ length: 9 }, (_, i) => ({ name: `Member ${i}` }))
    renderWithProviders(<DeployMyCrewDialog open onClose={() => {}} members={many} />)

    const faces = screen.getByTestId('deploy-faces')
    expect(faces.querySelectorAll('img').length).toBe(6)
    expect(screen.getByTestId('deploy-faces-more').textContent).toBe('+3')
  })

  it('draws no face row for an empty roster', async () => {
    cloudLaunches.mockResolvedValue({ jobs: [] })
    renderWithProviders(<DeployMyCrewDialog open onClose={() => {}} members={[]} />)
    await waitFor(() => expect(screen.getByTestId('deploy-state-none')).toBeTruthy())
    expect(screen.queryByTestId('deploy-faces')).toBeNull()
    expect(screen.queryByTestId('deploy-faces-more')).toBeNull()
  })

  it('names an earlier launch that still runs behind a failed retry', async () => {
    const live = job({ id: 'live', created_at: 1_000, region: 'eu-west-1' })
    const retry = job({ id: 'retry', created_at: 2_000, status: 'failed', instance_id: undefined, error: 'stack rolled back' })
    cloudLaunches.mockResolvedValue({ jobs: [live, retry] })
    renderWithProviders(<DeployMyCrewDialog open onClose={() => {}} members={MEMBERS} />)
    await waitFor(() => expect(screen.getByTestId('deploy-state-failed')).toBeTruthy())
    // The headline follows the retry; the line says what it would otherwise bury.
    const line = screen.getByTestId('deploy-earlier-live')
    expect(line.textContent).toContain('eu-west-1')
    expect(line.textContent).toContain('Details')
    // A warning about a paying machine ends with where to stop it.
    expect(line.textContent).toContain('Remote Crew in Settings')
  })

  it('does not name an earlier launch the registry no longer holds', async () => {
    const done = job({ id: 'done', created_at: 1_000, region: 'eu-west-1' })
    const retry = job({ id: 'retry', created_at: 2_000, status: 'failed', instance_id: undefined, error: 'stack rolled back' })
    cloudLaunches.mockResolvedValue({ jobs: [done, retry] })
    listInstances.mockResolvedValue({ instances: EMPTY_REGISTRY, active: true })
    renderWithProviders(<DeployMyCrewDialog open onClose={() => {}} members={MEMBERS} />)
    await waitFor(() => expect(screen.getByTestId('deploy-state-failed')).toBeTruthy())
    expect(screen.queryByTestId('deploy-earlier-live')).toBeNull()
  })

  it('says the machine is gone, in the past tense, once the registry no longer holds a finished launch', async () => {
    cloudLaunches.mockResolvedValue({ jobs: [job({ region: 'eu-west-1' })] })
    listInstances.mockResolvedValue({ instances: EMPTY_REGISTRY, active: true })
    const onClose = vi.fn()
    renderWithProviders(<DeployMyCrewDialog open onClose={onClose} members={MEMBERS} />)
    const state = await screen.findByTestId('deploy-state-finished')
    expect(screen.queryByTestId('deploy-state-deployed')).toBeNull()
    expect(state.textContent).toContain('A deploy finished')
    expect(state.textContent).toContain('eu-west-1')
    expect(state.textContent).toContain('no longer in Your crews')
    // Nothing to reach: no address, no stats, no console pointer for a machine
    // the registry says is gone.
    expect(screen.queryByTestId('deploy-address')).toBeNull()
    expect(screen.queryByTestId('deploy-stats')).toBeNull()
    expect(screen.queryByTestId('deploy-check-console')).toBeNull()
    // The one thing to do is the same as with no launch: the set-up flow.
    expect(screen.getByTestId('deploy-action-deploy').textContent).toContain('Settings')
    expect(screen.getByTestId('deploy-action-hint').textContent).toContain('Nothing is created')
    fireEvent.click(screen.getByTestId('deploy-action-deploy'))
    expect(navigateSpy).toHaveBeenCalledWith('/settings/instances')
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('says it cannot tell, never "gone", when the Instances feature is off', async () => {
    cloudLaunches.mockResolvedValue({ jobs: [job()] })
    listInstances.mockRejectedValue(new ApiError(403, 'Instances feature is disabled'))
    renderWithProviders(<DeployMyCrewDialog open onClose={() => {}} members={MEMBERS} />)
    const state = await screen.findByTestId('deploy-state-finished')
    expect(state.textContent).toContain('cannot tell whether it is still running')
    expect(state.textContent).not.toContain('no longer in Your crews')
    expect(screen.getByTestId('deploy-check-console').textContent).toContain('AWS console')
    // The sentence is the link, to that region's console, in a new tab.
    const link = screen.getByTestId('deploy-console-link')
    expect(link.getAttribute('href')).toBe('https://us-west-2.console.aws.amazon.com/console/home?region=us-west-2')
    expect(link.getAttribute('target')).toBe('_blank')
    expect(link.getAttribute('rel')).toContain('noreferrer')
    expect(screen.queryByTestId('deploy-stats')).toBeNull()
    // A 403 is not retried: the feature being off does not change on retry.
    expect(listInstances).toHaveBeenCalledTimes(1)
  })

  it('reports a failed registry read as an error with the hand-off and a retry, never as "cannot tell"', async () => {
    cloudLaunches.mockResolvedValue({ jobs: [job()] })
    listInstances.mockRejectedValue(new ApiError(500, 'registry unavailable'))
    const onClose = vi.fn()
    renderWithProviders(<DeployMyCrewDialog open onClose={onClose} members={MEMBERS} />)
    // The read retries twice with backoff before it settles as failed.
    await waitFor(() => expect(screen.getByTestId('deploy-error')).toBeTruthy(), { timeout: 8000 })
    // Neither the unknown sentence nor a state: a failed read is not a fact
    // about the machine.
    expect(screen.queryByTestId('deploy-state-finished')).toBeNull()
    expect(screen.queryByTestId('deploy-state-deployed')).toBeNull()
    expect(screen.queryByTestId('deploy-check-console')).toBeNull()
    // Through the shared error surface with the agent hand-off, like a failed
    // launch read.
    const notice = within(screen.getByTestId('deploy-error')).getByRole('alert')
    expect(within(notice).getByRole('button', { name: /ask the agent/i })).toBeInTheDocument()
    // Retry re-reads the registry, not the launch list that already answered.
    const launchReads = cloudLaunches.mock.calls.length
    listInstances.mockResolvedValue({ instances: REGISTERED, active: true })
    fireEvent.click(screen.getByTestId('deploy-retry'))
    await waitFor(() => expect(screen.getByTestId('deploy-state-deployed')).toBeTruthy())
    expect(cloudLaunches).toHaveBeenCalledTimes(launchReads)
  }, 15000)

  it('keeps the console words without a link when the record has no usable region', async () => {
    cloudLaunches.mockResolvedValue({ jobs: [job({ region: '' })] })
    listInstances.mockRejectedValue(new ApiError(403, 'Instances feature is disabled'))
    renderWithProviders(<DeployMyCrewDialog open onClose={() => {}} members={MEMBERS} />)
    await screen.findByTestId('deploy-state-finished')
    expect(screen.getByTestId('deploy-check-console').textContent).toContain('AWS console')
    expect(screen.queryByTestId('deploy-console-link')).toBeNull()
  })

  it('reads the registry only once a launch has finished', async () => {
    cloudLaunches.mockResolvedValue({ jobs: [job({ status: 'running' })] })
    renderWithProviders(<DeployMyCrewDialog open onClose={() => {}} members={MEMBERS} />)
    await waitFor(() => expect(screen.getByTestId('deploy-state-deploying')).toBeTruthy())
    expect(listInstances).not.toHaveBeenCalled()
  })

  it('draws no earlier-launch line when the only other registered machine is the newest launch\'s own', async () => {
    cloudLaunches.mockResolvedValue({ jobs: [job({ id: 'old', created_at: 500 }), job({ id: 'new', created_at: 900 })] })
    renderWithProviders(<DeployMyCrewDialog open onClose={() => {}} members={MEMBERS} />)
    await waitFor(() => expect(screen.getByTestId('deploy-state-deployed')).toBeTruthy())
    expect(screen.queryByTestId('deploy-earlier-live')).toBeNull()
  })

  it('names a second registered machine under the deployed headline', async () => {
    const old = job({ id: 'old', created_at: 500, instance_id: 'i-old', region: 'eu-west-1' })
    const live = job({ id: 'new', created_at: 900, instance_id: 'i-new', region: 'us-west-2' })
    cloudLaunches.mockResolvedValue({ jobs: [old, live] })
    listInstances.mockResolvedValue({ instances: [{ ssm_target: 'i-old', ssh_host: '' }, { ssm_target: 'i-new', ssh_host: '' }], active: true })
    renderWithProviders(<DeployMyCrewDialog open onClose={() => {}} members={MEMBERS} />)
    await waitFor(() => expect(screen.getByTestId('deploy-state-deployed')).toBeTruthy())
    // The headline and the target belong to the newest machine ...
    expect(screen.getByTestId('deploy-address-value').textContent).toBe('i-new')
    // ... and the line names the older one, still billing behind it.
    const line = screen.getByTestId('deploy-earlier-live')
    expect(line.textContent).toContain('eu-west-1')
    expect(line.textContent).toContain('Remote Crew in Settings')
  })

  it('makes no launch read at all while it is closed', async () => {
    cloudLaunches.mockResolvedValue({ jobs: [] })
    renderWithProviders(<DeployMyCrewDialog open={false} onClose={() => {}} members={MEMBERS} />)

    // Opening the page must not cost a request against the operator's account.
    await new Promise((r) => setTimeout(r, 20))
    expect(cloudLaunches).not.toHaveBeenCalled()
  })
})
