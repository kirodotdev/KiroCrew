/**
 * Your crew in the cloud: the Members page's answer to "is my crew deployed,
 * and what do I do next?"
 *
 * PERSONA FIRST. The crew's faces lead, then one plain sentence for the state
 * the crew is in, then the one thing a reader can do about it. The technical
 * identifiers an owner needs to debug a deploy (stack tag, provisioner id,
 * profile, region, instance id, raw status) are kept, but demoted to one
 * collapsed line at the bottom.
 *
 * SCOPE IS CREW-WIDE, NOT PER-MEMBER. A launch ships the whole local checkout
 * to one machine and names one CloudFormation stack (`tag`), so there is no
 * per-member deployment to show. That is why the trigger sits in the page
 * header beside "add a member" rather than inside a member's own drawer, and
 * why the faces here are the whole roster: the panel is about all of them at
 * once.
 *
 * ONE STATE, ONE ACTION. The newest launch decides which sentence is shown:
 *
 *   task        the launch is on the Fargate lane  -> its own view, FargateCrewView
 *   none        the crew runs only here            -> Deploy to the cloud
 *   deploying   step N of M                        -> wait; a link to the launch in Settings
 *   signin      the launch needs the user's code   -> Finish the sign-in
 *   deployed    time since deploy, region          -> copy the address
 *   finished    a deploy completed, then, there    -> Deploy again (machine gone)
 *                                                  or check the AWS console (unknown)
 *   failed      the recorded error                 -> Deploy to the cloud
 *
 * TWO LANES, TWO VIEWS. The states below `task` are the EC2 lane's, and their
 * truth is the Instances registry. The Fargate lane registers nothing, so its
 * truth is ECS, read live; that view lives in FargateCrewView.tsx and shares
 * only vocabulary with this one (deployShared.tsx). The dispatch is the one
 * line in `deployView` that reads `provider_id`.
 *
 * "Deployed" is said only while the Instances registry still holds the
 * machine the launch recorded. A teardown from Settings unregisters the
 * instance but never re-statuses the launch, so the launch record alone would
 * read "deployed" forever; a `done` record the registry cannot confirm is
 * therefore a past event ("finished"), said in the past tense, never a present
 * state. See `deployLiveness`.
 *
 * Every navigation lands in Settings > Remote Instances, which owns creating,
 * repairing and tearing down a launch. This panel never writes.
 *
 * HONEST NUMBERS ONLY. Both numbers derive from the launch record the gateway
 * holds: the time since the launch was created, and its region. Sessions
 * running on the deployment and turns it served are deliberately absent: the
 * deployed side has no counter and no path to report back, and the local
 * activity log counts the LOCAL crew, so its numbers here would state a scope
 * the data does not have. An unknown value renders as an en dash, never as 0,
 * because "0" and "we could not read it" must never look the same.
 *
 * DELIBERATELY NOT the crew-summary dashboard's mechanism. That surface renders
 * content a CREW published, so it needs a sandboxed frame and a human-authored
 * template. This panel shows the operator their OWN cloud state, read by the
 * host from `GET /api/cloud/launch`, so it is ordinary trusted React: no
 * sandbox, no template engine, no publish path.
 */
import { Suspense, lazy, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Check, Copy, ExternalLink } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { ApiError, api, type InstanceView, type LaunchJob } from '../../api/client'
import CrewAvatar from '../../components/CrewAvatar'
import ErrorNotice from '../../components/ErrorNotice'
import { Btn } from '../../components/ui'
import {
  Dialog, DialogContent, DialogHeader, DialogBody, DialogFooter, DialogTitle,
} from '../../components/ui/dialog'
import { copyToClipboard } from '../../utils/clipboard'
import { BUILTIN_PROVISIONER_ID, FARGATE_PROVISIONER_ID, launchIsInFlight } from '../../utils/remoteCrew'
import { AWS_REGION_RE, Stat, UNKNOWN, deployAge, deployProgress, deployStepLabel } from './deployShared'

// Its own chunk: the members page loads the Fargate view only when a Fargate
// launch is on screen, and pays nothing for it in the App chunk otherwise.
const FargateCrewView = lazy(() => import('./FargateCrewView'))

// The step and age helpers moved to the shared leaf module when the Fargate
// view arrived; their callers and tests import them from here.
export { deployAge, deployProgress, deployStepLabel } from './deployShared'

