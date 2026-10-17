/**
 * RemoteCrewPanel — Settings → Remote Crew. One page, two tabs:
 *
 *   1. "Your crews" (default) — the machines you can switch to from the top
 *      header: any in-progress cloud launch (a durable gateway job), the
 *      connected/added instances, and the add-a-machine form. Cloud-launched
 *      crews are told apart from hand-added ones by correlating each SSM
 *      instance's target id with a launch job's `instance_id`, so cloud rows can
 *      offer the cloud lifecycle (Stop / Delete-by-tag) that a plain tunnel row
 *      cannot.
 *   2. "Set up a new one" — an AWS prerequisite checklist (from the cloud
 *      preflight) and a launch form that spins up a cloud-hosted crew on the
 *      user's OWN AWS account, then a progress card that polls the launch job.
 *
 * The instance CRUD (connect / disconnect / diagnose / remove) and the
 * enable/disable gate mirror InstancesPanel; the add-existing form and the
 * StatusBadge are reused from it directly.
 *
 * This file keeps the panel component; its parts live in sibling owners:
 * `remoteCrewPanelModel.ts` (launch-job predicates, size tiers, remembered keys),
 * `RemoteCrewPanelLaunch.tsx` (the setup tab's cards and the sign-in block) and
 * `RemoteCrewPanelCrewRow.tsx` (one crew row, the turn-API URL field).
 */
import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import DefaultDestinationCrewPicker from '../../components/DefaultDestinationCrewPicker'
import {
  Server,
  Rocket,
  RefreshCw,
  Stethoscope,
  AlertTriangle,
  CheckCircle,
  Copy,
  Check,
  ExternalLink,
  ChevronDown,
  Power,
  X,
} from 'lucide-react'
import {
  api,
  ApiError,
  isAuthExpiredError,
  type InstanceView,
  type LaunchJob,
  type CloudPreflight,
  type CloudCoords,
} from '../../api/client'
import {
  BUILTIN_PROVISIONER_ID,
  WARM_SET_CAP_AUTO_CEILING,
  hasDashboardPane,
  launchIsInFlight,
  usesSsmTransport,
} from '../../utils/remoteCrew'
import { Card, Btn } from '../../components/ui'
import { SettingsToggle } from '../../components/settings'
import ErrorNotice from '../../components/ErrorNotice'
import ErrorBoundary from '../../components/ErrorBoundary'
import {
  announceChainedCrew,
  clearChainRefusal,
  readChainRefusal,
  subscribeChainRefusal,
} from '../../lib/chainAnnounce'
import {
  BUILTIN_REMOTE_PROVISIONER_KINDS,
  canRenderRemoteProvisionerKind,
  getRemoteProvisionerRenderer,
} from '../../components/remoteProvisionerRenderers'
import type { ErrorReport } from '../../utils/errorReport'
import { parseErrorCode } from '../../utils/errorReport'
import { reportInstanceFailure } from '../../utils/instanceFailureReport'
import { isInstancesDisabledError } from '../../utils/instancesDisabled'
import { readPersistedString, usePersistedString } from '../../hooks/usePersistedString'
import { usePersistedBool } from '../../hooks/usePersistedBool'
import { AUTO_CONNECT_KEY } from '../../hooks/useAutoConnectInstances'
import { copyToClipboard } from '../../utils/clipboard'
import { useAppDispatch, useAppSelector } from '../../store'
import { removeWarm, setCrewEditForm, setCrewLaunchDraft, dropLaunchedCrewDraft, type CrewLaunchDraft } from '../../store/instancesSlice'
import { setCrewEnabled } from '../../lib/crewEnabled'
import { i18nT } from '../../i18n/t'
import { AddInstanceForm, RestartToActivateText } from './InstancesPanel'
import { CrewVersionsReadError } from './CrewVersionStatus'
import { awsCliRefusalCopy, awsCliRefusalReport, credentialHelperMissingSentence, pluginRefusalCopy, pluginRefusalReport } from './remoteCrewPluginRefusal'
import {
  instanceFormFromView,
} from './InstanceFormFields'
import {
  isInProgress, CLOUD_PROFILE_KEY, CLOUD_REGION_KEY, CLOUD_SIZE_KEY, CLOUD_PROVISIONER_KEY,
  DEFAULT_REGION, isTerminal, isRegistered, needsSignin, type SizeTier, SIZE_TIERS, X86_TIERS,
  PRICING_CALCULATOR_URL, type SigninNotice,
} from './remoteCrewPanelModel'
import {
  SizeCard, ProvisionerCard, SettingUpRow, PrereqRow, LaunchProgressCard,
} from './RemoteCrewPanelLaunch'
import { CREW_SWITCH_HINT_ID, CREW_NEEDS_RESTART_BANNER_ID, CrewRow } from './RemoteCrewPanelCrewRow'
export { TurnUrlField } from './RemoteCrewPanelCrewRow'

