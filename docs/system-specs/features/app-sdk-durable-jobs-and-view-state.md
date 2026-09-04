# App SDK: durable jobs and view state

## Current SDK boundary

The App SDK does not provide a shared durable-job or URL-view-state contract.
`AppContext` exposes `cron`, `events`, `storage`, and `spawn` when their
permissions allow them; it has no job service (`src/kiro_crew/apps/context.py`,
`AppContext` and `build_app_context`). The frontend barrel exports app API,
event, metadata, navigation, and chat surfaces, but no `useAppJob` or
`useAppViewState` hook (`website/src/app-sdk/index.ts`, `useAppApi`,
`useAppEvents`, `useAppInfo`, `useNavigate`, and the barrel exports).

This boundary is load-bearing: an app that needs to report or recover
long-running work must own its server route, persistence, lifecycle recovery,
and frontend reattachment. A common SDK cannot be assumed to supply those
semantics.

`CronSDK` is a separate app-scoped scheduling surface (`src/kiro_crew/apps/cron_sdk.py`,
`CronSDK`). It does not establish a job-run registry for app-initiated HTTP
work.

## Durable work is currently app-specific

### AWS Control backups

`_handle_backup_run` executes the selected backup runner in a worker thread and
returns its terminal record (`src/kiro_crew/apps/builtins/aws_control/backend/routes.py`,
`_handle_backup_run`). The runner records completed backup metadata through
`_record_run`, and `last_runs` reads that per-account terminal ledger
(`backend/backup.py`, `_record_run` and `last_runs`). The status endpoint
returns that ledger as `runs` (`backend/routes.py`, `_handle_backup_status`).
There is no backup job identifier or persisted in-flight registry in this path.

A worker thread cannot be killed by cancelling the awaiting coroutine, and
`_STOP` only prevents an upload reached after app teardown (`backend/backup.py`,
`_STOP` and `_authorize_upload`). A client disconnect therefore does not create
a cancelable or reattachable backup job.

The backup UI starts the request with a component-owned React Query mutation and
derives its running indicator from that mutation (`website/src/apps/aws-control/DrivePage.tsx`,
`BackupSection` and `runMut.isPending`). The API client posts directly to the
backup route (`website/src/apps/aws-control/api.ts`, `backupRun`). A fresh mount
can read prior terminal records through the status query, but it has no
server-owned in-flight record to adopt.

### Code Review Sage review runs

Code Review Sage implements its own durable review-run registry. Its backend
stores lightweight run descriptors in `_RUNS`, writes the registry to
`runs.json`, and reloads it at startup (`src/kiro_crew/apps/builtins/code_review_sage/backend/routes.py`,
`_RUNS`, `_runs_file`, `_save_runs`, and `_load_runs`). On load,
`_load_runs` marks a persisted `running` review as `interrupted` because the
in-process driver did not survive the restart. The test
`test_orphaned_running_becomes_interrupted_on_load` enforces that recovery
rule (`src/kiro_crew/apps/builtins/code_review_sage/tests/test_backend_routes.py`).

That recovery is load-bearing: leaving an orphaned run marked running would
advertise work that no process can complete and could block follow-up actions.
The registry is an app implementation, not an App SDK surface.

### Dev Fleet runs

Dev Fleet tracks run descriptors in the module-memory `_RUNS` registry and
keeps active tasks and subprocesses in `_ACTIVE_RUNS`
(`src/kiro_crew/apps/builtins/dev_fleet/runtime.py`, `_RUNS` and `_ACTIVE_RUNS`).
Its fleet payload overlays `sync_run_id` and per-worktree provision run IDs at
request time in `_with_live_run_pointers`; `DevFleetPage` uses those pointers
to reattach a mounted page to a current run (`http_api.py`,
`_with_live_run_pointers`; `website/src/pages/DevFleetPage.tsx`, sync and
provision reattach effects).

The run records and pointers are process memory, so this reattachment applies
within the current gateway process rather than supplying restart durability.
During `dev_fleet_cleanup`, the app closes admission for new runs, snapshots
active work, and kills tracked process trees (`server.py`,
`dev_fleet_cleanup`; `runtime.py`, `_kill_tree`). That cleanup prevents a stopped gateway
from leaving Dev Fleet subprocesses behind; it also shows why a durable record
cannot imply that its original process is still executable.

## Job SDK process scope