/** Where every action this panel offers lands: the Settings tab that owns the
 *  whole launch flow. */
const SETTINGS_PATH = '/settings/instances'

/** How often the launch list is re-read while a launch is still moving. At
 *  rest nothing is polled: a finished or failed launch does not change. */
const IN_FLIGHT_POLL_MS = 4000

/** Faces drawn before the rest collapse into a "+N" tile. */
const MAX_FACES = 6

/**
 * The AWS console home for a region, or undefined when the record's region is
 * missing or not a region id. The panel cannot say whether a machine still
 * runs when the registry cannot; the console can, and this is where it is.
 */
export function awsConsoleUrl(region: string | undefined): string | undefined {
  if (!region || !AWS_REGION_RE.test(region)) return undefined
  // Built through the URL API from one whole-URL literal: the i18n gate reads
  // every literal on a new line, and a template's inner fragment
  // (`.console.aws.amazon.com/...`) is neither a URL nor a path to it.
  const url = new URL('https://console.aws.amazon.com/console/home?region=' + region)
  url.hostname = [region, url.hostname].join('.')
  return url.toString()
}

const LAUNCHES_QUERY_KEY = ['cloud', 'launches'] as const

/** The Instances registry, under the key the Settings panels already use for
 *  it, so both read one cache entry and cannot disagree about which crews are
 *  registered. */
const INSTANCES_QUERY_KEY = ['instances'] as const

/** The face of one crew member, as the roster carries it. */
export interface CrewFace {
  name: string
  avatar?: unknown
}

/**
 * Whether a launch has a machine a reader could reach right now.
 *
 * Only `done` qualifies. `running` is the launch still working, and a job that
 * `failed` or was `cancelled` can still carry an `instance_id` from the attempt
 * that got that far — offering it as reachable would send the reader at a
 * machine that is gone or half-built.
 */
export function deployIsReachable(job: Pick<LaunchJob, 'status' | 'instance_id'>): boolean {
  return job.status === 'done' && !!job.instance_id
}

/**
 * Whether the machine's id is something a reader can paste into Session
 * Manager. Only the built-in EC2 provisioner records an instance id there; the
 * Fargate lane records the task's ARN, which Session Manager does not accept,
 * so that launch finished but has no address to offer here. `provider_id` is
 * absent on records written before it existed, and those were all EC2.
 */
export function deployHasShellTarget(
  job: Pick<LaunchJob, 'status' | 'instance_id' | 'provider_id'>,
): boolean {
  return deployIsReachable(job) && (job.provider_id ?? BUILTIN_PROVISIONER_ID) === BUILTIN_PROVISIONER_ID
}

/**
 * Whether the launch is on the Fargate lane, and so belongs to FargateCrewView
 * rather than to the registry-keyed states here. Keyed on the Fargate id, not
 * on "anything but EC2": a provisioner this panel does not know is classified
 * with the EC2 states (its record is drawn verbatim in Details), because the
 * Fargate view's ECS read would be false of it. Nor is it the negation of
 * `deployHasShellTarget`: an EC2 launch that recorded no machine has no target
 * either, and stays on the EC2 view for the same reason.
 */
export function deployRunsAsTask(job: Pick<LaunchJob, 'provider_id'>): boolean {
  return job.provider_id === FARGATE_PROVISIONER_ID
}

/**
 * The launch the panel is about: the most recently created one.
 *
 * Sorted here rather than trusting the list's order, so a gateway that returns
 * jobs in another order cannot make the panel describe a stale attempt as the
 * current one. Ties keep the list's own order.
 */
export function newestLaunch(jobs: readonly LaunchJob[]): LaunchJob | undefined {
  return [...jobs].sort((a, b) => b.created_at - a.created_at)[0]
}

/** The registry rows the liveness read needs; `undefined` means the registry
 *  could not be read at all (the Instances feature is off, or the read failed). */
export type RegistryRows = readonly Pick<InstanceView, 'ssm_target' | 'ssh_host'>[] | undefined

