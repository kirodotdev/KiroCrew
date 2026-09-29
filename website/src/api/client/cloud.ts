/**
 * The cloud launcher: preflight, IAM policy, provisioners, durable launch
 * jobs with their sign-in and task reads, and stop/start/destroy of a crew on
 * the operator's own AWS account.
 */

import type { ClientTransport } from './transport'

/* ── Cloud provisioning (GET/POST /api/cloud/*) ──
 * Shapes mirror the backend launch-job model (cloud/launch_job.py). `size_key`
 * is the stable CLI id (light|balanced|power); `balanced` is the recommended
 * "Development" default. */

/** AWS preflight for the cloud launcher. Booleans are per-capability checks;
 *  `note`/`detail` are server-authored human text rendered verbatim. */
/** AWS coordinates a cloud lifecycle call needs beyond the stack tag.
 *
 *  `instanceId` is only meaningful for destroy, where the gateway uses it to
 *  drop the local Instances registration alongside the stack.
 */
export interface CloudCoords {
  profile?: string
  region?: string
  instanceId?: string
}

const cloudQuery = (c?: CloudCoords): string => {
  const q = new URLSearchParams()
  if (c?.profile) q.set('profile', c.profile)
  if (c?.region) q.set('region', c.region)
  if (c?.instanceId) q.set('instance_id', c.instanceId)
  const s = q.toString()
  return s ? `?${s}` : ''
}

export interface CloudPreflight {
  reachable: boolean
  account: string
  arn: string
  ec2_reachable: boolean
  cloudformation_reachable: boolean
  ssm_reachable: boolean
  note: string
  detail: string
  session_manager_plugin: boolean
  /** Copy-pasteable install command for the GATEWAY's platform, resolved
   *  server-side (the browser cannot know that host's OS). "" when the platform
   *  has no one-liner. */
  session_manager_plugin_command?: string
}

/** One remote-instance provisioner the gateway offers, from
 *  `GET /api/cloud/provisioners`.
 *
 *  `id` is what `POST /api/cloud/launch` names in `provider_id`; `kind` names the
 *  FRONTEND form that collects its inputs (see
 *  `components/remoteProvisionerRenderers.tsx`), so several rows may share one
 *  kind. `label` and `steps[].label` are server-authored and rendered verbatim,
 *  not translated. `posix_only` is informational: the server refuses a launch on
 *  a Windows gateway itself, with 400 `posix_host_required`. */
export interface RemoteProvisioner {
  id: string
  kind: string
  label: string
  posix_only: boolean
  steps: { key: string; label: string }[]
  /** What the operator must confirm before this lane may launch, resolved by the
   *  server, or '' for a lane with nothing to confirm. The client sends it back
   *  verbatim as `confirm_recipient`; the server refuses a launch whose value does
   *  not match the lane's own, so this is never something the client composes. */
  confirm_before_launch?: string
  /** The ONE crew this lane can deploy, or '' for a lane that deploys whichever crew
   *  it is given. A lane running a prebuilt, digest-pinned image carries one crew's
   *  bundle and cannot serve another; published here so the crew picker says so while
   *  the user is choosing rather than after a refused launch. */
  serves_mate?: string
}

export type LaunchJobStatus =
  | 'pending' | 'running' | 'awaiting_signin' | 'done' | 'failed' | 'cancelled'

export type LaunchStepState = 'pending' | 'active' | 'done' | 'failed' | 'skipped'

/** One step of a launch job (preflight/provision/signin/connect). `label` and
 *  `detail` are server-authored and rendered verbatim, not translated. */
export interface LaunchStep {
  key: string
  label: string
  state: LaunchStepState
  detail?: string
}

/** The device-code sign-in prompt surfaced while a job is `awaiting_signin`. */
export interface CloudLaunchSignin {
  url: string
  code: string
  ports?: number[]
}

/** The Kiro identity a managed crew signs in as. Empty fields = Builder ID.
 *  `region` is the IAM Identity Center region, NOT the EC2 region. Never a credential. */