`JobSDK` records the liveness of the gateway process. `_ORIGIN` is minted once per
gateway process, a run's worker is a thread in that process, and `reconcile`
resolves every non-terminal record whose origin is not the current `_ORIGIN`
(`src/kiro_crew/apps/job_sdk.py`, `_ORIGIN`, `JobSDK.start`, `JobSDK._execute`, and
`JobSDK.reconcile`). Staleness is decided by one question: whether the gateway
process that minted the record survived.

An app declaring `backend.entryPoint` does not execute its work in that process.
`start_app_backend` spawns the backend under `popen_limited` as a separate OS
process with its own interpreter and rlimit profile
(`src/kiro_crew/apps/backend.py`, `start_app_backend`), and that app's teardown
hook runs on the backend's own aiohttp application rather than the gateway's
(`src/kiro_crew/apps/builtins/dev_fleet/app.json`, `backend.entryPoint`;
`dev_fleet/server.py`, `dev_fleet_cleanup` registered on `on_cleanup`).
`JobSDK.register` binds a kind to a Python callable held in the gateway
(`job_sdk.py`, `JobSDK.register`), so a runner defined in a backend process has no
registration path.

Together the two produce a record that contradicts the work it describes. At
gateway startup `_reap_stale_app_backends` terminates a backend left by a prior
gateway generation only when the pid's identity positively matches the recorded
one; when identity cannot be confirmed the pid is left alone (`backend.py`,
`_reap_stale_app_backends`). A backend spared by that check keeps executing, and
the new gateway's reconciliation marks its runs `INTERRUPTED`, recording that the
gateway restarted while the run was executing and that no runner is registered for
its kind — a backend-process runner has no registration path, so the resolved
record's `interrupt_cause` is `runner_unregistered` (`job_sdk.py`,
`JobSDK.reconcile` and `CAUSE_RUNNER_UNREGISTERED`).
`INTERRUPTED` is a member of `TERMINAL_STATES`, so that record is neither
reconciled again nor resumed (`job_sdk.py`, `TERMINAL_STATES`).

This scope is load-bearing: an app whose run registry is process memory reports
nothing after a restart, and reporting nothing is accurate, while a durable record
written from a process that does not own the work reports a run as ended when it
has not ended. Dev Fleet's registry is the process-memory case (see "Dev Fleet
runs"). `JobSDK` describes work that the gateway process itself executes.

A durable `running` record and a process actually owning the work are therefore
two separate facts, and the public view serves both.
`JobSDK.cancelling_and_live_ids` returns the `cancelling` and `live` run id
sets under a single lock acquisition, so the two fields describe one instant;
`live` is live-table membership — claimed at start, dropped after the terminal
write — the same authority that `cancel` and `reconcile` already consult
(`src/kiro_crew/apps/job_sdk.py`, `JobSDK.cancelling_and_live_ids`).
`_public_view` serves that as the boolean `live` field, computed at read time
from memory — nothing new is persisted, and `origin` and `pid` stay withheld
(`src/kiro_crew/apps/job_routes.py`, `_public_view`). A record whose terminal
write failed twice and a record minted by another gateway process both read
`live: false`, which is what lets an API consumer tell a stale `running`
record from real work between the reconciliation passes that would eventually
resolve it.

## View position is not an App SDK contract

Builtin apps are mounted by the single-segment `/:builtinApp` route, and
`BuiltinAppRoute` resolves that parameter to one component
(`website/src/App.tsx`, builtin-app `Route`; `website/src/apps/BuiltinAppRoute.tsx`,
`BuiltinAppRoute`). The host does not provide a builtin-app sub-route contract.

Apps can already read URL search parameters: `CodeReviewSagePage` calls
`useSearchParams` to select an initial run (`website/src/apps/code-review-sage/CodeReviewSagePage.tsx`,
`CodeReviewSagePage`). The App SDK does not namespace, serialize, or synchronize
view keys for apps, so each app that needs URL-backed state must define that
contract itself.

AWS Control keeps its selected account and drive in local component state
(`website/src/apps/aws-control/AwsControlPage.tsx`, `AwsControlPage`). Those
values are not encoded in its URL, so they cannot identify an account or drive
in a shareable link. This local-state boundary is load-bearing: a URL can only
restore coordinates that the application has made part of its URL contract.

## Query-client scope

The shared `QueryClient` configures query defaults
(`website/src/api/queryClient.ts`, `queryClient`). It does not make a mutation's
component-owned pending state a server-owned work record. Durable work therefore
requires an app-owned registry or another explicit server contract; persisting
or reusing a client-side pending flag would not establish whether the backend
still has work in progress.