/**
 * Whether the machine a finished launch recorded still exists NOW.
 *
 *   live     the Instances registry holds it
 *   gone     the registry was read and does not hold it
 *   unknown  nothing can say either way
 *
 * The launch record cannot answer this by itself: a Settings delete tears the
 * stack down, unregisters the instance and removes the uploaded source, but
 * never re-statuses the launch, so a `done` record outlives its machine and on
 * its own would read "deployed" forever. The registry is the field a teardown
 * DOES update, and the EC2 launcher registers the instance (its id as the SSM
 * target, or as the host on a legacy row) as a hard condition of `done`, so for
 * that lane "registered" and "exists" move together.
 *
 * Unknown, deliberately, for everything the registry cannot speak to: a launch
 * that is not a finished EC2 one (the Fargate lane registers nothing by design),
 * and any finished launch when the registry could not be read. Unknown is never
 * rounded to either answer, because both roundings put a false sentence about a
 * billing machine on the screen.
 */
export function deployLiveness(
  job: Pick<LaunchJob, 'status' | 'instance_id' | 'provider_id'>,
  instances: RegistryRows,
): 'live' | 'gone' | 'unknown' {
  if (!deployHasShellTarget(job) || instances === undefined) return 'unknown'
  const id = job.instance_id
  return instances.some((i) => i.ssm_target === id || i.ssh_host === id) ? 'live' : 'gone'
}

/** What the panel says, derived from the newest launch and the registry. */
export type DeployView =
  | { kind: 'none' }
  | { kind: 'task'; job: LaunchJob }
  | { kind: 'deploying'; job: LaunchJob }
  | { kind: 'signin'; job: LaunchJob }
  | { kind: 'deployed'; job: LaunchJob }
  | { kind: 'finished'; job: LaunchJob; liveness: 'gone' | 'unknown' }
  | { kind: 'failed'; job: LaunchJob }

/**
 * Classify the launch list into the one state the panel shows.
 *
 * A launch on the Fargate lane is `task`, whatever its status: that lane has
 * its own view (FargateCrewView), which reads the task's state from ECS, the
 * only source that can speak to it. The states below are the EC2 lane's, whose
 * truth is the Instances registry.
 *
 * `awaiting_signin` is split out of the other in-flight statuses because it is
 * the one where waiting achieves nothing: the launch is holding for the user
 * to approve a code, and a panel that only said "deploying" would leave them
 * waiting on a step that is waiting on them.
 *
 * A `done` launch is `deployed` only while the registry confirms its machine.
 * Otherwise it is `finished`: a past event the record does support (a deploy
 * completed, then, there), said in the past tense, with `liveness` saying
 * whether the machine is known to be gone or simply unknowable from here. The
 * present-tense "is deployed" sentence is never shown on the record alone.
 */
export function deployView(jobs: readonly LaunchJob[], instances: RegistryRows): DeployView {
  const job = newestLaunch(jobs)
  if (!job) return { kind: 'none' }
  if (deployRunsAsTask(job)) return { kind: 'task', job }
  if (job.status === 'awaiting_signin') return { kind: 'signin', job }
  if (launchIsInFlight(job.status)) return { kind: 'deploying', job }
  if (job.status === 'done') {
    const liveness = deployLiveness(job, instances)
    return liveness === 'live' ? { kind: 'deployed', job } : { kind: 'finished', job, liveness }
  }
  return { kind: 'failed', job }
}

/**
 * An older launch whose machine the registry still holds, other than the
 * newest launch's own: the headline follows the newest launch, and this is
 * what it would otherwise bury. Nothing in the launch flow tears an earlier
 * stack down: not a retry that failed, not one still moving, and not a
 * re-deploy that finished, so that machine keeps running (and billing) behind
 * a "did not finish", a "deploying" AND a "deployed" headline alike; the last
 * is the one a reader has least reason to look past. Undefined when no other
 * launch is CONFIRMED live: a record the registry cannot vouch for is not
 * named here, because this line's whole claim is that a machine is running
 * now. Two records naming the same machine are one machine, not another, so an
 * older record on the newest launch's own instance is skipped.
 */
export function earlierReachableLaunch(jobs: readonly LaunchJob[], instances: RegistryRows): LaunchJob | undefined {
  const newest = newestLaunch(jobs)
  if (!newest) return undefined
  return [...jobs]
    .filter((j) => j !== newest && j.instance_id !== newest.instance_id && deployLiveness(j, instances) === 'live')
    .sort((a, b) => b.created_at - a.created_at)[0]
}

/** The crew, as faces. The whole roster, because the deployment is the whole
 *  roster; past `MAX_FACES` the rest collapse into a count. */