export interface KiroLoginTarget {
  license: '' | 'free' | 'pro'
  start_url: string
  region: string
}

/** `GET /api/cloud/identity` — the launching machine's own kiro-cli sign-in and
 *  the launch target it suggests (Identity Center region left empty: whoami
 *  does not report it, the form asks). A suggestion the user can override.
 *  `discovery: 'unknown'` means whoami could not answer: both fields are null
 *  and the form must not present the Builder ID default as a read value. */
export interface CloudIdentity {
  identity: { account_type?: string; start_url?: string } | null
  suggested_target: KiroLoginTarget | null
  discovery?: 'read' | 'unknown'
}

export interface LaunchJob {
  id: string
  /** Which provisioner ran this job. A job persisted before the provisioner seam
   *  existed loads as "aws_ec2", so this is always present. */
  provider_id: string
  /** The identity this launch signs the crew in as; absent on jobs from an
   *  older gateway, which means Builder ID. */
  login_target?: KiroLoginTarget
  profile: string
  region: string
  size_key: string
  tag: string
  status: LaunchJobStatus
  steps: LaunchStep[]
  instance_id?: string
  signin?: CloudLaunchSignin | null
  signin_detected?: boolean
  error?: string
  created_at: number
  updated_at: number
  /** Which crew this launch was for, as the picker chose it; absent on a job from
   *  before the field, and on any launch that named no crew. It is what lets a card
   *  for a launch still in flight carry the crew's own name and face after a reload. */
  mate_name?: string
}

/** One ECS task as `GET /api/cloud/launch/{id}/task` reports it: the Fargate
 *  lane's own answer to whether the crew is still up. `cluster` and `task_id`
 *  are split out of the ARN server-side. `stopped_*` are empty until ECS
 *  reports a stop; `started_at`/`stopped_at` are epoch seconds. */
export interface LaunchTaskSighting {
  task_arn: string
  cluster: string
  task_id: string
  /** ECS's lifecycle word, verbatim (PROVISIONING, PENDING, RUNNING, STOPPED, …). */
  last_status: string
  /** Where ECS is taking the task: RUNNING, or STOPPED once a stop is accepted. */
  desired_status: string
  started_at: number | null
  stopped_at: number | null
  /** ECS's own sentence for why the task stopped; empty while it runs. */
  stopped_reason: string
}

/** The single-task read for one launch. `task` is null when ECS no longer
 *  lists the ARN (ECS drops a stopped task from its list after about an hour).
 *  `read_at` is when THIS read happened, epoch seconds: a status is a reading
 *  at an instant, and the panel shows it as one. */
export interface LaunchTaskReport {
  job_id: string
  task_arn: string
  read_at: number
  task: LaunchTaskSighting | null
}