export function RemoteCrewPanel() {
  const queryClient = useQueryClient()
  const dispatch = useAppDispatch()
  const [tab, setTab] = useState<'crews' | 'setup'>('crews')
  // Default-on: when set, the web app auto-connects every crew on load and on
  // tab focus (see useAutoConnectInstances). Off lets a many-crew user stop the
  // per-load SSH + token-mint fan-out.
  const [autoConnect, setAutoConnect] = usePersistedBool(AUTO_CONNECT_KEY, true)

  // Setup-tab form + preflight state. `checkedProfile`/`checkedRegion` are the
  // committed values the preflight ran against, so typing a profile does not
  // hammer AWS on every keystroke — a check fires on first open, on blur, and on
  // the explicit Re-check.
  //
  // Both are persisted: this panel unmounts when you visit another Settings
  // section, and losing the profile meant more than retyping — `checkedProfile`
  // fell back to '', so the next probe silently tested the AWS CLI *default*
  // profile and reported someone else's expired credentials. The committed
  // mirrors seed from the same keys on this first render so the very first probe
  // uses the remembered account. Profile and region are names, not secrets —
  // the panel's own copy promises the profile NAME is all that is kept.
  const [profile, setProfile] = usePersistedString(CLOUD_PROFILE_KEY, '')
  const [region, setRegion] = usePersistedString(CLOUD_REGION_KEY, DEFAULT_REGION)
  const [checkedProfile, setCheckedProfile] = useState(() => readPersistedString(CLOUD_PROFILE_KEY, ''))
  const [checkedRegion, setCheckedRegion] = useState(() => readPersistedString(CLOUD_REGION_KEY, DEFAULT_REGION))
  // Opened when the REMEMBERED size lives inside it. Derived rather than persisted
  // on its own: the extra tiers are behind this disclosure, so a remembered x86
  // size with the section closed would drive the launch while its card was not on
  // screen — a worse failure than the reset, because nothing shows what is selected.
  const [showMoreSizes, setShowMoreSizes] = useState(
    () => X86_TIERS.some(t => t.key === readPersistedString(CLOUD_SIZE_KEY, 'balanced')),
  )
  const [persistedSize, setSizeKey] = usePersistedString(CLOUD_SIZE_KEY, 'balanced')
  // A value written by an older build — or naming a tier since removed — must not
  // select a tier that no longer exists: every card would render unselected while
  // the launch still carried the stale id.
  const sizeKey = (
    SIZE_TIERS.some(t => t.key === persistedSize) || X86_TIERS.some(t => t.key === persistedSize)
      ? persistedSize
      : 'balanced'
  ) as SizeTier['key']
  // The provisioner the setup tab draws. '' means "not chosen yet", which resolves
  // to the first renderable row below.
  const [persistedProvisioner, setProvisionerId] = usePersistedString(CLOUD_PROVISIONER_KEY, '')
  const [copied, setCopied] = useState<'command' | 'policy' | null>(null)
  // The Kiro identity the crew signs in as. Preselected from the launching
  // machine's own sign-in (an Identity Center user gets their organization's
  // portal, not the Builder ID one) and overridable; the server re-validates.
  // `identity_region` is the IAM Identity Center region — NOT the EC2 region.
  // Seeded from the reader's own unlaunched choice when the store holds one
  // (crewForms.launch): that choice is theirs, so it counts as touched and the
  // inherited identity does not overwrite it.
  const heldLaunchDraft = useAppSelector(s => s.instances.crewForms?.launch ?? null)
  const [launchDraft] = useState(heldLaunchDraft)
  const identityDraft = launchDraft?.identity ?? null
  const [identityMode, setIdentityMode] = useState<'builder_id' | 'identity_center'>(identityDraft?.mode ?? 'builder_id')
  const [identityStartUrl, setIdentityStartUrl] = useState(identityDraft?.start_url ?? '')
  const [identityRegion, setIdentityRegion] = useState(identityDraft?.region ?? '')
  const [subnetId, setSubnetId] = useState(launchDraft?.subnet_id ?? '')
  const identityTouched = useRef(identityDraft !== null)
  // Render-visible twin of the ref: an explicit choice must re-render the
  // Launch gate even when it re-selects the already-checked default.
  const [identityChosen, setIdentityChosen] = useState(identityDraft !== null)
  // Only a sign-in choice the reader made is kept: the inherited preselection
  // is read again on the next visit anyway.
  const currentLaunchDraft = useMemo<CrewLaunchDraft | null>(
    () => (subnetId || identityChosen
      ? { subnet_id: subnetId, identity: identityChosen ? { mode: identityMode, start_url: identityStartUrl, region: identityRegion } : null }
      : null),
    [subnetId, identityChosen, identityMode, identityStartUrl, identityRegion],
  )
  // Held on every edit, like the add form, so every exit keeps it.
  useEffect(() => { dispatch(setCrewLaunchDraft(currentLaunchDraft)) }, [dispatch, currentLaunchDraft])
  // The draft the last built-in launch was sent with, so its success drops that
  // draft and not one typed while the request was in flight.
  const sentLaunchDraft = useRef<CrewLaunchDraft | null>(null)
  const identityQuery = useQuery({
    queryKey: ['cloud-identity'],
    queryFn: api.cloudIdentity,
    staleTime: 60_000,
    retry: false,
  })
  useEffect(() => {
    // Seed ONCE from the inherited identity; never overwrite a user's edits.
    if (identityTouched.current) return
    const suggested = identityQuery.data?.suggested_target
    if (suggested?.start_url) {
      setIdentityMode('identity_center')
      setIdentityStartUrl(suggested.start_url)
    }
  }, [identityQuery.data])
  // Mirrors normalize_start_url on the backend, which is the authority: the
  // scheme may be omitted (https is assumed), the host is any valid DNS name
  // (Identity Center portals live in other partitions and on custom domains,
  // not only <org>.awsapps.com), and characters that would be unsafe on the
  // remote shell are refused. The form only decides whether Launch is enabled.
  const identityStartUrlOk = (() => {
    const v = identityStartUrl.trim()
    if (!v || /[\s"'`$\\;&|<>(){}[\]*?!~#]/.test(v)) return false
    if (/^[a-z][a-z0-9+.-]*:\/\//i.test(v) && !/^https:\/\//i.test(v)) return false // http:, ftp:, ...
    const bare = v.replace(/^https:\/\//i, '')
    return /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+(\/[^?#]*)?$/i.test(bare)
  })()
  const identityRegionOk = /^[a-z]{2}(-[a-z]+)+-\d{1,2}$/.test(identityRegion.trim())
  // Until the launching computer's identity has been READ (or the user has
  // made a choice themselves), the Builder ID default is a placeholder, not a
  // decision: a returning operator with satisfied prerequisites could click
  // Launch inside that window and send no target, and an Identity Center
  // preselection that lands a moment later would have been ignored. A read
  // that could not answer -- the server reports `discovery: 'unknown'`, or the
  // request itself failed -- is the same placeholder: nothing is known about
  // this computer's sign-in, so the inline notice explains it and Launch waits
  // for the user to pick by hand. An Identity Center user whose whoami timed
  // out must not be launched as Builder ID by a preselection they never saw
  // was a guess.
  const identityUnknown = identityQuery.isError || identityQuery.data?.discovery === 'unknown'
  // The server reports the two unknown causes distinctly: `identity` is null
  // when whoami did not answer, and carries the Identity Center account type
  // when the sign-in was read but its portal address was not. The notice names
  // the one that happened rather than handing the user the disjunction.
  const identityUnknownCause: 'no_answer' | 'no_portal' | null =
    identityQuery.data?.discovery === 'unknown'
      ? identityQuery.data.identity?.account_type === 'IamIdentityCenter' ? 'no_portal' : 'no_answer'
      : null
  const identityResolving = (identityQuery.isPending || identityUnknown) && !identityChosen
  // While nothing is known and the user has not chosen, no radio renders
  // checked: a checked Builder ID beside a gate that says "choose" reads as a
  // choice already made. A read identity (or the user's own click) shows one.
  const identityRadioShown = identityChosen || !identityUnknown
  const identityOk =
    !identityResolving && (identityMode === 'builder_id' || (identityStartUrlOk && identityRegionOk))
  const loginTargetBody = identityMode === 'identity_center'
    ? { login_target: { license: 'pro', start_url: identityStartUrl.trim(), region: identityRegion.trim() } }
    : {}
  /** A failed copy, pinned to the checklist row whose button was pressed. */
  const [copyErr, setCopyErr] = useState<{ target: 'command' | 'policy'; message: string } | null>(null)
  const [activeLaunchId, setActiveLaunchId] = useState<string | null>(null)
  const [confirmDeleteTag, setConfirmDeleteTag] = useState<string | null>(null)
  const [confirmRemoveId, setConfirmRemoveId] = useState<string | null>(null)
  // Only one crew is editable at a time: two open forms on the same list would
  // let the user save conflicting ports without ever seeing the clash.
  const [editingId, setEditingId] = useState<string | null>(null)
  // Unsaved work in the open form. Swapping rows would unmount it and lose typed
  // host/port corrections silently, so the swap is refused instead.
  // The unsaved edit itself, keyed by crew — NOT a boolean. The form unmounts
  // whenever the crew list does (switching to the setup tab is enough) AND when
  // the error → agent hand-off navigates out of Settings entirely, and a guard can
  // only refuse the exits it knows about. Holding it in the store rather than in
  // this component means the work outlives every one of those exits without a
  // guard per exit, and without a serialised copy that would have to be
  // re-measured against a server record on the way back.
  // `seq` counts REBASES, and is used as the form's React key: adopting the current
  // record rewrites the draft's values, and a mounted form cannot re-seed itself.
  const editDraft = useAppSelector(s => s.instances.crewForms?.edit ?? null)
  // Which row's Edit was refused, not a bare flag: the refusal has to render at
  // the row the user actually clicked. Shown once at the bottom of the Card it
  // could sit off-screen in a long crew list, so the click looked like a no-op.
  const [editBlockedId, setEditBlockedId] = useState<string | null>(null)
  // Tags whose delete has been accepted by the gateway but not yet confirmed by AWS.
  // The DELETE endpoint returns `cleanup: "pending"` the moment the CloudFormation
  // delete is *requested* — the local registry row is only dropped minutes later, by
  // the gateway's background teardown watcher, once AWS reports DELETE_COMPLETE. Until
  // then the row is still returned by listInstances(), so without this the row simply
  // reappears unchanged after the click and looks like nothing happened. We remember
  // the tag to (a) show a "Deleting…" state on its row and (b) poll the list so the
  // row disappears on its own when the teardown finishes.
  const [deletingTags, setDeletingTags] = useState<Set<string>>(new Set())
  const [actionErr, setActionErr] = useState<string | null>(null)
  // A crew connected HERE that the gateway showing this page declined to adopt as
  // a tab of its own. Separate from `actionErr` because the connect succeeded:
  // folding it in would report a working crew as a failed connect.
  // Read from the module store, not from state set by a listener mounted here: the
  // host answers while the user is watching the crew connect, and this panel may
  // be closed by then. See `chainAnnounce.ts`.
  const chainRefusalNotice = useSyncExternalStore(
    subscribeChainRefusal,
    readChainRefusal,
    readChainRefusal,
  )
  const chainRefusal = chainRefusalNotice
    ? chainRefusalNotice.reason || i18nT('pages.settings.instancesPanel.unknown_error')
    : null
  // The last sign-in fetch/recheck outcome, for the job it belongs to. Rendered
  // inside that job's sign-in block, beside the button that produced it.
  const [signinNotice, setSigninNotice] = useState<({ jobId: string } & SigninNotice) | null>(null)
  // `kind` decides the surface: only `warn` (a negative ladder verdict, or the
  // tunnel's own `status.error`) is an error. `ok` / `info` describe a state that
  // has not gone wrong — healthy, or simply not connected yet — and render as a
  // status note, never as a red ErrorNotice with an agent hand-off. Mirrors
  // InstancesPanel's classification.
  const [diagNote, setDiagNote] = useState<{ kind: 'ok' | 'info' | 'warn'; text: string } | null>(null)
  // The diagnosis note's own report, so the hand-off carries the ladder's verdict
  // code and probe chain rather than the `id: reason` string on screen. Held as an
  // object because message text is not an identity: two crews unreachable the same
  // way produce byte-identical prose.
  const [diagReport, setDiagReport] = useState<ErrorReport | null>(null)
  const [restartPending, setRestartPending] = useState(false)

  const errMsg = useCallback(
    (e: unknown, fallback: string) => (e instanceof ApiError ? e.message : e instanceof Error ? e.message : fallback),
    [],
  )

  const instancesQuery = useQuery({
    queryKey: ['instances'],
    queryFn: () => api.listInstances(),
    // A delete only *requests* the teardown; the row is dropped later by the gateway's
    // background watcher once AWS confirms. Without polling the list would never
    // refetch again after the click's one invalidation, so the row would sit there
    // until an unrelated refetch. Poll while any delete is in flight, then stop.
    refetchInterval: () => (deletingTags.size > 0 ? 4000 : false),
  })
  // Keyed on the gateway's `instances_disabled` code, not the message text, so
  // an owner-only or Slack-origin 403 shows as a load error, not as "turned off".
  const disabled = isInstancesDisabledError(instancesQuery.error)
  // Any OTHER failure is a load error, not "you have no crews": rendering the
  // empty state over it would tell the user their crews are gone.
  // Both queries gate the crew list: a row's cloud-vs-manual identity comes from the
  // Enabled (data present, no 403) but not active => the flag was set after the
  // gateway started, so tunnels cannot be opened until it restarts. Connect would
  // return 503. Same distinction InstancesPanel draws.
  const needsRestart = !disabled && instancesQuery.data?.active === false

  const launchesQuery = useQuery({
    queryKey: ['cloud', 'launches'],
    queryFn: () => api.cloudLaunches(),
    // Owner-only; a non-owner 403 just yields no cloud rows.
    enabled: !disabled,
    refetchInterval: q => {
      const jobs = (q.state.data as { jobs?: LaunchJob[] } | undefined)?.jobs ?? []
      return jobs.some(isInProgress) ? 4000 : false
    },
  })

  const launches = useMemo(() => launchesQuery.data?.jobs ?? [], [launchesQuery.data])
  const inProgress = useMemo(() => launches.filter(isInProgress), [launches])

  // An OLDER gateway POSIX-gates the read-only launch-history route too, so a
  // Windows host answers 400 posix_host_required for it. That is not a load
  // failure — the gateway is refusing a capability, not failing to read — and
  // letting it reach `loadError` below replaced the entire crew list, hand-added
  // SSH rows included, with a cloud-provisioning error and no way to connect,
  // edit or remove anything. Current gateways answer the route on every
  // platform; this keeps the panel usable against one that does not.
  //
  // Safe to proceed with no launch history — and it does NOT rest on the host
  // having no cloud crews, which a carried-over config dir would break. The row
  // itself does not assume: an SSM target with no matching job renders as
  // `unverifiedCloud`, keeping the confirm step and the copy about what Remove
  // does not do.
  const cloudUnsupported =
    launchesQuery.error instanceof ApiError &&
    launchesQuery.error.status === 400 &&
    parseErrorCode(launchesQuery.error.body) === 'posix_host_required'

  // Both queries gate the crew list: a row's cloud-vs-manual identity comes from the
  // launch history, and the two destructive actions are NOT interchangeable. Treating
  // absent launch data as [] makes a real cloud crew render as "added by you", whose
  // trash button is a single unconfirmed click that unregisters the instance and
  // leaves the EC2 stack running and billing, invisible to the dashboard. So the list
  // waits until both are known, and surfaces either failure instead of guessing.
  const loadError =
    !disabled && (instancesQuery.isError || (launchesQuery.isError && !cloudUnsupported))
  const authExpired =
    isAuthExpiredError(instancesQuery.error) || isAuthExpiredError(launchesQuery.error)
  const listLoading = !disabled && (instancesQuery.isLoading || launchesQuery.isLoading)

  // `activeLaunchId` is component state, so navigating away and back loses it while
  // the gateway keeps driving the job. Falling back to the persisted in-progress job
  // is what makes "Keeps running if you leave this page" true for the sign-in step:
  // without it the device code and verification link — the only way to finish setup —
  // are unreachable after a remount, and the user's only recovery is cancel-and-relaunch.
  //
  // A job that FINISHED without a confirmed sign-in needs the same treatment: the
  // gateway keeps its prompt alive on purpose (it clears job.signin only once sign-in
  // is confirmed), so restricting the fallback to in-progress jobs left the surviving
  // code unreachable — the crew is registered but never signed in, and the one screen
  // that could fix it renders nothing.
  //
  // Falling back to the newest persisted job (launches[0] — the API returns them
  // created-at descending) rather than only jobs that still carry a prompt: a launch
  // that FAILED or was reaped on restart has no signin, so gating on `!!j.signin`
  // hid its failure card after a reload — including the "check your crews, it may
  // still be running" warning for a stack that could still be billing.
  const effectiveLaunchId = activeLaunchId ?? inProgress[0]?.id ?? launches[0]?.id ?? null

  const launchStatusQuery = useQuery({
    queryKey: ['cloud', 'launch', effectiveLaunchId],
    queryFn: () => api.cloudLaunchStatus(effectiveLaunchId as string),
    enabled: !!effectiveLaunchId,
    refetchInterval: q => {
      const s = (q.state.data as LaunchJob | undefined)?.status
      return s && launchIsInFlight(s) ? 3000 : false
    },
  })

  // Which provisioners this gateway offers, and which of them this frontend can
  // draw. The stock build gets exactly one row (`aws_ec2`, drawn by the cards
  // below), so the selector never appears and this tab is unchanged. Fetched only
  // on the setup tab, because that is the only place it decides anything.
  const provisionersQuery = useQuery({
    queryKey: ['cloud', 'provisioners'],
    queryFn: () => api.cloudProvisioners(),
    enabled: tab === 'setup' && !disabled,
  })

  // Built-in first, then the edition's own in the order the server sent them: the
  // core-drawn launcher is the one every deployment has, so it is the default a
  // user who has never chosen lands on.
  const provisioners = useMemo(() => {
    const rows = provisionersQuery.data?.provisioners ?? []
    const drawable = rows.filter(p => canRenderRemoteProvisionerKind(p.kind))
    const builtin = (BUILTIN_REMOTE_PROVISIONER_KINDS as readonly string[])
    return [...drawable.filter(p => builtin.includes(p.kind)), ...drawable.filter(p => !builtin.includes(p.kind))]
  }, [provisionersQuery.data])

  // A remembered id the gateway no longer offers must not leave the tab drawing
  // nothing: fall back to the first renderable row, exactly as an unset choice
  // does. `null` while the list is unknown (loading or failed), which is what
  // keeps the built-in form the answer in those cases — the stock build never
  // depends on this query succeeding.
  const selectedProvisioner =
    provisioners.find(p => p.id === persistedProvisioner) ?? provisioners[0] ?? null
  // The user chose a lane the gateway has since stopped offering. Surfaced as a
  // line above the form rather than swallowed: the fallback puts them in front
  // of a different form, and a different bill, than the one they picked.
  const staleChoice =
    persistedProvisioner !== '' && provisioners.length > 0 && selectedProvisioner?.id !== persistedProvisioner
  // The gateway answered and none of its lanes is one this frontend can draw
  // (an edition withdrew the built-in and this frontend predates its renderer).
  // Drawing the EC2 cards here would offer a Launch that the server refuses
  // with `unknown_provisioner`; a notice says why there is nothing to launch.
  const noDrawableLane = provisionersQuery.isSuccess && provisioners.length === 0
  // The fallback for an UNKNOWN list is the built-in form, so a failed or
  // still-loading query renders the EC2 cards rather than an empty tab.
  const builtinProvisioner =
    selectedProvisioner === null
    || (BUILTIN_REMOTE_PROVISIONER_KINDS as readonly string[]).includes(selectedProvisioner.kind)
  const registeredProvisioner = builtinProvisioner || selectedProvisioner === null
    ? undefined
    : getRemoteProvisionerRenderer(selectedProvisioner.kind)

  const preflightQuery = useQuery({
    queryKey: ['cloud', 'preflight', checkedProfile, checkedRegion],
    queryFn: () => api.cloudPreflight(checkedProfile || undefined, checkedRegion || undefined),
    // A user with no remembered lane is on the built-in form whatever the list
    // says, so the probe fires at once, as it always did. Only a remembered
    // choice waits for the list: it may resolve to a lane whose form must not
    // trigger an AWS probe at all.
    enabled: tab === 'setup' && !disabled && !noDrawableLane
      && (persistedProvisioner === '' || (builtinProvisioner && !provisionersQuery.isLoading)),
  })

  const instances = useMemo(() => instancesQuery.data?.instances ?? [], [instancesQuery.data])
  const warmCap = instancesQuery.data?.warm_set_cap || WARM_SET_CAP_AUTO_CEILING

  // A draft outlives its form ON PURPOSE, which means it can also outlive the CREW
  // it belongs to: Remove a crew mid-edit and the draft stays keyed by that id, so
  // adding a crew that lands on the same id (ids are derived from the name) would
  // remount the stale draft on a different machine and let Save overwrite settings
  // the user never typed. Anchored to the crew's EXISTENCE rather than to the
  // remove button, so a removal from the CLI, or a cloud Delete, clears it too.
  // Gated on a successful fetch: an errored poll must not be read as "all gone"
  // and throw away unsaved work.
  // Which of the draft's own fields no longer match the crew as it is PERSISTED.
  // The id staying alive is not proof the record did: a crew removed and recreated
  // under the same derived id between two polls never disappears from the list, and
  // a concurrent CLI edit moves the record without touching its id. Both make the
  // draft's baseline a description of something that no longer exists, so the form
  // is told and refuses to save until the user adopts the current record.
  const editExternallyChanged = useMemo(() => {
    if (editDraft === null) return []
    const live = instances.find(i => i.id === editDraft.id)
    if (live === undefined) return []
    const now = instanceFormFromView(live)
    const then = instanceFormFromView(editDraft.draft.baseline)
    // Only the fields that ADDRESS a machine. A label or lifetime someone changed
    // elsewhere cannot make this a different crew, and the baseline diff already
    // stops the save from reverting it — interrupting for that would spend the
    // user's attention on the case that was never dangerous.
    const identifying = ['method', 'sshHost', 'remotePort', 'ssmTarget', 'awsProfile', 'awsRegion'] as const
    return identifying.filter(k => now[k] !== then[k])
  }, [editDraft, instances])

  useEffect(() => {
    if (!instancesQuery.isSuccess) return
    const live = new Set(instances.map(i => i.id))
    if (editingId !== null && !live.has(editingId)) setEditingId(null)
    if (editDraft !== null && !live.has(editDraft.id)) dispatch(setCrewEditForm(null))
    setEditBlockedId(prev => (prev !== null && !live.has(prev) ? null : prev))
  }, [instances, instancesQuery.isSuccess, editingId, editDraft, dispatch])

  // Re-open the row whose edit is still held: a draft nobody re-mounts is the same
  // loss with an extra step. Runs whenever an unsaved edit exists with no form open
  // — arriving back from the hand-off, and equally after the setup tab unmounted the
  // list. No race with the user's own Cancel, and no guard for one: Cancel drops the
  // held values, so there is nothing left for this to re-open. That is the whole
  // benefit of one source of truth over a stored copy plus component state.
  useEffect(() => {
    if (!instancesQuery.isSuccess || editingId !== null || editDraft === null) return
    if (!instances.some(i => i.id === editDraft.id)) return
    setEditingId(editDraft.id)
  }, [instances, instancesQuery.isSuccess, editingId, editDraft])

  // instance_id → cloud tag, from every EC2 launch job that produced an instance.
  // An SSM instance whose target matches is a cloud crew, and this is its tag.
  // Built-in lane only: the Stop/Start/Delete this map unlocks call the EC2
  // routes, and a job from another provisioner names a resource those routes
  // cannot reach (a lane that needs lifecycle controls contributes its own).
  // A job with no `provider_id` at all can only come from a gateway older than
  // the field (a dev-server build against one); every such job WAS an EC2
  // launch, and dropping it here would let Remove unregister a crew whose stack
  // keeps billing.
  const cloudTagByInstanceId = useMemo(() => {
    const m = new Map<string, string>()
    for (const j of launches) {
      if (j.instance_id && (j.provider_id ?? BUILTIN_PROVISIONER_ID) === BUILTIN_PROVISIONER_ID) m.set(j.instance_id, j.tag)
    }
    return m
  }, [launches])
  // instance_id → its launch job while the Kiro sign-in is missing or being
  // redone. Newest job per instance wins (the list is created-at descending), so
  // a crew re-signed by a later retry drops out once that retry confirms. The
  // polled copy of the active job outranks the list's snapshot of it: the list
  // is refetched on demand, the poll every few seconds, and the row must show the
  // code the moment the gateway publishes it.
  const signinJobByInstanceId = useMemo(() => {
    const m = new Map<string, LaunchJob>()
    for (const j of launches) {
      if (!j.instance_id || m.has(j.instance_id)) continue
      const live = launchStatusQuery.data?.id === j.id ? launchStatusQuery.data : j
      if (needsSignin(live) || (!isTerminal(live) && isRegistered(live))) m.set(j.instance_id, live)
    }
    return m
  }, [launches, launchStatusQuery.data])

  const reloadInstances = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ['instances'] })
  }, [queryClient])
  const reloadLaunches = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: ['cloud', 'launches'] })
  }, [queryClient])

  // The other half of the announce. When the gateway showing this page refuses a
  // crew we announced, only IT holds the reason -- the depth cap and the cycle
  // guard are its decisions, taken against a registry this pane never sees -- so
  // without this the crew connects here and silently never appears up there. The
  // listener and the sender check live in `chainAnnounce.ts`, which catches the
  // answer whether or not this panel is open; `chainRefusal` above reads it.

  const connectMutation = useMutation({
    mutationFn: (id: string) => api.connectInstance(id),
    onMutate: () => { setActionErr(null); setDiagNote(null) },
    onSuccess: (st, id) => {
      if (st.state !== 'connected') { setActionErr(st.error || i18nT('pages.settings.instancesPanel.connection_did_not_complete_try_diagnose_for_det')); return }
      // Inside a pane, this gateway is itself a crew of the one showing the page,
      // so a crew connected here is reachable from up there only through our hop —
      // and the host never sees our registry. Tell it. A no-op at top level. The
      // notice carries no credential: the host mints its own over the credential
      // it already holds for us.
      const inst = instancesQuery.data?.instances?.find(i => i.id === id)
      // `hasDashboardPane` and not an inline transport test: it is the existing predicate
      // for this exact question and its own docstring gives the reason -- a fargate crew
      // "exposes a turn API on its forwarded port and nothing else: no dashboard, no
      // token", so it gets no switcher tab, no pane and no auto-connect. Every other
      // consumer already asks it; this announce was the one that did not, and a fargate
      // connect DOES supply `local_port`, so the port check alone let one through. The
      // host then persists the row before it ever tries to connect it, and its refusal
      // relay only fires when the gateway raises -- so the row survived as a crew
      // promising a tab that nothing can serve, removable only by hand.
      // `!inst.via_instance_id` for the depth cap, and it has to be asked HERE: the
      // host counts hops in its own registry, where our own hop to this crew is
      // invisible, so its `_chain_refusal` accepts the add and commits the row. Only
      // our gateway can see the extra hop, and it does -- `mint_embed_token` answers
      // `chain_too_deep` for exactly this row -- but by then the host has persisted
      // it, and the refusal relay leaves the row behind. The same field the backend
      // refuses on, so the two ends agree on what "one hop too far" means.
      if (inst && st.local_port && hasDashboardPane(inst) && !inst.via_instance_id) {
        announceChainedCrew({
          id,
          name: inst.name,
          sshHost: inst.connection_method === 'ssh' ? inst.ssh_host : inst.ssm_target,
          remotePort: inst.remote_port,
          port: st.local_port,
        })
      }
    },
    onError: (e, id) => {
      // The flag was turned on after the gateway started, so no tunnel manager
      // exists until a restart. The not-active banner already says how; the error
      // points at it, keyed on the gateway's code, instead of quoting its message.
      setActionErr(e instanceof ApiError && parseErrorCode(e.body) === 'instances_manager_unavailable'
        ? i18nT('pages.settings.instancesPanel.connect_needs_restart', { id })
        : i18nT('pages.settings.instancesPanel.connect_failed', { id, error: errMsg(e, i18nT('pages.settings.instancesPanel.unknown_error')) }))
    },
    onSettled: reloadInstances,
  })
  const disconnectMutation = useMutation({
    mutationFn: (id: string) => api.disconnectInstance(id),
    onMutate: () => setActionErr(null),
    onSuccess: (_r, id) => dispatch(removeWarm(id)),
    onError: (e, id) => setActionErr(i18nT('pages.settings.instancesPanel.disconnect_failed', { id, error: errMsg(e, i18nT('pages.settings.instancesPanel.unknown_error')) })),
    onSettled: reloadInstances,
  })
  const crewToggleMutation = useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) => setCrewEnabled(dispatch, id, enabled),
    onMutate: () => setActionErr(null),
    onError: (e, { id }) => setActionErr(i18nT('pages.settings.remoteCrewPanel.crew_toggle_failed', {
      name: instancesQuery.data?.instances?.find(i => i.id === id)?.name || id,
      error: errMsg(e, i18nT('pages.settings.instancesPanel.unknown_error')),
    })),
    onSettled: reloadInstances,
  })
  const removeMutation = useMutation({
    mutationFn: async (id: string) => { await api.disconnectInstance(id).catch(() => {}); await api.removeInstance(id) },
    onMutate: () => setActionErr(null),
    onSuccess: (_r, id) => dispatch(removeWarm(id)),
    onError: (e, id) => setActionErr(i18nT('pages.settings.instancesPanel.remove_failed', { id, error: errMsg(e, i18nT('pages.settings.instancesPanel.unknown_error')) })),
    onSettled: reloadInstances,
  })
  const diagnoseMutation = useMutation({
    mutationFn: (id: string) => api.instanceStatus(id, true),
    onMutate: () => { setActionErr(null); setDiagNote(null); setDiagReport(null) },
    onSuccess: (st, id) => {
      const code = st.diagnosis?.code
      // Two verdicts are BENIGN: `ok`, and `not_connected` — which is `ok: false`
      // on the wire but whose reason is guidance ("click Connect"), not a failure.
      // Neither may label an error surface or reach the failure report.
      const benign = code === 'ok' || code === 'not_connected'
      const failing = st.diagnosis && !benign ? st.diagnosis : undefined
      // Displayed text, most specific first: a FAILING ladder verdict names the
      // broken link, so it wins; otherwise the tunnel's live `status.error`; and
      // only then a benign verdict's own reason. The ladder result is the last
      // RUN, so a stale "All checks passed" / "click Connect" must never label a
      // red notice whose real cause is the live error.
      const reason = failing?.reason || st.error || st.diagnosis?.reason
      // A benign verdict is only benign while the tunnel has no error of its own.
      const kind: 'ok' | 'info' | 'warn' =
        st.error || failing ? 'warn' : code === 'ok' ? 'ok' : code === 'not_connected' ? 'info' : 'warn'
      if (reason) setDiagNote({ kind, text: `${id}: ${reason}` })
      // Journal unconditionally, healthy verdict included: the recorder's
      // no-failure path is what clears its de-dup signature, so skipping the call
      // on a healthy diagnose would leave the signature standing and suppress the
      // next identical failure. It returns null when there is nothing to describe.
      // A benign verdict is stripped from the status handed over: the recorder
      // treats any not-ok verdict as a failure, so `not_connected` would otherwise
      // be journaled as a system error (the #11110 defect by another path), and a
      // stale benign verdict beside a live error would decorate that error's
      // report with a probe chain that says nothing is wrong.
      const inst = instances.find(i => i.id === id)
      const { diagnosis: _omitted, ...withoutDiagnosis } = st
      setDiagReport(reportInstanceFailure({
        id,
        name: inst?.name || id,
        transport: inst && usesSsmTransport(inst) ? 'ssm' : 'ssh',
        status: benign ? withoutDiagnosis : st,
        stage: 'connect',
        fallbackMessage: kind === 'warn' ? reason || '' : '',
      }))
    },
    onError: (e, id) => setActionErr(i18nT('pages.settings.instancesPanel.diagnose_failed', { id, error: errMsg(e, i18nT('pages.settings.instancesPanel.unknown_error')) })),
    onSettled: reloadInstances,
  })
  const cancelMutation = useMutation({
    mutationFn: (id: string) => api.cloudLaunchCancel(id),
    onMutate: () => setActionErr(null),
    onError: e => setActionErr(errMsg(e, i18nT('pages.settings.instancesPanel.unknown_error'))),
    onSettled: reloadLaunches,
  })
  const stopMutation = useMutation({
    mutationFn: (v: { tag: string; coords: CloudCoords }) => api.cloudStop(v.tag, v.coords),
    onMutate: () => setActionErr(null),
    onError: e => setActionErr(errMsg(e, i18nT('pages.settings.instancesPanel.unknown_error'))),
    onSettled: () => { reloadInstances(); reloadLaunches() },
  })
  const startMutation = useMutation({
    mutationFn: (v: { tag: string; coords: CloudCoords }) => api.cloudStart(v.tag, v.coords),
    onMutate: () => setActionErr(null),
    onError: e => setActionErr(errMsg(e, i18nT('pages.settings.instancesPanel.unknown_error'))),
    onSettled: () => { reloadInstances(); reloadLaunches() },
  })
  const deleteMutation = useMutation({
    mutationFn: (v: { tag: string; coords: CloudCoords }) => api.cloudDestroy(v.tag, v.coords),
    onMutate: () => { setActionErr(null); setConfirmDeleteTag(null) },
    // The request only *starts* the teardown (the gateway returns cleanup: "pending");
    // remember the tag so its row shows "Deleting…" and the list polls until the
    // background watcher drops the row once AWS confirms.
    onSuccess: (_r, v) => setDeletingTags(prev => new Set(prev).add(v.tag)),
    onError: e => setActionErr(errMsg(e, i18nT('pages.settings.instancesPanel.unknown_error'))),
    onSettled: () => { reloadInstances(); reloadLaunches() },
  })
  // Fetches the pending device-code prompt for a job that is already awaiting
  // sign-in. Distinct from the restart below: this asks for the code that exists,
  // that one asks for a new one. Keyed by the job the click named, not by
  // `activeLaunchId` — the crew row offers this too, and there the job being
  // fetched may not be the active launch at all.
  // The job id a row's sign-in buttons act on, or null when it has none. Used to
  // scope the shared mutations' pending state to the row that was clicked.
  const signinJobIdFor = (inst: InstanceView): string | null =>
    inst.connection_method === 'ssm' && inst.ssm_target
      ? signinJobByInstanceId.get(inst.ssm_target)?.id ?? null
      : null
  const signinMutation = useMutation({
    mutationFn: (id: string) => api.cloudLaunchSignin(id),
    onMutate: () => { setActionErr(null); setSigninNotice(null) },
    onError: (e, id) => {
      // "I approved it -- check now" SUCCEEDS as HTTP 409 `signin_already_complete`:
      // the gateway re-probed the box, found the sign-in, and there is no prompt
      // left to return. That is the outcome the user clicked for, not an error.
      // Painting it red told a signed-in user their sign-in had failed.
      const code = e instanceof ApiError && e.status === 409 ? parseErrorCode(e.body) : null
      if (code === 'signin_already_complete') {
        reloadInstances()
        reloadLaunches()
        return
      }
      // The other 409: the box was re-probed and the approval has NOT landed
      // (clicked early, or not yet propagated). The ordinary recheck outcome,
      // not a failure -- and a re-render of the same screen is not an answer, so
      // the block says "not signed in yet" beside the button.
      if (code === 'no_signin_pending') {
        setSigninNotice({ jobId: id, kind: 'not_signed_in_yet' })
        reloadLaunches()
        return
      }
      // Anything else: say so next to the button that was clicked, not only in
      // the panel banner above the size cards.
      setSigninNotice({ jobId: id, kind: 'request_failed', detail: errMsg(e, i18nT('pages.settings.instancesPanel.unknown_error')) })
    },
    onSettled: (_r, _e, id) => { void queryClient.invalidateQueries({ queryKey: ['cloud', 'launch', id] }) },
  })
  // Starts the Kiro sign-in again on a crew that ended up without one. The job
  // becomes the active launch so its card and its row poll the new code live.
  const signinRestartMutation = useMutation({
    mutationFn: (id: string) => api.cloudLaunchSigninRestart(id),
    onMutate: () => { setActionErr(null); setSigninNotice(null) },
    onSuccess: job => { setActiveLaunchId(job.id); reloadLaunches() },
    onError: e => {
      // The route refuses a restart on a crew that is ALREADY signed in with 409
      // `signin_already_complete`. That is the good outcome -- another tab, or the
      // re-probe, confirmed the sign-in -- and painting it red told the reader
      // their crew had failed to sign in when it had just succeeded. Reload so the
      // badge and Connect catch up, and say nothing.
      if (e instanceof ApiError && e.status === 409 && parseErrorCode(e.body) === 'signin_already_complete') {
        reloadInstances()
        reloadLaunches()
        return
      }
      setActionErr(errMsg(e, i18nT('pages.settings.instancesPanel.unknown_error')))
    },
    onSettled: (_r, _e, id) => { void queryClient.invalidateQueries({ queryKey: ['cloud', 'launch', id] }) },
  })
  // Takes its body as VARIABLES rather than closing over the form state: a
  // registered provisioner's form owns its own inputs, and the core cannot read
  // them. The built-in call site passes the panel's own profile/region/size.
  const launchMutation = useMutation({
    mutationFn: (body: { provider_id?: string; profile: string; region: string; size_key: string; subnet_id?: string }) =>
      api.cloudLaunch(body),
    onMutate: () => setActionErr(null),
    onSuccess: job => {
      setActiveLaunchId(job.id)
      reloadLaunches()
      // The built-in form's draft has been used. Only the built-in Launch sets
      // the ref, so a registered provisioner's launch leaves the draft alone.
      if (sentLaunchDraft.current) dispatch(dropLaunchedCrewDraft(sentLaunchDraft.current))
    },
    onError: e => setActionErr(errMsg(e, i18nT('pages.settings.instancesPanel.unknown_error'))),
    onSettled: () => { sentLaunchDraft.current = null },
  })
  const enableMutation = useMutation({
    mutationFn: () => api.patchConfig('instances.enabled', true),
    onSuccess: () => { setRestartPending(true); reloadInstances() },
    onError: e => setActionErr(errMsg(e, i18nT('pages.settings.instancesPanel.unknown_error'))),
  })

  const busy = connectMutation.isPending
    ? `connect:${connectMutation.variables}`
    : diagnoseMutation.isPending
      ? `diagnose:${diagnoseMutation.variables}`
      : stopMutation.isPending
        // These two take {tag, coords}, so interpolating `variables` directly yielded
        // "stop:[object Object]" — a key no row could ever match, leaving the button
        // label stuck on "Stop" for the whole request. (The row still disabled, since
        // that only tests `!!busy`, which is why this stayed invisible.)
        ? `stop:${stopMutation.variables?.tag}`
        : startMutation.isPending
          ? `start:${startMutation.variables?.tag}`
          : disconnectMutation.isPending || removeMutation.isPending || deleteMutation.isPending || crewToggleMutation.isPending
            ? 'busy'
            : ''

  const runCheck = useCallback(() => {
    setCheckedProfile(profile)
    setCheckedRegion(region)
    void queryClient.invalidateQueries({ queryKey: ['cloud', 'preflight'] })
  }, [profile, region, queryClient])

  // Both copies branch on the boolean `copyToClipboard` returns: a denied
  // clipboard write used to paint "Copied" regardless, which is a false
  // confirmation on exactly the command the user is about to need. Each failure
  // is keyed to its own button so it renders beside it (the checklist sits well
  // below the page-level notices), and the two messages differ because the
  // command IS on screen to select by hand while the policy JSON never is.
  const copyCommand = useCallback(async (command: string) => {
    setCopyErr(null)
    // try/catch as well as the boolean: the helper resolves `false` when the
    // `execCommand` fallback reports failure, but REJECTS when that fallback
    // throws, and this callback is fire-and-forget at its call site — an
    // unhandled rejection would be a copy that failed with no notice.
    let ok = false
    try {
      ok = await copyToClipboard(command)
    } catch {
      ok = false
    }
    if (!ok) {
      setCopyErr({ target: 'command', message: i18nT('pages.settings.remoteCrewPanel.copy_failed') })
      return
    }
    setCopied('command')
    setTimeout(() => setCopied(null), 1500)
  }, [])
  const copyPolicy = useCallback(async () => {
    setCopyErr(null)
    try {
      const { policy } = await api.cloudIamPolicy()
      if (!(await copyToClipboard(policy))) {
        setCopyErr({ target: 'policy', message: i18nT('pages.settings.remoteCrewPanel.copy_policy_failed') })
        return
      }
      setCopied('policy')
      setTimeout(() => setCopied(null), 1500)
    } catch (e) {
      setCopyErr({ target: 'policy', message: errMsg(e, i18nT('pages.settings.instancesPanel.unknown_error')) })
    }
  }, [errMsg])

  const preflight: CloudPreflight | undefined = preflightQuery.data
  // Found but refused is not "not installed": the install command would put the
  // same refused file back, so the plugin row says why it was refused instead.
  const pluginRefusal = preflight ? pluginRefusalCopy(preflight) : null
  // The same for an `aws` CLI the gateway found and refused: the credentials row
  // says why, instead of the CLI's "not found" error and its install link.
  const cliRefusal = preflight ? awsCliRefusalCopy(preflight) : null
  const blockingOk = !!preflight
    && preflight.reachable
    && !!preflight.account
    && preflight.ec2_reachable
    && preflight.cloudformation_reachable
    && preflight.ssm_reachable
    && preflight.session_manager_plugin

  // Prefer the polled detail, but fall back to the list's copy so the card (and its
  // device code) is present on the very first render after a remount, before the
  // status query has resolved.
  const activeJob = launchStatusQuery.data ?? inProgress[0] ?? null

  // A launch that reaches `done` has just added a crew, but nothing else invalidates
  // the instances cache and switching tabs does not remount this component — so "Your
  // crews" would keep showing the pre-launch list until an unrelated refetch happened.
  // Keyed by job id so this fires once per launch instead of on every poll.
  const reconciledLaunch = useRef<string | null>(null)
  useEffect(() => {
    if (!activeJob || !isTerminal(activeJob)) return
    if (reconciledLaunch.current === activeJob.id) return
    reconciledLaunch.current = activeJob.id
    reloadInstances()
  }, [activeJob, reloadInstances])

  // Once a teardown finishes the gateway drops the instance, so its row (and its
  // "Deleting…" state) vanishes on the next poll. Prune the tag then, which also
  // stops the poll once nothing is deleting. Keyed off the instance list so it fires
  // exactly when a row disappears rather than on a timer.
  useEffect(() => {
    if (deletingTags.size === 0) return
    const liveTags = new Set(
      instances
        .map(i => (i.ssm_target ? cloudTagByInstanceId.get(i.ssm_target) : undefined))
        .filter((t): t is string => !!t),
    )
    const next = new Set([...deletingTags].filter(t => liveTags.has(t)))
    if (next.size !== deletingTags.size) setDeletingTags(next)
  }, [instances, cloudTagByInstanceId, deletingTags])

  // ── Initial load: don't render the full UI until we know whether the
  //    feature is enabled. Without this the panel flashes the tabbed form
  //    and then jitters to the "off" card once the 403 arrives. ──
  if (instancesQuery.isLoading) {
    return (
      <Card>
        <div className="flex items-center gap-2 text-muted text-sm py-2">
          <RefreshCw className="lucide-inline animate-spin" /> {i18nT('pages.settings.instancesPanel.loading')}
        </div>
      </Card>
    )
  }

  // ── Disabled feature gate (mirrors InstancesPanel) ──
  if (disabled) {
    return (
      <Card>
        <div className="flex items-center gap-2 text-text font-medium mb-1" data-setting-label={i18nT('pages.settings.instancesPanel.enable_remote_crew_management')}>
          <Server className="lucide-inline" /> {i18nT('pages.settings.instancesPanel.multi_instance_management_is_off')}
        </div>
        <p className="text-[13px] text-muted mb-3">{i18nT('pages.settings.instancesPanel.enable_it_to_let_this_gateway_open_ssh_tunnels_t')}</p>
        {restartPending && (
          <div role="status" className="flex items-start gap-2 px-3 py-2 mb-3 text-[13px] rounded-md bg-warn/10 text-warn-text border border-warn/30">
            <AlertTriangle size={14} className="lucide-inline mt-0.5 shrink-0" />
            <span>{i18nT('pages.settings.instancesPanel.disabled_in_config_restart_the_gateway')}<code className="text-text">{i18nT('pages.settings.instancesPanel.kirocrew_restart')}</code>) {i18nT('pages.settings.instancesPanel.to_fully_tear_down_any_tunnels_still_running_fro')}</span>
          </div>
        )}
        <Btn primary onClick={() => enableMutation.mutate()} disabled={enableMutation.isPending}>
          <Power className="lucide-inline" /> {enableMutation.isPending ? i18nT('pages.settings.instancesPanel.enabling') : i18nT('pages.settings.instancesPanel.enable_remote_crew_management')}
        </Btn>
        <ErrorNotice message={actionErr} askAgent className="mt-2" />
      </Card>
    )
  }

  const Tabs = (
    <div className="flex gap-1 border-b border-border mb-5">
      <button
        type="button"
        onClick={() => setTab('crews')}
        aria-label={i18nT('pages.settings.remoteCrewPanel.your_crews')}
        className={`px-3.5 py-2 text-[13px] font-semibold -mb-px border-b-2 transition-colors flex items-center gap-2 ${tab === 'crews' ? 'text-text-strong border-accent' : 'text-muted border-transparent hover:text-text'}`}
      >
        {i18nT('pages.settings.remoteCrewPanel.your_crews')}
        <span className={`text-[11px] px-1.5 rounded-full ${tab === 'crews' ? 'bg-accent-subtle text-accent' : 'bg-bg-hover text-muted'}`}>{instances.length}</span>
      </button>
      <button
        type="button"
        onClick={() => setTab('setup')}
        aria-label={i18nT('pages.settings.remoteCrewPanel.set_up_a_new_one')}
        className={`px-3.5 py-2 text-[13px] font-semibold -mb-px border-b-2 transition-colors flex items-center gap-2 ${tab === 'setup' ? 'text-text-strong border-accent' : 'text-muted border-transparent hover:text-text'}`}
      >
        <Rocket size={14} /> {i18nT('pages.settings.remoteCrewPanel.set_up_a_new_one')}
        {inProgress.length > 0 && <span className="w-1.5 h-1.5 rounded-full bg-accent" aria-hidden />}
      </button>
    </div>
  )

  const Notices = (
    <>
      {/* askAgent ON on both notices: every unsaved input this panel holds
          outlives the navigation — the add-crew and edit-crew forms and the
          launch form's unlaunched subnet and sign-in choice live in the store
          (setCrewAddForm / setCrewEditForm / setCrewLaunchDraft), its size and
          account in localStorage — so the hand-off destroys nothing, and every
          message here (a refused connect, a failed diagnose, a rejected launch)
          is a gateway-side failure the agent can look into. */}
      {actionErr && <ErrorNotice message={actionErr} onDismiss={() => setActionErr(null)} className="mb-3" askAgent />}
      {chainRefusal && (
        // askAgent on: the reason names a limit of the arrangement (too deep, a
        // loop) rather than something to retype here, so the next step is a
        // conversation about the topology, not another press of Connect.
        <ErrorNotice
          message={chainRefusal}
          onDismiss={clearChainRefusal}
          className="mb-3"
          askAgent
          testId="remote-crew-chain-refused"
        />
      )}
      {/* A `warn` diagnosis names the broken link (`diagnosis.reason`, or the
          tunnel's own `status.error`), so it is an error surface. The structured
          `report` is passed when the journal produced one, so the hand-off carries
          the transport and stage rather than a message match. `ok` / `info`
          describe a state that has not gone wrong and stay a status note — a
          healthy "All checks passed" must not paint red or offer an agent hand-off. */}
      {diagNote?.kind === 'warn' && (
        <ErrorNotice
          message={diagNote.text}
          report={diagReport ?? undefined}
          askAgent
          onDismiss={() => { setDiagNote(null); setDiagReport(null) }}
          className="mb-3"
          testId="remote-crew-diagnosis"
        />
      )}
      {diagNote && diagNote.kind !== 'warn' && (
        <div
          role="status"
          data-testid="remote-crew-diagnosis-status"
          className={
            'mb-3 flex items-start gap-2 px-3 py-2 text-[13px] rounded-md border ' +
            (diagNote.kind === 'ok'
              ? 'bg-ok/10 text-ok-text border-ok/30'
              : 'bg-accent/10 text-accent border-accent/30')
          }
        >
          <Stethoscope size={14} className="lucide-inline mt-0.5 shrink-0" />
          <span className="flex-1 break-words">{diagNote.text}</span>
          <button
            type="button"
            aria-label={i18nT('pages.settings.instancesPanel.dismiss_diagnosis')}
            className="shrink-0 opacity-70 hover:opacity-100"
            onClick={() => { setDiagNote(null); setDiagReport(null) }}
          >
            <X size={12} />
          </button>
        </div>
      )}
    </>
  )

  return (
    <div>
      {Tabs}
      {Notices}

      {tab === 'crews' ? (
        <div className="space-y-4">
          <Card>
            <SettingsToggle
              label={i18nT('pages.settings.remoteCrewPanel.auto_connect')}
              hint={i18nT('pages.settings.remoteCrewPanel.auto_connect_desc')}
              checked={autoConnect}
              onChange={setAutoConnect}
            />
          </Card>
          <Card>
            <div className="flex items-center justify-between mb-1">
              <div className="flex items-center gap-2 text-text font-medium">
                <Server className="lucide-inline" /> {i18nT('pages.settings.remoteCrewPanel.crews_you_can_switch_to')}
              </div>
              <div className="flex items-center gap-2">
                <span className="text-[12px] text-muted">{i18nT('pages.settings.remoteCrewPanel.up_to_warm', { n: warmCap })}</span>
                <Btn onClick={reloadInstances} aria-label={i18nT('pages.settings.instancesPanel.refresh')}><RefreshCw className="lucide-inline" /></Btn>
              </div>
            </div>
            <p id={CREW_SWITCH_HINT_ID} className="text-[12px] text-muted mb-1">
              {i18nT('pages.settings.remoteCrewPanel.crew_switch_hint')}
            </p>
            {needsRestart && (
              <div id={CREW_NEEDS_RESTART_BANNER_ID} role="status" className="flex items-start gap-2 px-3 py-2 mb-3 text-[13px] rounded-md bg-warn/10 text-warn-text border border-warn/30">
                <AlertTriangle size={14} className="lucide-inline mt-0.5 shrink-0" />
                <RestartToActivateText />
              </div>
            )}
            {listLoading ? (
              <div className="flex items-center gap-2 text-muted text-sm py-2">
                <RefreshCw className="lucide-inline animate-spin" /> {i18nT('pages.settings.instancesPanel.loading')}
              </div>
            ) : loadError ? (
              // Never fall through to the empty state on a failed load — that
              // reads as "your crews are gone" when the list simply did not load.
              <div className="py-1">
                {/* No hand-off: the add-crew form shares this tab — a failed
                    list refresh must not offer a navigation that discards it. */}
                <ErrorNotice message={errMsg(instancesQuery.error ?? launchesQuery.error, i18nT('pages.settings.instancesPanel.unknown_error'))} />
                {/* Refresh replays the same rejected credential, so it can only
                    reproduce the error until the user re-authenticates through
                    the banner the notice points at. */}
                {!authExpired && (
                  <Btn className="mt-2" onClick={() => { reloadInstances(); reloadLaunches() }}>
                    <RefreshCw className="lucide-inline" /> {i18nT('pages.settings.instancesPanel.refresh')}
                  </Btn>
                )}
              </div>
            ) : (
              <div>
                {/* A sign-in retry is also in progress, but its crew is already a
                    row below (the connect step ran); a second "Setting up" row
                    would read as a second instance being created — and billed. */}
                {inProgress.filter(job => !isRegistered(job)).map(job => (
                  <SettingUpRow key={job.id} job={job} cancelling={cancelMutation.isPending && cancelMutation.variables === job.id} onCancel={id => cancelMutation.mutate(id)} />
                ))}
                <CrewVersionsReadError anyConnected={instances.some(i => !i.disabled && i.status.state === 'connected')} />
                <DefaultDestinationCrewPicker instances={instances} value={instancesQuery.data?.default_destination ?? ''} />
                {instances.map(inst => (
                  <CrewRow
                    key={inst.id}
                    inst={inst}
                    cloudTag={inst.connection_method === 'ssm' && inst.ssm_target ? cloudTagByInstanceId.get(inst.ssm_target) ?? null : null}
                    signinJob={inst.connection_method === 'ssm' && inst.ssm_target ? signinJobByInstanceId.get(inst.ssm_target) ?? null : null}
                    onRestartSignin={id => signinRestartMutation.mutate(id)}
                    // Keyed to THIS row's job: the mutation is one object shared by
                    // every row, so its bare `isPending` would disable the sign-in
                    // buttons on every other unsigned instance the moment one is
                    // clicked -- reading as if the whole panel were busy.
                    restartingSignin={signinRestartMutation.isPending && signinRestartMutation.variables === signinJobIdFor(inst)}
                    onFetchSignin={id => signinMutation.mutate(id)}
                    fetchingSignin={signinMutation.isPending && signinMutation.variables === signinJobIdFor(inst)}
                    signinNotice={signinNotice && signinNotice.jobId === signinJobIdFor(inst) ? signinNotice : null}
                    busy={busy}
                    connectBlocked={needsRestart}
                    deleting={inst.ssm_target ? deletingTags.has(cloudTagByInstanceId.get(inst.ssm_target) ?? '') : false}
                    confirmDelete={confirmDeleteTag !== null && confirmDeleteTag === (inst.ssm_target ? cloudTagByInstanceId.get(inst.ssm_target) : null)}
                    confirmRemove={confirmRemoveId === inst.id}
                    onConnect={id => connectMutation.mutate(id)}
                    onDisconnect={id => disconnectMutation.mutate(id)}
                    onSetEnabled={(id, enabled) => crewToggleMutation.mutate({ id, enabled })}
                    onDiagnose={id => diagnoseMutation.mutate(id)}
                    onRemove={id => removeMutation.mutate(id)}
                    onStop={(tag, coords) => stopMutation.mutate({ tag, coords })}
                    onStart={(tag, coords) => startMutation.mutate({ tag, coords })}
                    onDelete={(tag, coords) => deleteMutation.mutate({ tag, coords })}
                    onRequestDelete={tag => setConfirmDeleteTag(tag)}
                    onRequestRemove={id => setConfirmRemoveId(id)}
                    editing={editingId === inst.id}
                    blocked={editBlockedId === inst.id}
                    onEdit={id => {
                      // Switching rows would unmount another crew's draft.
                      if (
                        id !== null
                        && editDraft !== null
                        && id !== editDraft.id
                      ) {
                        setEditBlockedId(id)
                        return
                      }
                      setEditBlockedId(null)
                      // Cancel (id === null) is the user CHOOSING to discard; the draft
                      // goes with it. Every other way the form disappears keeps it.
                      if (id === null) dispatch(setCrewEditForm(null))
                      setEditingId(id)
                    }}
                    editDraft={editDraft?.id === inst.id ? editDraft.draft : null}
                    editExternallyChanged={editDraft?.id === inst.id ? editExternallyChanged : []}
                    // A three-way merge, with the old baseline as the merge base: the
                    // user's TYPED fields are kept, and every field they did not touch
                    // is taken from the record that actually exists. Keeping all the old
                    // values instead would turn untouched-but-stale fields into
                    // deliberate writes — the exact clobber the baseline exists to stop.
                    editDraftSeq={editDraft?.id === inst.id ? editDraft.seq : 0}
                    onEditRebase={() => {
                      if (editDraft === null || editDraft.id !== inst.id) return
                      const base = instanceFormFromView(editDraft.draft.baseline)
                      const live = instanceFormFromView(inst)
                      const merged = { ...live }
                      for (const k of Object.keys(base) as (keyof typeof base)[]) {
                        if (editDraft.draft.values[k] === base[k]) continue
                        // Field-wise assign: the value's type is the field's own, and
                        // a generic index write cannot see that.
                        Object.assign(merged, { [k]: editDraft.draft.values[k] })
                      }
                      dispatch(setCrewEditForm({
                        id: inst.id,
                        draft: { values: merged, baseline: inst },
                        seq: editDraft.seq + 1,
                      }))
                    }}
                    onEditDraftChange={draft => {
                      const next =
                        draft === null
                          ? null
                          : {
                              id: inst.id, draft,
                              seq: editDraft?.id === inst.id ? editDraft.seq : 0,
                            }
                      // Same values, same action: the report fires on every keystroke,
                      // and dispatching an equal-but-new object re-renders for nothing.
                      if (JSON.stringify(editDraft) === JSON.stringify(next)) return
                      if (next === null) setEditBlockedId(null)
                      dispatch(setCrewEditForm(next))
                    }}
                    // Clearing editingId without clearing the refusal left the UI
                    // instructing the user about a form that no longer exists.
                    onEditSaved={updated => {
                      setEditingId(null)
                      dispatch(setCrewEditForm(null))
                      setEditBlockedId(null)
                      // A warm pane is an iframe pointed at the OLD local port with the
                      // OLD token. If the save tore the tunnel down (any transport
                      // field changed), that pane cannot be revived by reconnecting —
                      // it would reuse a credential the new tunnel never issued and sit
                      // on 403. Drop it so the next Connect builds a fresh one. A
                      // name-or-ttl-only edit leaves the tunnel up, and its pane keeps
                      // working, so it is deliberately NOT dropped.
                      if (updated.status?.state !== 'connected') dispatch(removeWarm(inst.id))
                      reloadInstances()
                    }}
                  />
                ))}
                {inProgress.length === 0 && instances.length === 0 && (
                  <div className="text-[13px] text-muted py-1">{i18nT('pages.settings.remoteCrewPanel.no_crews')}</div>
                )}
              </div>
            )}
            {confirmDeleteTag !== null && <p className="mt-2 text-[12px] text-warn">{i18nT('pages.settings.remoteCrewPanel.delete_warning')}</p>}
            {confirmRemoveId !== null && <p className="mt-2 text-[12px] text-warn">{i18nT('pages.settings.remoteCrewPanel.remove_warning')}</p>}
          </Card>

          <AddInstanceForm onAdded={reloadInstances} />
        </div>
      ) : (
        <div className="space-y-4">
          {/* The lane list could not be read. The built-in form below still
              renders (the list is presentation, not permission), but the
              failure is said rather than swallowed: a newer dashboard on an
              older gateway is exactly the version skew the agent can explain.
              askAgent ON: the launch form keeps every entry the reader has not
              launched with, so the navigation destroys nothing. */}
          {provisionersQuery.isError && (
            <ErrorNotice
              message={errMsg(provisionersQuery.error, i18nT('pages.settings.remoteCrewPanel.provisioners_unavailable'))}
              askAgent
            />
          )}

          {/* Which provisioner. Rendered only when the gateway offers more than
              one this frontend can draw, so the stock build shows nothing here
              and goes straight to the AWS cards below. */}
          {provisioners.length > 1 && (
            <Card>
              <div className="text-text font-medium mb-3">{i18nT('pages.settings.remoteCrewPanel.provisioner_choose')}</div>
              <div className="space-y-2.5">
                {provisioners.map(p => (
                  <ProvisionerCard
                    key={p.id}
                    provisioner={p}
                    on={selectedProvisioner?.id === p.id}
                    onPick={setProvisionerId}
                  />
                ))}
              </div>
            </Card>
          )}

          {/* The remembered lane is gone; say so before showing a different form. */}
          {staleChoice && selectedProvisioner && (
            <p role="status" className="text-[12px] text-muted flex items-start gap-1.5">
              <AlertTriangle size={13} className="mt-0.5 shrink-0 text-warn" />
              {i18nT('pages.settings.remoteCrewPanel.provisioner_stale_choice', { label: selectedProvisioner.label })}
            </p>
          )}

          {noDrawableLane ? (
            <Card>
              <div className="text-[13px] text-muted">{i18nT('pages.settings.remoteCrewPanel.provisioner_none_drawable')}</div>
            </Card>
          ) : builtinProvisioner ? (
          <>
          {/* AWS prerequisites — the account inputs live HERE, above the rows they
              produce. The check runs against this profile/region, so showing the
              verdict first and the inputs in a later card inverted cause and effect:
              a red "credentials expired" row gave no hint that it had probed a
              different profile than the one the reader had in mind. These same two
              values are also what the launch below uses. */}
          <Card>
            <div className="flex items-center gap-2 mb-3 text-text font-medium">
              <CheckCircle className="lucide-inline" /> {i18nT('pages.settings.remoteCrewPanel.before_you_start')}
            </div>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 mb-3">
              <label htmlFor="cloud-profile" className="flex flex-col gap-1 text-[13px] text-muted">
                {i18nT('pages.settings.instancesPanel.aws_profile')}
                <input
                  id="cloud-profile"
                  aria-label={i18nT('pages.settings.instancesPanel.aws_profile')}
                  className="bg-bg-elevated border border-border rounded-md px-3 py-2 text-text text-sm outline-hidden focus-ring"
                  value={profile}
                  onChange={e => setProfile(e.target.value)}
                  onBlur={runCheck}
                  placeholder={i18nT('pages.settings.instancesPanel.default_credential_chain')}
                />
              </label>
              <label htmlFor="cloud-region" className="flex flex-col gap-1 text-[13px] text-muted">
                {i18nT('pages.settings.remoteCrewPanel.region')}
                <input
                  id="cloud-region"
                  aria-label={i18nT('pages.settings.remoteCrewPanel.region')}
                  className="bg-bg-elevated border border-border rounded-md px-3 py-2 text-text text-sm outline-hidden focus-ring"
                  value={region}
                  onChange={e => setRegion(e.target.value)}
                  onBlur={runCheck}
                  placeholder="us-east-1"
                />
              </label>
            </div>
            <p className="mb-3 text-[12px] text-muted">
              {i18nT('pages.settings.remoteCrewPanel.checking_against', {
                profile: checkedProfile || i18nT('pages.settings.remoteCrewPanel.no_profile_set'),
                region: checkedRegion,
              })}
            </p>
            {/* Rendered independently of `preflight`: a failed RE-check keeps the
                previous result in cache, and the rows below would otherwise paint
                that stale verdict as if the check had just passed. askAgent ON:
                the launch form on this tab keeps every entry the reader has not
                launched with (see the diagnosis notice above), so the navigation
                destroys nothing, and a preflight that cannot even run is exactly
                the credential/CLI problem the agent can look into. */}
            {preflightQuery.isError && (
              <ErrorNotice
                className="mb-2"
                message={errMsg(preflightQuery.error, i18nT('pages.settings.remoteCrewPanel.credentials_bad'))}
                askAgent
              />
            )}
            {preflightQuery.isLoading ? (
              <div className="flex items-center gap-2 text-muted text-sm py-2">
                <RefreshCw className="lucide-inline animate-spin" /> {i18nT('pages.settings.remoteCrewPanel.checking')}
              </div>
            ) : preflight ? (
              <ul className="m-0 p-0 list-none">
                <PrereqRow
                  ok={preflight.reachable && !!preflight.account}
                  title={i18nT('pages.settings.remoteCrewPanel.prereq_credentials')}
                  detail={preflight.reachable && preflight.account
                    ? i18nT('pages.settings.remoteCrewPanel.credentials_ok', { profile: checkedProfile || i18nT('pages.settings.remoteCrewPanel.no_profile_set'), account: preflight.account })
                    : ''}
                  failure={preflight.reachable && preflight.account
                    ? undefined
                    : cliRefusal
                      ? cliRefusal.failure
                    : preflight.credential_helper_missing
                      // The helper's name is the actionable part, so the sentence
                      // leads; the CLI's own error sits under it (failureDetail)
                      // for anything the sentence misses.
                      ? credentialHelperMissingSentence(preflight.credential_helper_missing)
                      : (preflight.detail || i18nT('pages.settings.remoteCrewPanel.credentials_bad'))}
                  failureDetail={preflight.reachable && preflight.account
                    ? undefined
                    : cliRefusal
                      ? cliRefusal.failureDetail
                      : (preflight.credential_helper_missing && preflight.detail) || undefined}
                  failureReport={cliRefusal ? awsCliRefusalReport(preflight, cliRefusal) : undefined}
                  steps={cliRefusal?.steps}
                  onRecheck={runCheck}
                  rechecking={preflightQuery.isFetching}
                />
                <PrereqRow ok={preflight.ec2_reachable} title={i18nT('pages.settings.remoteCrewPanel.prereq_ec2')} detail={preflight.ec2_reachable ? i18nT('pages.settings.remoteCrewPanel.service_ok') : i18nT('pages.settings.remoteCrewPanel.service_missing')} />
                <PrereqRow ok={preflight.cloudformation_reachable} title={i18nT('pages.settings.remoteCrewPanel.prereq_cloudformation')} detail={preflight.cloudformation_reachable ? i18nT('pages.settings.remoteCrewPanel.service_ok') : i18nT('pages.settings.remoteCrewPanel.service_missing')} extraAction={preflight.cloudformation_reachable ? undefined : <Btn onClick={copyPolicy}>{copied === 'policy' ? <Check className="lucide-inline" /> : <Copy className="lucide-inline" />} {copied === 'policy' ? i18nT('pages.settings.remoteCrewPanel.copied') : i18nT('pages.settings.remoteCrewPanel.copy_policy_json')}</Btn>} error={copyErr?.target === 'policy' ? copyErr.message : undefined} />
                <PrereqRow ok={preflight.ssm_reachable} title={i18nT('pages.settings.remoteCrewPanel.prereq_ssm')} detail={preflight.ssm_reachable ? i18nT('pages.settings.remoteCrewPanel.service_ok') : i18nT('pages.settings.remoteCrewPanel.service_missing')} />
                <PrereqRow
                  ok={preflight.session_manager_plugin}
                  title={i18nT('pages.settings.remoteCrewPanel.plugin')}
                  detail={preflight.session_manager_plugin
                    ? i18nT('pages.settings.remoteCrewPanel.plugin_ok')
                    : i18nT('pages.settings.remoteCrewPanel.plugin_missing')}
                  failure={pluginRefusal?.failure}
                  failureDetail={pluginRefusal?.failureDetail}
                  failureReport={pluginRefusal ? pluginRefusalReport(preflight, pluginRefusal) : undefined}
                  steps={pluginRefusal?.steps}
                  commandNote={pluginRefusal?.commandNote}
                  command={preflight.session_manager_plugin ? undefined : (preflight.session_manager_plugin_command || undefined)}
                  onCopyCommand={
                    preflight.session_manager_plugin || !preflight.session_manager_plugin_command
                      ? undefined
                      : () => { void copyCommand(preflight.session_manager_plugin_command as string) }
                  }
                  copied={copied === 'command'}
                  error={copyErr?.target === 'command' ? copyErr.message : undefined}
                  onRecheck={preflight.session_manager_plugin ? undefined : runCheck}
                  rechecking={preflightQuery.isFetching}
                />
              </ul>
            ) : (
              <div className="text-[13px] text-muted py-1">
                {/* The failure itself renders above, independent of this branch;
                    this is the no-result state with its Re-check. */}
                {i18nT('pages.settings.remoteCrewPanel.credentials_bad')}
                <div className="mt-2"><Btn onClick={runCheck} disabled={preflightQuery.isFetching}><RefreshCw className={`lucide-inline${preflightQuery.isFetching ? ' animate-spin' : ''}`} /> {preflightQuery.isFetching ? i18nT('pages.settings.remoteCrewPanel.checking') : i18nT('pages.settings.remoteCrewPanel.re_check')}</Btn></div>
              </div>
            )}
            <p className="mt-3 text-[12px] text-muted flex items-start gap-1.5">
              <CheckCircle size={13} className="mt-0.5 shrink-0 text-ok" /> {i18nT('pages.settings.remoteCrewPanel.profile_name_only')}
            </p>
          </Card>

          {/* Launch form — profile/region are set in the prerequisites card above,
              which is the same account this launches into. */}
          <Card>
            <div className="text-text font-medium mb-3">{i18nT('pages.settings.remoteCrewPanel.new_cloud_crew')}</div>

            <div>
              <div className="text-[13px] text-muted mb-2">{i18nT('pages.settings.remoteCrewPanel.size')}</div>
              <div className="space-y-2.5">
                {SIZE_TIERS.map(tier => (
                  <SizeCard key={tier.key} tier={tier} on={sizeKey === tier.key} onPick={setSizeKey} />
                ))}
              </div>

              <button
                type="button"
                onClick={() => setShowMoreSizes(v => !v)}
                className="mt-2.5 w-full flex items-center gap-2 text-[12px] text-muted px-3 py-2 rounded-md border border-dashed border-border-strong hover:text-text"
              >
                <ChevronDown size={14} className={`transition-transform ${showMoreSizes ? 'rotate-180' : ''}`} /> {i18nT('pages.settings.remoteCrewPanel.more_sizes')}
              </button>
              {showMoreSizes && (
                <>
                  <p className="mt-2 text-[12px] text-muted">{i18nT('pages.settings.remoteCrewPanel.more_sizes_hint')}</p>
                  <div className="mt-2 space-y-2.5">
                    {X86_TIERS.map(tier => (
                      <SizeCard key={tier.key} tier={tier} on={sizeKey === tier.key} onPick={setSizeKey} />
                    ))}
                  </div>
                </>
              )}
            </div>

            <label className="mt-4 block text-[13px] text-muted">
              {i18nT('pages.settings.remoteCrewPanel.subnet')}
              <input
                type="text"
                value={subnetId}
                aria-label={i18nT('pages.settings.remoteCrewPanel.subnet')}
                onChange={e => setSubnetId(e.target.value)}
                placeholder="subnet-0123456789abcdef0"
                spellCheck={false}
                className="mt-1 w-full px-2 py-1.5 text-[13px] font-mono bg-bg border border-border rounded text-text outline-hidden focus-visible:border-accent"
              />
              <span className="block mt-1 text-[12px]">{i18nT('pages.settings.remoteCrewPanel.subnet_hint')}</span>
            </label>

            <div className="mt-4">
              <div className="text-[13px] text-muted mb-2">{i18nT('pages.settings.remoteCrewPanel.identity')}</div>
              {identityQuery.isError ? (
                <>
                  {/* askAgent ON: the launch form keeps every entry the reader has
                      not launched with (crewForms.launch), so the hand-off
                      loses nothing, and a sign-in the gateway could not read is one
                      the agent can look into. The failure is non-blocking: the user
                      can still pick and type the target below. Once they have
                      picked, the notice says the selected option is theirs rather
                      than asking them to choose. */}
                  <ErrorNotice
                    variant="inline"
                    className="mb-2"
                    message={i18nT(identityChosen
                      ? 'pages.settings.remoteCrewPanel.identity_unknown_chosen'
                      : 'pages.settings.remoteCrewPanel.identity_lookup_failed')}
                    askAgent
                  />
                </>
              ) : identityUnknownCause !== null ? (
                <>
                  {/* askAgent ON, as above. The server suggests nothing, so no radio
                      is checked until the user picks, and Launch waits for that pick.
                      The notice names the cause the server reported: whoami did not
                      answer, or an Identity Center sign-in was read without its
                      portal address; after a pick it says the choice is theirs. */}
                  <ErrorNotice
                    variant="inline"
                    className="mb-2"
                    message={i18nT(
                      identityChosen
                        ? 'pages.settings.remoteCrewPanel.identity_unknown_chosen'
                        : identityUnknownCause === 'no_portal'
                          ? 'pages.settings.remoteCrewPanel.identity_unknown_no_portal'
                          : 'pages.settings.remoteCrewPanel.identity_unknown_no_answer',
                    )}
                    askAgent
                  />
                </>
              ) : null}
              <div className="space-y-2">
                <label className="flex items-start gap-2 text-[13px] text-text cursor-pointer">
                  <input
                    type="radio"
                    name="kiro-identity"
                    className="mt-0.5"
                    checked={identityRadioShown && identityMode === 'builder_id'}
                    aria-label={i18nT('pages.settings.remoteCrewPanel.identity_builder_id')}
                    onChange={() => { identityTouched.current = true; setIdentityChosen(true); setIdentityMode('builder_id') }}
                    // Re-selecting the already-checked default fires no change
                    // event, but it IS the user's explicit choice, and that
                    // choice ends the wait for the inherited identity.
                    onClick={() => { identityTouched.current = true; setIdentityChosen(true); setIdentityMode('builder_id') }}
                  />
                  <span>
                    {i18nT('pages.settings.remoteCrewPanel.identity_builder_id')}
                    <span className="block text-[12px] text-muted">{i18nT('pages.settings.remoteCrewPanel.identity_builder_id_hint')}</span>
                  </span>
                </label>
                <label className="flex items-start gap-2 text-[13px] text-text cursor-pointer">
                  <input
                    type="radio"
                    name="kiro-identity"
                    className="mt-0.5"
                    checked={identityRadioShown && identityMode === 'identity_center'}
                    aria-label={i18nT('pages.settings.remoteCrewPanel.identity_center')}
                    onChange={() => { identityTouched.current = true; setIdentityChosen(true); setIdentityMode('identity_center') }}
                  />
                  <span>
                    {i18nT('pages.settings.remoteCrewPanel.identity_center')}
                    <span className="block text-[12px] text-muted">
                      {identityQuery.data?.discovery !== 'unknown' && identityQuery.data?.identity?.account_type === 'IamIdentityCenter'
                        ? i18nT('pages.settings.remoteCrewPanel.identity_center_inherited')
                        : i18nT('pages.settings.remoteCrewPanel.identity_center_hint')}
                    </span>
                  </span>
                </label>
                {identityMode === 'identity_center' && (
                  <div className="ml-6 space-y-2">
                    <label className="block text-[12px] text-muted">
                      {i18nT('pages.settings.remoteCrewPanel.identity_start_url')}
                      <input
                        type="url"
                        value={identityStartUrl}
                        aria-label={i18nT('pages.settings.remoteCrewPanel.identity_start_url')}
                        onChange={e => { identityTouched.current = true; setIdentityChosen(true); setIdentityStartUrl(e.target.value) }}
                        placeholder="https://example.awsapps.com/start"
                        spellCheck={false}
                        aria-invalid={identityStartUrl !== '' && !identityStartUrlOk}
                        className="mt-1 w-full px-2 py-1.5 text-[13px] font-mono bg-bg border border-border rounded text-text outline-hidden focus-visible:border-accent"
                      />
                    </label>
                    <label className="block text-[12px] text-muted">
                      {i18nT('pages.settings.remoteCrewPanel.identity_region')}
                      <input
                        type="text"
                        value={identityRegion}
                        aria-label={i18nT('pages.settings.remoteCrewPanel.identity_region')}
                        onChange={e => { identityTouched.current = true; setIdentityChosen(true); setIdentityRegion(e.target.value) }}
                        placeholder="us-east-1"
                        spellCheck={false}
                        aria-invalid={identityRegion !== '' && !identityRegionOk}
                        className="mt-1 w-full px-2 py-1.5 text-[13px] font-mono bg-bg border border-border rounded text-text outline-hidden focus-visible:border-accent"
                      />
                      <span className="block mt-1">{i18nT('pages.settings.remoteCrewPanel.identity_region_hint')}</span>
                    </label>
                  </div>
                )}
              </div>
            </div>

            <div className="mt-4 flex items-start gap-2 rounded-md border border-border bg-bg-elevated px-3 py-2.5">
              <AlertTriangle size={15} className="mt-0.5 shrink-0 text-warn" />
              <div className="text-[12px] text-text">
                {i18nT('pages.settings.remoteCrewPanel.billing')}{' '}
                <a className="text-accent font-medium hover:underline inline-flex items-center gap-1" href={PRICING_CALCULATOR_URL} target="_blank" rel="noreferrer">
                  {i18nT('pages.settings.remoteCrewPanel.pricing_calculator')} <ExternalLink size={12} />
                </a>
              </div>
            </div>

            <div className="mt-4 flex items-center gap-3 flex-wrap">
              {/* Name the lane that is on screen. The built-in form draws EVERY
                  `aws_ec2`-kind row, and an edition may register a second one
                  behind a different engine; omitting the id would let the server
                  default to the built-in and provision on the wrong lane. Only
                  an UNKNOWN list (loading or failed) sends the pre-seam body. */}
              <Btn primary onClick={() => { sentLaunchDraft.current = currentLaunchDraft; launchMutation.mutate({ ...(selectedProvisioner ? { provider_id: selectedProvisioner.id } : {}), profile, region, size_key: sizeKey, ...(subnetId.trim() ? { subnet_id: subnetId.trim() } : {}), ...loginTargetBody }) }} disabled={!blockingOk || !identityOk || launchMutation.isPending}>
                <Rocket className="lucide-inline" /> {launchMutation.isPending ? i18nT('pages.settings.remoteCrewPanel.launching') : i18nT('pages.settings.remoteCrewPanel.launch')}
              </Btn>
              <span className="text-[12px] text-muted">
                {identityResolving
                  ? identityUnknown
                    ? i18nT('pages.settings.remoteCrewPanel.identity_choose')
                    : i18nT('pages.settings.remoteCrewPanel.identity_resolving')
                  : !identityOk
                    ? i18nT('pages.settings.remoteCrewPanel.identity_incomplete')
                    : blockingOk ? i18nT('pages.settings.remoteCrewPanel.ready_in_6') : i18nT('pages.settings.remoteCrewPanel.finish_prereqs')}
              </span>
            </div>
          </Card>
          </>
          ) : registeredProvisioner && selectedProvisioner ? (
            // The edition owns this form entirely — its own inputs, its own copy,
            // its own prerequisites. Isolated in an ErrorBoundary so a throwing
            // renderer costs the user this form and not the whole Settings page;
            // the progress card and the status notice below still render, which is
            // what keeps a launch already in flight visible.
            <ErrorBoundary scope={`remote-provisioner:${selectedProvisioner.kind}`}>
              <registeredProvisioner.component
                provisioner={selectedProvisioner}
                launch={input => launchMutation.mutate({
                  provider_id: selectedProvisioner.id,
                  profile: input.profile ?? '',
                  // `||`, not `??`: a provisioner form with no region field sends
                  // an EMPTY STRING, not undefined, and every provisioner's
                  // preflight refuses an empty region -- reported as
                  // `region='' is not a region`, which reads as a malformed value
                  // rather than an absent one. Falling back to the panel's own
                  // region means a form that does not ask for one inherits the
                  // account the reader already chose here. The gateway defaults
                  // it too, from the operator's config; this is what keeps the
                  // two from disagreeing about which region the reader picked.
                  region: input.region || region,
                  size_key: input.size_key,
                })}
                launching={launchMutation.isPending}
                disabled={disabled}
                activeJob={activeJob}
              />
            </ErrorBoundary>
          ) : null}

          {/* The progress poll itself failed. Gated on the query having a job to
              poll (`effectiveLaunchId`), NOT on `activeJob`: when the polled
              detail never arrived and the list carries no copy either, the card
              below is absent and this notice is the only thing that says why.
              When a card IS showing, it shows the last state received, not a
              live one, and would otherwise just stop moving. askAgent ON: the
              launch form keeps every entry the reader has not launched with,
              so the navigation loses
              nothing. */}
          {effectiveLaunchId && launchStatusQuery.isError && (
            <ErrorNotice
              className="mt-4"
              message={errMsg(launchStatusQuery.error, i18nT('pages.settings.remoteCrewPanel.launch_status_unavailable'))}
              askAgent
            />
          )}
          {activeJob && (
            <LaunchProgressCard
              job={activeJob}
              cancelling={cancelMutation.isPending && cancelMutation.variables === activeJob.id}
              onCancel={id => cancelMutation.mutate(id)}
              onRestartSignin={id => signinRestartMutation.mutate(id)}
              restartingSignin={signinRestartMutation.isPending}
              onFetchSignin={id => signinMutation.mutate(id)}
              fetchingSignin={signinMutation.isPending}
              signinNotice={signinNotice && signinNotice.jobId === activeJob.id ? signinNotice : null}
            />
          )}
        </div>
      )}
    </div>
  )
}