function CrewFaces({ members }: { members: readonly CrewFace[] }) {
  const shown = members.slice(0, MAX_FACES)
  const extra = members.length - shown.length
  if (shown.length === 0) return null
  return (
    <div className="flex items-center justify-center" data-testid="deploy-faces">
      {shown.map((m, i) => (
        <CrewAvatar
          key={m.name}
          seed={m.name}
          avatar={m.avatar}
          size={56}
          className={`rounded-lg ${i > 0 ? '-ml-3' : ''}`}
        />
      ))}
      {extra > 0 && (
        <span
          className="-ml-3 w-14 h-14 shrink-0 grid place-items-center rounded-lg border border-border bg-bg-elevated text-[13px] font-semibold text-muted"
          data-testid="deploy-faces-more"
        >
          +{extra}
        </span>
      )}
    </div>
  )
}

/**
 * The deployed crew's Session Manager target, with the one action the deployed
 * state offers. The value stays selectable text beside the button, so a reader
 * whose clipboard is denied can still read it, and a failed copy says so in
 * the shared error surface rather than leaving the button silent.
 */
function AddressRow({ value, onHandoff }: { value: string; onHandoff: () => void }) {
  const { t } = useTranslation()
  const [done, setDone] = useState(false)
  const [failed, setFailed] = useState(false)
  return (
    <div className="flex flex-col gap-1.5" data-testid="deploy-address">
      <div className="text-[11px] text-muted">{t('pages.membersPage.deploy_address')}</div>
      <div className="flex items-center gap-2">
        <code
          className="flex-1 min-w-0 break-all text-[12px] text-text bg-bg-hover rounded px-2 py-1"
          data-testid="deploy-address-value"
        >
          {value}
        </code>
        <Btn
          primary
          onClick={() => {
            // The guarded helper, not `navigator.clipboard` directly: on a
            // plain-HTTP remote dashboard the async API does not exist, and the
            // helper falls back to `execCommand` before reporting. It never
            // rejects, so the boolean is the whole outcome, and the tick is
            // gated on it: a tick over an unchanged clipboard is the worst
            // affordance there is.
            void copyToClipboard(value).then((ok) => {
              setDone(ok)
              setFailed(!ok)
            })
          }}
          data-testid="deploy-address-copy"
        >
          {done ? <Check size={14} /> : <Copy size={14} />}
          {done ? t('pages.membersPage.deploy_copied') : t('pages.membersPage.deploy_copy')}
        </Btn>
      </div>
      {/* askAgent ON: nothing here is unsaved, so the hand-off can destroy
          nothing, and the agent has a real remedy the message does not (read
          the id off the launch record and open the session itself). The dialog
          closes first, as for the panel's other two notices. */}
      {failed && (
        <ErrorNotice
          variant="inline"
          message={t('pages.membersPage.deploy_copy_failed')}
          askAgent
          onHandoff={onHandoff}
          testId="deploy-address-copy-error"
        />
      )}
      <div className="text-[11px] text-muted">{t('pages.membersPage.deploy_address_hint')}</div>
    </div>
  )
}

/**
 * The full-window panel.
 *
 * `open` is the host's state so the trigger and the panel do not both own it.
 * The query runs only while open (`enabled`), so a page visit that never opens
 * this panel makes no launch read at all; while a launch is moving it re-reads
 * every few seconds so the step count advances without a reopen.
 */