export function createCloudEndpoints({ get, post, del, j }: ClientTransport) {
  const launcher = {
    // Cloud provisioning (owner-only) — launch a cloud-hosted remote crew on the
    // user's OWN AWS account, then register it as an SSM instance on connect. The
    // launch is a DURABLE gateway job (see cloud/launch_job.py): it survives
    // dashboard navigation and restart, so the UI polls its state rather than
    // holding it in memory. `tag` (kc-xxxx) is the cloud lifecycle handle used by
    // stop/start/destroy; `instance_id` (i-...) is the EC2 id it registers under.
    cloudPreflight: (profile?: string, region?: string) => {
      const p = new URLSearchParams()
      if (profile) p.set('profile', profile)
      if (region) p.set('region', region)
      const s = p.toString()
      return get('/api/cloud/preflight' + (s ? '?' + s : '')).then(j) as Promise<CloudPreflight>
    },
    cloudIamPolicy: () => get('/api/cloud/iam-policy').then(j) as Promise<{ policy: string }>,
    // Which provisioners this gateway offers. Answers on every platform (a Windows
    // host still lists the POSIX-only built-in, and refuses the launch itself), so
    // the setup tab can pick a form before it knows whether a launch would be
    // allowed.
    cloudProvisioners: () =>
      get('/api/cloud/provisioners').then(j) as Promise<{ provisioners: RemoteProvisioner[] }>,
    cloudLaunches: () => get('/api/cloud/launch').then(j) as Promise<{ jobs: LaunchJob[] }>,
    /** The launching machine's own Kiro sign-in — the launch form's inherited default. */
    cloudIdentity: () => get('/api/cloud/identity').then(j) as Promise<CloudIdentity>,
    // `provider_id` is optional on the wire: the server defaults it to "aws_ec2"
    // and answers 400 `unknown_provisioner` for an id it does not offer.
    cloudLaunch: (body: { provider_id?: string; profile: string; region: string; size_key: string; subnet_id?: string; login_target?: KiroLoginTarget; confirm_recipient?: string; mate_name?: string }) =>
      post('/api/cloud/launch', body).then(j) as Promise<LaunchJob>,
    cloudLaunchStatus: (id: string) =>
      get('/api/cloud/launch/' + encodeURIComponent(id)).then(j) as Promise<LaunchJob>,
    // The ECS task a Fargate launch started, read from ECS now (one
    // describe-tasks for the ARN the job recorded). 400 `provisioner_cannot_describe`
    // for a lane with no such read (EC2, whose liveness is the registry), 400
    // `launch_task_not_recorded` when the launch never started a task, 502
    // `aws_call_failed` when the read did not complete. POSIX-only, like every
    // route here that runs the AWS CLI.
    cloudLaunchTask: (id: string) =>
      get('/api/cloud/launch/' + encodeURIComponent(id) + '/task').then(j) as Promise<LaunchTaskReport>,
    cloudLaunchCancel: (id: string) =>
      post('/api/cloud/launch/' + encodeURIComponent(id) + '/cancel').then(j) as Promise<LaunchJob>,
    // Fetches the device-code prompt while the job is awaiting sign-in; 409 when
    // there is no pending prompt (surfaced as ApiError(409) to the caller).
    cloudLaunchSignin: (id: string) =>
      post('/api/cloud/launch/' + encodeURIComponent(id) + '/signin').then(j) as Promise<{ signin: CloudLaunchSignin }>,
    // Starts the Kiro sign-in AGAIN on a crew whose launch finished unsigned: a
    // fresh device code, run with the `login_target` the job was created with, so
    // a company-SSO crew is not retried through the Builder ID prompt its
    // organization cannot approve. Answers the job, which becomes the polled one.
    // 409 while another launch or sign-in is already running on it, 400 when the
    // job never created a crew (there is nothing to sign in).
    cloudLaunchSigninRestart: (id: string) =>
      post('/api/cloud/launch/' + encodeURIComponent(id) + '/signin/restart').then(j) as Promise<LaunchJob>,
    // The gateway resolves the stack from the tag but needs the launch's AWS
    // coordinates: a crew created under a non-default profile/region is invisible
    // to the default ones, so omitting them makes stop/start/destroy fail. destroy
    // also needs instance_id to drop the local Instances registration, otherwise
    // the crew keeps appearing in the list after its box is gone.
    cloudStop: (tag: string, coords?: CloudCoords) =>
      post('/api/cloud/' + encodeURIComponent(tag) + '/stop' + cloudQuery(coords)).then(j) as Promise<{ ok?: boolean }>,
    cloudStart: (tag: string, coords?: CloudCoords) =>
      post('/api/cloud/' + encodeURIComponent(tag) + '/start' + cloudQuery(coords)).then(j) as Promise<{ ok?: boolean }>,
    cloudDestroy: (tag: string, coords?: CloudCoords) =>
      del('/api/cloud/' + encodeURIComponent(tag) + cloudQuery(coords)).then(j) as Promise<{ ok?: boolean; unregistered?: boolean; source_removed?: boolean }>,
  }

  return { launcher }
}
