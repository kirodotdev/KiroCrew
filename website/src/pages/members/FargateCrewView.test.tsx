/**
 * Your crew as a container task: the Fargate lane's own view.
 *
 * The helpers are asserted directly (a phase folded wrong is invisible on
 * screen: "stopping" and "stopped" look alike until a reader acts on them),
 * then one render per state pins the claim that state makes and, as
 * carefully, the claims it must NOT make: nothing here says "running" without
 * a RUNNING that ECS returned, nothing says "stopped" without a STOPPED, and a
 * read that failed says it could not read, never a state.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { renderWithProviders } from '../../test/helpers'
import FargateCrewView, { ecsTaskConsoleUrl, taskPhase, taskPhaseIsTransitional } from './FargateCrewView'
import { ApiError, type LaunchJob, type LaunchTaskReport, type LaunchTaskSighting } from '../../api/client'

const cloudLaunchTask = vi.fn()
vi.mock('../../api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/client')>()
  return {
    ...actual,
    api: {
      cloudLaunchTask: (...a: unknown[]) => cloudLaunchTask(...a),
    },
  }
})

const ARN = 'arn:aws:ecs:us-west-2:123456789012:task/crew-cluster/0f1e2d3c4b5a69788796a5b4c3d2e1f0'

function job(over: Partial<LaunchJob> = {}): LaunchJob {
  return {
    id: 'jf',
    provider_id: 'aws_fargate',
    profile: 'dev',
    region: 'us-west-2',
    size_key: 'small',
    tag: 'kc-7a21cd',
    status: 'done',
    steps: [],
    instance_id: ARN,
    created_at: Math.floor(Date.now() / 1000) - 3 * 3600,
    updated_at: Math.floor(Date.now() / 1000) - 3 * 3600,
    ...over,
  }
}

function sighting(over: Partial<LaunchTaskSighting> = {}): LaunchTaskSighting {
  return {
    task_arn: ARN,
    cluster: 'crew-cluster',
    task_id: '0f1e2d3c4b5a69788796a5b4c3d2e1f0',
    last_status: 'RUNNING',
    desired_status: 'RUNNING',
    started_at: Math.floor(Date.now() / 1000) - 3 * 3600,
    stopped_at: null,
    stopped_reason: '',
    ...over,
  }
}

function report(task: LaunchTaskSighting | null): LaunchTaskReport {
  return { job_id: 'jf', task_arn: ARN, read_at: 1_758_470_400, task }
}

describe('taskPhase', () => {
  it('folds ECS lifecycle words to the panel phases, case-insensitively', () => {
    expect(taskPhase(report(sighting({ last_status: 'RUNNING' })))).toBe('running')
    expect(taskPhase(report(sighting({ last_status: 'running' })))).toBe('running')
    for (const s of ['PROVISIONING', 'PENDING', 'ACTIVATING']) {
      expect(taskPhase(report(sighting({ last_status: s })))).toBe('starting')
    }
    for (const s of ['DEACTIVATING', 'STOPPING', 'DEPROVISIONING']) {
      expect(taskPhase(report(sighting({ last_status: s })))).toBe('stopping')
    }
    expect(taskPhase(report(sighting({ last_status: 'STOPPED' })))).toBe('stopped')
    expect(taskPhase(report(sighting({ last_status: 'DELETED' })))).toBe('stopped')
  })

  it('reads an accepted stop from desiredStatus before lastStatus catches up', () => {
    expect(taskPhase(report(sighting({ last_status: 'RUNNING', desired_status: 'STOPPED' })))).toBe('stopping')
    expect(taskPhase(report(sighting({ last_status: 'PENDING', desired_status: 'stopped' })))).toBe('stopping')
    // Once lastStatus agrees, the task is stopped, not still stopping.
    expect(taskPhase(report(sighting({ last_status: 'STOPPED', desired_status: 'STOPPED' })))).toBe('stopped')
    // A desire that is not a stop changes nothing.
    expect(taskPhase(report(sighting({ last_status: 'RUNNING', desired_status: 'RUNNING' })))).toBe('running')
  })

  it('is missing when ECS returned no task, and other for a word it does not know', () => {
    expect(taskPhase(report(null))).toBe('missing')
    expect(taskPhase(report(sighting({ last_status: 'HIBERNATING' })))).toBe('other')
    expect(taskPhase(report(sighting({ last_status: '' })))).toBe('other')
  })

  it('polls only the moving phases', () => {
    expect(taskPhaseIsTransitional('starting')).toBe(true)
    expect(taskPhaseIsTransitional('stopping')).toBe(true)
    for (const p of ['running', 'stopped', 'missing', 'other'] as const) {
      expect(taskPhaseIsTransitional(p)).toBe(false)
    }
  })
})

describe('ecsTaskConsoleUrl', () => {
  it('links the exact task page in the region, encoding the path segments', () => {
    expect(ecsTaskConsoleUrl('us-west-2', 'crew-cluster', '0f1e2d3c')).toBe(
      'https://us-west-2.console.aws.amazon.com/ecs/v2/clusters/crew-cluster/tasks/0f1e2d3c?region=us-west-2',
    )
    expect(ecsTaskConsoleUrl('eu-west-1', 'a/b', 'x y')).toBe(
      'https://eu-west-1.console.aws.amazon.com/ecs/v2/clusters/a%2Fb/tasks/x%20y?region=eu-west-1',
    )
  })

  it('links nothing when the region is not a region id or a segment is missing', () => {
    // The region becomes a hostname, so only a value that parses as one is used.
    expect(ecsTaskConsoleUrl('evil.example.com', 'c', 't')).toBeUndefined()
    expect(ecsTaskConsoleUrl('', 'c', 't')).toBeUndefined()
    expect(ecsTaskConsoleUrl(undefined, 'c', 't')).toBeUndefined()
    expect(ecsTaskConsoleUrl('us-west-2', '', 't')).toBeUndefined()
    expect(ecsTaskConsoleUrl('us-west-2', 'c', '')).toBeUndefined()
  })
})

describe('FargateCrewView', () => {
  const goToSettings = vi.fn()
  const onClose = vi.fn()
  const render = (j: LaunchJob) =>
    renderWithProviders(<FargateCrewView job={j} open onClose={onClose} goToSettings={goToSettings} />)

  beforeEach(() => {
    cloudLaunchTask.mockReset()
    goToSettings.mockReset()
    onClose.mockReset()
  })

  it('describes a moving launch from the record and reads no task', async () => {
    const steps = [
      { key: 'preflight', label: 'Check your AWS setup', state: 'done' as const },
      { key: 'provision', label: 'Start the task', state: 'active' as const },
      { key: 'signin', label: 'Sign in to Kiro', state: 'pending' as const },
      { key: 'connect', label: 'Connect', state: 'pending' as const },
    ]
    render(job({ status: 'running', instance_id: '', steps }))
    expect(screen.getByTestId('deploy-task-state-starting').textContent).toContain('starting as a container task')
    // How far, and what the step is, in the gateway's wording.
    expect(screen.getByTestId('deploy-progress').textContent).toContain('Step 2 of 4')
    expect(screen.getByTestId('deploy-step-label').textContent).toContain('Start the task')
    // The way to the launch's Cancel, as a door: the label names the place.
    fireEvent.click(screen.getByTestId('deploy-action-manage'))
    expect(goToSettings).toHaveBeenCalledTimes(1)
    expect(screen.getByTestId('deploy-action-manage').textContent).toBe('Open Remote Crew in Settings')
    expect(cloudLaunchTask).not.toHaveBeenCalled()
  })

  it('shows the recorded error and the set-up door after a failed launch, and reads no task', () => {
    render(job({ status: 'failed', instance_id: '', error: 'RunTask refused: the cluster has no capacity provider' }))
    expect(screen.getByTestId('deploy-task-state-failed').textContent).toContain('did not start a container task')
    expect(screen.getByTestId('deploy-launch-error').textContent).toContain('RunTask refused')
    fireEvent.click(screen.getByTestId('deploy-action-deploy'))
    expect(goToSettings).toHaveBeenCalledTimes(1)
    expect(cloudLaunchTask).not.toHaveBeenCalled()
  })

  it('says running only on a RUNNING from ECS, with the read instant, the task identity and the console link', async () => {
    cloudLaunchTask.mockResolvedValue(report(sighting()))
    render(job())
    const state = await screen.findByTestId('deploy-task-state-running')
    expect(state.textContent).toContain('running as a container task in us-west-2')
    expect(cloudLaunchTask).toHaveBeenCalledWith('jf')
    // Both stat cards, off the launch record.
    expect(screen.getByTestId('deploy-stat-since').textContent).toBe('3h')
    expect(screen.getByTestId('deploy-stat-region').textContent).toBe('us-west-2')
    // The task as ECS names it, and the full ARN one Copy away.
    expect(screen.getByTestId('deploy-task-cluster').textContent).toBe('crew-cluster')
    expect(screen.getByTestId('deploy-task-id').textContent).toBe('0f1e2d3c4b5a69788796a5b4c3d2e1f0')
    expect(screen.getByTestId('deploy-task-arn').textContent).toBe(ARN)
    // A status is a reading at an instant: the instant is on screen.
    expect(screen.getByTestId('deploy-task-read-at').textContent).toMatch(/As of \d/)
    // The console link goes to this task, in a new tab, and says it leaves.
    const link = screen.getByTestId('deploy-task-console-link')
    expect(link.getAttribute('href')).toBe(
      'https://us-west-2.console.aws.amazon.com/ecs/v2/clusters/crew-cluster/tasks/0f1e2d3c4b5a69788796a5b4c3d2e1f0?region=us-west-2',
    )
    expect(link.getAttribute('target')).toBe('_blank')
    expect(link.getAttribute('rel')).toContain('noreferrer')
    // No EC2 vocabulary: no Session Manager target, no set-up button on a running task.
    expect(screen.queryByTestId('deploy-address')).toBeNull()
    expect(screen.queryByTestId('deploy-action-deploy')).toBeNull()
  })

  it('re-reads on Check again and nothing else', async () => {
    cloudLaunchTask.mockResolvedValue(report(sighting()))
    render(job())
    await screen.findByTestId('deploy-task-state-running')
    expect(cloudLaunchTask).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByTestId('deploy-task-check'))
    await waitFor(() => expect(cloudLaunchTask).toHaveBeenCalledTimes(2))
    expect(goToSettings).not.toHaveBeenCalled()
  })

  it('says stopped only on a STOPPED from ECS, with ECS\'s own reason and when, and offers the set-up door', async () => {
    const stoppedAt = Math.floor(Date.now() / 1000) - 25 * 60
    cloudLaunchTask.mockResolvedValue(
      report(sighting({ last_status: 'STOPPED', desired_status: 'STOPPED', stopped_at: stoppedAt, stopped_reason: 'Essential container in task exited' })),
    )
    render(job())
    const state = await screen.findByTestId('deploy-task-state-stopped')
    expect(state.textContent).toContain('has stopped')
    expect(screen.getByTestId('deploy-task-stopped').textContent).toContain('It stopped 25m ago.')
    expect(screen.getByTestId('deploy-task-stopped-reason').textContent).toBe('Essential container in task exited')
    // No "since deploy" beside "has stopped": that would read as an uptime.
    expect(screen.queryByTestId('deploy-stats')).toBeNull()
    // The identity stays, for the owner who wants to look it up.
    expect(screen.getByTestId('deploy-task-id').textContent).toBe('0f1e2d3c4b5a69788796a5b4c3d2e1f0')
    // The instant and the re-read both stay: a stopped task changes once more
    // (ECS drops it from the list), and re-checking should not need a reopen.
    expect(screen.getByTestId('deploy-task-read-at')).toBeInTheDocument()
    expect(screen.getByTestId('deploy-task-check')).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('deploy-action-deploy'))
    expect(goToSettings).toHaveBeenCalledTimes(1)
  })

  it('says ECS no longer lists the task when the read returned none, and nothing about running or billing', async () => {
    cloudLaunchTask.mockResolvedValue(report(null))
    render(job())
    const state = await screen.findByTestId('deploy-task-state-missing')
    expect(state.textContent).toContain('ECS no longer lists')
    expect(state.textContent).not.toMatch(/running|stopped|billing/i)
    expect(screen.queryByTestId('deploy-task-row')).toBeNull()
    expect(screen.queryByTestId('deploy-task-console-link')).toBeNull()
    expect(screen.getByTestId('deploy-task-read-at')).toBeInTheDocument()
    expect(screen.getByTestId('deploy-task-check')).toBeInTheDocument()
    expect(screen.getByTestId('deploy-action-deploy')).toBeInTheDocument()
  })

  it('shows a lifecycle word it does not know verbatim rather than rounding it', async () => {
    cloudLaunchTask.mockResolvedValue(report(sighting({ last_status: 'HIBERNATING' })))
    render(job())
    const state = await screen.findByTestId('deploy-task-state-other')
    expect(state.textContent).toContain('HIBERNATING')
    expect(state.textContent).not.toMatch(/is running|has stopped/)
  })

  it('reports a failed read as could-not-read with the hand-off and Try again, never as a state', async () => {
    cloudLaunchTask.mockRejectedValue(new ApiError(502, 'aws ecs describe-tasks failed: AccessDeniedException'))
    render(job())
    // One retry, then the error surface.
    const state = await screen.findByTestId('deploy-task-state-unknown', {}, { timeout: 8000 })
    expect(state.textContent).toContain('could not read')
    expect(screen.queryByTestId('deploy-task-state-running')).toBeNull()
    expect(screen.queryByTestId('deploy-task-state-stopped')).toBeNull()
    expect(screen.queryByTestId('deploy-task-state-missing')).toBeNull()
    const notice = screen.getByTestId('deploy-task-error')
    expect(notice.textContent).toContain('AccessDeniedException')
    expect(within(notice).getByRole('button', { name: /ask the agent/i })).toBeInTheDocument()
    // Try again re-reads the task.
    const reads = cloudLaunchTask.mock.calls.length
    cloudLaunchTask.mockResolvedValue(report(sighting()))
    fireEvent.click(screen.getByTestId('deploy-task-retry'))
    await screen.findByTestId('deploy-task-state-running')
    expect(cloudLaunchTask.mock.calls.length).toBeGreaterThan(reads)
  }, 15000)

  it('reads nothing while the panel is closed', () => {
    renderWithProviders(<FargateCrewView job={job()} open={false} onClose={onClose} goToSettings={goToSettings} />)
    expect(cloudLaunchTask).not.toHaveBeenCalled()
  })
})