export default function DeployMyCrewDialog({
  open,
  onClose,
  members,
}: {
  open: boolean
  onClose: () => void
  members: readonly CrewFace[]
}) {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const q = useQuery({
    queryKey: LAUNCHES_QUERY_KEY,
    queryFn: () => api.cloudLaunches(),
    enabled: open,
    refetchInterval: (query) =>
      query.state.data?.jobs.some((j) => launchIsInFlight(j.status)) ? IN_FLIGHT_POLL_MS : false,
  })
  const jobs = useMemo(() => q.data?.jobs ?? [], [q.data])
  // The registry read that turns "a launch finished" into "its machine exists".
  // Only needed once some launch has finished; a 403 is the Instances feature
  // being off, which no retry changes, so it is not retried.
  const needRegistry = jobs.some((j) => j.status === 'done')
  const inst = useQuery({
    queryKey: INSTANCES_QUERY_KEY,
    queryFn: () => api.listInstances(),
    enabled: open && needRegistry,
    retry: (count, err) => !(err instanceof ApiError && err.status === 403) && count < 2,
  })
  const registry: RegistryRows = inst.data?.instances
  // A 403 is a capability answer (the Instances feature is off), so the
  // liveness it leaves unknown is the honest state. Any other failure of the
  // registry read is an error like a failed launch read, and is reported as
  // one: not rendered as the unknown sentence, which would say "this page
  // cannot tell" where the truth is "this read failed".
  const registryFailed = inst.isError && !(inst.error instanceof ApiError && inst.error.status === 403)
  const view = useMemo(() => deployView(jobs, registry), [jobs, registry])
  const earlier = useMemo(() => earlierReachableLaunch(jobs, registry), [jobs, registry])
  const ordered = useMemo(() => [...jobs].sort((a, b) => b.created_at - a.created_at), [jobs])
  // The Fargate view depends on the launch read alone: its truth is ECS, read
  // by the view itself, so a pending or failed registry read neither delays
  // nor hides it. The registry still gates the EC2 states and the
  // earlier-launch line, and a failed registry read is still reported beside
  // the task view, because that line is what the failure has silenced.
  const taskView = view.kind === 'task'
  // Still reading while either answer that decides the sentence is pending, so
  // a finished launch is not first drawn as unconfirmed and then as deployed.
  const loading = q.isPending || (q.isSuccess && needRegistry && inst.isPending && !taskView)
  const readFailed = q.isError || registryFailed
  const ready = q.isSuccess && !loading && (!registryFailed || taskView)

  // Closed first, then navigated: the destination is another page, so an open
  // dialog would otherwise be the first thing a reader sees when they come back.
  const goToSettings = () => {
    onClose()
    navigate(SETTINGS_PATH)
  }

  return (
    <Dialog open={open} onOpenChange={(v) => { if (!v) onClose() }}>
      <DialogContent maxWidth={520} className="max-h-[86vh]">
        <DialogHeader>
          <DialogTitle>{t('pages.membersPage.deploy_title')}</DialogTitle>
        </DialogHeader>
        <DialogBody className="flex flex-col gap-4">
          <CrewFaces members={members} />

          {loading && (
            <div className="text-[12px] text-muted text-center" data-testid="deploy-loading">
              {t('pages.membersPage.deploy_loading')}
            </div>
          )}

          {/* A failed read is reported as unknown, never as "your crew runs
              only here": the second would tell a reader their crew is not
              deployed when the truth is that we could not find out. Both
              reads the sentence depends on land here: the launch list, and
              the registry read that decides whether a finished launch is
              live (a 403 from it is not a failure, see `registryFailed`). */}
          {readFailed && (
            <div className="flex flex-col items-center gap-2" data-testid="deploy-error">
              <ErrorNotice
                message={t('pages.membersPage.deploy_error')}
                askAgent
                onHandoff={onClose}
                className="w-full"
              />
              <Btn onClick={() => { if (q.isError) void q.refetch(); else void inst.refetch() }} data-testid="deploy-retry">
                {t('pages.membersPage.deploy_retry')}
              </Btn>
            </div>
          )}

          {/* The Fargate lane's own view: it reads the task's state from ECS,
              the only source that can speak to it, and says nothing the
              registry-keyed EC2 states below say. It waits on the launch read
              alone; the registry gates only the EC2 states and the
              earlier-launch line. */}
          {ready && view.kind === 'task' && (
            <Suspense
              fallback={
                <div className="text-[12px] text-muted text-center" data-testid="deploy-task-loading">
                  {t('pages.membersPage.deploy_task_loading')}
                </div>
              }
            >
              <FargateCrewView job={view.job} open={open} onClose={onClose} goToSettings={goToSettings} />
            </Suspense>
          )}

          {ready && view.kind === 'none' && (
            <div className="flex flex-col items-center gap-3 text-center" data-testid="deploy-state-none">
              <p className="m-0 text-[14px] font-medium">{t('pages.membersPage.deploy_state_none')}</p>
              <Btn primary onClick={goToSettings} data-testid="deploy-action-deploy">
                {t('pages.membersPage.deploy_action_deploy')}
              </Btn>
              {/* The label names what the button does (open Settings) and where,
                  never the outcome a reader wants: a button that reads as the
                  act itself is not pressed by a reader who fears something is
                  created outside this computer right now. The line under it
                  says the one thing the label cannot: nothing is created until
                  the steps there are confirmed. */}
              <div className="text-[11.5px] text-muted" data-testid="deploy-action-hint">
                {t('pages.membersPage.deploy_action_hint')}
              </div>
            </div>
          )}

          {ready && view.kind === 'deploying' && (
            <div className="flex flex-col items-center gap-1 text-center" data-testid="deploy-state-deploying">
              <p className="m-0 text-[14px] font-medium">{t('pages.membersPage.deploy_state_deploying')}</p>
              {/* "Step 2 of 4" says how far; the step's own name says what
                  step 2 is, in the gateway's wording. An older record with no
                  steps shows the counter alone. */}
              <div className="text-[12px] text-text" data-testid="deploy-progress">
                {t('pages.membersPage.deploy_step_progress', deployProgress(view.job))}
                {deployStepLabel(view.job) && (
                  <span className="text-muted" data-testid="deploy-step-label">
                    {' \u00b7 '}
                    {deployStepLabel(view.job)}
                  </span>
                )}
              </div>
              {/* No duration estimate: nothing in the repo measures a typical
                  launch, and a number nothing attests is not shown here (the
                  same rule the two stat cards follow). The step counter is
                  the progress. */}
              {/* The launch runs in the gateway, not in this dialog; a reader
                  who fears Close cancels it stays and babysits the window. */}
              <div className="text-[11.5px] text-muted" data-testid="deploy-close-hint">
                {t('pages.membersPage.deploy_close_hint')}
              </div>
              {/* Not an action on the launch, which this panel never has, but
                  the way to the place that does: the Settings page lists the
                  moving launch with its Cancel. Muted and link-styled, so the
                  state still reads as "wait", with a door rather than a
                  button; a reader told nothing here stops the deploy is
                  otherwise left with no idea where anything would. */}
              <button
                type="button"
                onClick={goToSettings}
                className="bg-transparent border-0 p-0 text-[11.5px] text-accent hover:underline cursor-pointer"
                data-testid="deploy-action-manage"
              >
                {t('pages.membersPage.deploy_action_manage')}
              </button>
            </div>
          )}

          {ready && view.kind === 'signin' && (
            <div className="flex flex-col items-center gap-3 text-center" data-testid="deploy-state-signin">
              <p className="m-0 text-[14px] font-medium">{t('pages.membersPage.deploy_state_signin')}</p>
              <Btn primary onClick={goToSettings} data-testid="deploy-action-signin">
                {t('pages.membersPage.deploy_action_signin')}
              </Btn>
              {/* The same reassurance as the deploying state: the launch keeps
                  waiting in the gateway whether or not this window is open. */}
              <div className="text-[11.5px] text-muted" data-testid="deploy-close-hint">
                {t('pages.membersPage.deploy_close_hint')}
              </div>
            </div>
          )}

          {ready && view.kind === 'deployed' && (
            <div className="flex flex-col gap-3" data-testid="deploy-state-deployed">
              <p className="m-0 text-[14px] font-medium text-center">
                {t('pages.membersPage.deploy_state_deployed')}
              </p>
              {/* Honest counters only: both come off the launch record. */}
              <div className="grid grid-cols-2 gap-2" data-testid="deploy-stats">
                <Stat
                  value={deployAge(view.job.created_at)}
                  label={t('pages.membersPage.deploy_stat_since')}
                  testid="deploy-stat-since"
                />
                <Stat
                  value={view.job.region || UNKNOWN}
                  label={t('pages.membersPage.deploy_stat_region')}
                  testid="deploy-stat-region"
                />
              </div>
              {/* Deployed means the registry confirmed an EC2 machine, so the
                  target is always there to offer; the guard keeps the id
                  narrowed and the rule in one place. */}
              {deployHasShellTarget(view.job) && view.job.instance_id && (
                <AddressRow value={view.job.instance_id} onHandoff={onClose} />
              )}
            </div>
          )}

          {ready && view.kind === 'finished' && (
            <div className="flex flex-col items-center gap-3 text-center" data-testid="deploy-state-finished">
              {/* A past event, in the past tense: the record supports "a deploy
                  finished, then, there" and nothing about now. Two sentences,
                  by what the registry could say: it was read and does not hold
                  the machine (gone), or it could not speak to this launch at
                  all (unknown: an EC2 record that kept no machine, or the
                  registry could not be read). Neither ever says the crew IS
                  deployed, and neither says it is not. */}
              <p className="m-0 text-[14px] font-medium">
                {t(
                  view.liveness === 'gone'
                    ? 'pages.membersPage.deploy_state_gone'
                    : 'pages.membersPage.deploy_state_unknown',
                  { age: deployAge(view.job.created_at), region: view.job.region || UNKNOWN },
                )}
              </p>
              {/* Where the answer this panel cannot give does live. The whole
                  sentence is the link when the record's region parses as one,
                  so the reader who is pointed at the console is not then left
                  to find it; a record with no usable region keeps the words. */}
              {view.liveness === 'unknown' && (() => {
                const url = awsConsoleUrl(view.job.region)
                const words = t('pages.membersPage.deploy_check_console', { region: view.job.region || UNKNOWN })
                return (
                  <div className="text-[11.5px] text-muted" data-testid="deploy-check-console">
                    {url ? (
                      <a
                        href={url}
                        target="_blank"
                        rel="noreferrer"
                        className="inline-flex items-center gap-1 text-accent underline"
                        data-testid="deploy-console-link"
                      >
                        {words}
                        {/* The link leaves the app: said with the glyph, and
                            the underline stays, so a reader can tell advice
                            from a door without hovering. */}
                        <ExternalLink size={11} aria-hidden="true" />
                      </a>
                    ) : words}
                  </div>
                )
              })()}
              {/* The machine is gone, so the one thing to do is the same as
                  with no launch at all: the set-up flow, with its line. */}
              {view.liveness === 'gone' && (
                <>
                  <Btn primary onClick={goToSettings} data-testid="deploy-action-deploy">
                    {t('pages.membersPage.deploy_action_deploy')}
                  </Btn>
                  <div className="text-[11.5px] text-muted" data-testid="deploy-action-hint">
                    {t('pages.membersPage.deploy_action_hint')}
                  </div>
                </>
              )}
            </div>
          )}

          {ready && view.kind === 'failed' && (
            <div className="flex flex-col items-center gap-3 text-center" data-testid="deploy-state-failed">
              <p className="m-0 text-[14px] font-medium">{t('pages.membersPage.deploy_state_failed')}</p>
              {/* The gateway's own sentence about what went wrong, verbatim,
                  through the shared error surface so the agent can take it. */}
              <ErrorNotice
                message={view.job.error}
                askAgent
                onHandoff={onClose}
                messageClassName="font-mono text-left"
                className="w-full"
                testId="deploy-launch-error"
              />
              <Btn primary onClick={goToSettings} data-testid="deploy-action-deploy">
                {t('pages.membersPage.deploy_action_deploy')}
              </Btn>
              <div className="text-[11.5px] text-muted" data-testid="deploy-action-hint">
                {t('pages.membersPage.deploy_action_hint')}
              </div>
            </div>
          )}

          {/* The headline follows the newest launch; this names what that would
              otherwise bury: another launch whose machine the registry still
              holds, running (and billing) behind a failed retry, a launch still
              moving, a finish the registry cannot vouch for, or a re-deploy
              that finished, since none of those tears the earlier stack down.
              The sentence ends with where to act, because a warning about a
              paying machine with no way to stop it only frightens. */}
          {ready && earlier && (
            <div className="text-[12px] text-text text-center" data-testid="deploy-earlier-live">
              {t('pages.membersPage.deploy_earlier_live', { region: earlier.region || UNKNOWN })}
            </div>
          )}

          {/* The identifiers an owner debugging a deploy needs, one line per
              launch, newest first, collapsed by default. The raw status token
              is deliberate here: it is the searchable name the gateway logs
              use, and the sentence above is the plain-language version. */}
          {ordered.length > 0 && (
            <details className="text-[11.5px] text-muted" data-testid="deploy-details">
              <summary className="cursor-pointer select-none">{t('pages.membersPage.details')}</summary>
              <ul className="list-none m-0 mt-1.5 p-0 space-y-1">
                {ordered.map((job) => (
                  <li key={job.id} className="font-mono text-[11px] break-words" data-testid="deploy-launch">
                    {[job.tag, job.provider_id, job.status, job.profile, job.region, job.instance_id]
                      .filter(Boolean)
                      .join(' \u00b7 ')}
                  </li>
                ))}
              </ul>
            </details>
          )}
        </DialogBody>
        <DialogFooter>
          <Btn onClick={onClose} data-testid="deploy-close">
            {t('pages.membersPage.close')}
          </Btn>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
