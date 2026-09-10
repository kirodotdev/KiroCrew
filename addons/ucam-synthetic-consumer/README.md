# Fixed synthetic Kiro Crew consumer

Prepared on 2026-09-10 in an isolated branch. **Never install this proof on the
shared gateway.** Deployment targets a newly owned isolated host with empty state;
deployment and real native-proof status are recorded separately, not implied by
these source files or unit tests. FleetDeck harness files are not part of this patch.

## Contract and actual identity

This is an App Kit app, not a provider, legacy importer, or global hook. Its input
route is `POST /api/apps/ucam-synthetic-consumer/consume`, accepting exactly
`{"task":"synthetic task","request_id":"stable-caller-id"}`. It requires middleware-verified BOTH
`request["app"] == "ucam-synthetic-consumer"` and
`request["user"] == "ucam-synthetic-consumer"`.

The second comparison is intentional: the deployed token exchange calls
`generate_token(app_name, app=app_name)`. **Its subject is the app name, NOT a
Cognito user subject.** The operator-owned configuration maps that one synthetic
app to exactly one UCAM owner/workspace/scope. Arbitrary users are not delegated.
No owner, workspace, scope, agent, model, or credential override is accepted.
The existing OWUI bridge cannot use this route; do not change its configuration.
An optional separate operator-only OWUI connection may use the NEW app secret.

The app uses `ctx.spawn.run(..., agent="ucam-synthetic-reader", silent=True)`.
The initial response says `queued`, never `injected`. The authenticated result
route is `GET /api/apps/ucam-synthetic-consumer/runs/{run_id}`; see the v2 contract
below for durable deduplication and bounded output. Spawn SDK ownership checks,
governance, admission, concurrency control, and SEL remain in the host. The agent
declares no tools, resources, or MCP servers; model selection remains inherited.
Verify its materialized filename is app-owned and its JSON `name` remains the
dispatchable agent name. A duplicate existing name is a deployment refusal.

## Actual native send path

1. Only the reserved app disables session sharing and must obtain a fresh,
   non-resumed Kiro ACP session. Retained conversations and cancellation recovery
   are refused. A durable run-hash attempt marker prevents replay after restart.
2. `SubagentManager._run_inner` still calls `ContextBuilder.build_message`.
   The exact deployed patch passes an empty `context_groups`, the custom agent
   name, and `blocks_reads=True` for this app only. Critical rules and conduct
   remain; memory/lessons/project groups and legacy history reads are excluded.
   The older checkout lacks `context_groups`; its local patch uses its supported
   custom-agent/`blocks_reads` seam. **Deploy the exact-version generated diff,
   not a wholesale checkout module.**
3. `ConsumerRun.stream` activates a task-local callback only while advancing the
   dedicated provider stream. `AcpClient._send_request` invokes it only for
   `session/prompt`, after readiness checks and native prompt-block construction.
4. The callback GETs `/iam/v1/{scope}/projection`, verifies the exact UTF-8
   `canonical_payload` SHA-256 and typed deep-equivalence of the separately used
   scope/generation/epoch/records. Duplicate keys, nonfinite values, unsafe control
   integers, stale leases, non-approved or expired records, wrong owner/workspace
   `scope_context`, and non-synthetic records fail closed. Finite exponent/fraction
   metadata is supported. DNA exchange objects remain nested and unmodified.
5. A successful `fetched` ACK must echo the receipt and the server-pinned harness
   `kirocrew`. The callback adds an advisory JSON text block to the outgoing ACP
   prompt, not to a mutable prompt file. It rechecks the lease immediately before
   stdin.write, after serialization, with a one-second safety margin.
6. Only successful `stdin.drain()` sets the native receipt and attempts `injected`.
   Drain proves local transport handoff, NOT model understanding. Logs contain
   adapter version, run hash, projection digest, and outgoing prompt hash only.
   API errors log phase/run hash, not tokens or claims. ACK failure after dispatch
   never retries the prompt. `turn_result` describes stream completion/degradation,
   not independent proof that the model followed memory.

HTTP operations have a 2.5-second total timeout each, including credential-file
loading. Before dispatch there are at most two API operations (GET and fetched
ACK). There is no offline/legacy fallback for this dedicated consumer. Unrelated
apps/sessions never load UCAM config or change their legacy paths. The consumer
does not capture candidates or write legacy lessons, and gets no create/review
grant. Existing synthetic transcript/result persistence remains host-owned.

## Separate service principal and protected files

Provision a NEW role, not the gateway instance role or a native harness role.
Trust only the parent-approved operator credential issuer. Its IAM policy needs
`execute-api:Invoke` for exactly the service API/stage's
`GET/iam/v1/{scope}/projection` and `POST/iam/v1/{scope}/acks` resources. It needs
no DynamoDB, grant administration, record creation, or review access.

Use the service's operator grant flow with principal equal to the role ARN
(assumed-role sessions normalize to that ARN), permissions `["read","ack"]`,
harness `kirocrew`, source `agent-self-report`, synthetic `true`, short expiration,
and the existing synthetic scope's exact seven `scope_context` dimensions. Match
its `user` and `workspace` to the configuration. Empty projections cannot attest
these dimensions themselves; grant provisioning is the trusted binding boundary.

Create `/opt/ucam-consumer/config.json` as an operator-owned, gateway-readable
file; never overwrite an existing file. Its exact keys are:

```json
{
  "enabled": true,
  "api_url": "https://SERVICE.execute-api.REGION.amazonaws.com",
  "scope": "EXISTING_SYNTHETIC_SCOPE",
  "owner_sub": "EXISTING_SYNTHETIC_OWNER",
  "workspace": "EXISTING_SYNTHETIC_WORKSPACE",
  "generation": "CURRENT_SERVICE_GENERATION",
  "region": "us-east-1",
  "credentials_file": "/opt/ucam-consumer/credentials.json",
  "state_dir": "/opt/ucam-consumer/state"
}
```

The separate short-lived STS credential file has exactly `access_key`,
`secret_key`, `token`, `expires_at` (Unix seconds). No ambient AWS credentials or
instance metadata fallback is used. The operator rotates it atomically outside
the app. Keep credentials/config out of model resources, logs, shell argv and
SSM command output; use protected files/secrets staging. Create the state
directory gateway-writable and config/credentials gateway-readable, other-user
inaccessible. Same-UID gateway Python apps remain mutually trusted: this is NOT a
sandbox against a malicious installed backend.

## Exact-version deployment driver — parent approval required

The shared-host rollout is superseded. Use this helper to derive and validate
the exact-version postimages for persistent per-file mounts on the NEW isolated
host. The apply/rollback commands below describe its mechanics and tests, not
authorization to mutate or restart the original gateway.

`deploy.py` changes only `subagent.py` and `acp/client.py`, and adds
`ucam_consumer.py`. It checks eleven deployed source hashes, including unchanged
auth, token exchange, provider, context, and App Kit seams. It derives narrow
edits from those exact preimages, checks unique anchors and Python syntax, pins
all outputs in a hashed plan, refuses existing addon modules, keeps backups,
and refuses rollback over another worker's drift. It NEVER restarts the gateway,
installs/enables apps, changes existing config, or provisions credentials/grants.

Prepare locally against read-only deployed snapshots:

```sh
python addons/ucam-synthetic-consumer/deploy.py prepare \
  --source-root /tmp/ucam-kirocrew-deployed \
  --module src/kiro_crew/ucam_consumer.py \
  --bundle /tmp/ucam-kirocrew-bundle
```

Send the two generated `.patch` files and the printed plan SHA for review.
After separate parent authorization, stage the approved bundle/driver into a
new protected directory on the gateway container; recheck container image ID,
package version, and all source hashes. The live Python is 3.12, package 0.2.0,
CLI 2.16.2. Source hashes, not the version string alone, are authoritative.

The inspected gateway has aiohttp 3.14.3 but **no botocore**. Build an isolated
wheelhouse from `requirements.txt`, install into a new
`/opt/ucam-consumer/deps` using `pip --target` (never upgrade existing packages),
and add ONE new `.pth` file containing that directory to Python site-packages.
Inspect and approve wheel hashes, dependencies and the new `.pth` path before
deployment. A controlled restart is needed to activate the path. The driver
preflight refuses if dependencies are unavailable. No wheel install was done on
the gateway by this lane.

Inside the target container, with the reviewed SHA and paths:

```sh
python deploy.py preflight --source-root /usr/local/lib/python3.12/site-packages/kiro_crew \
  --bundle APPROVED_BUNDLE --approved-plan-sha REVIEWED_SHA
python deploy.py apply --source-root /usr/local/lib/python3.12/site-packages/kiro_crew \
  --bundle APPROVED_BUNDLE --approved-plan-sha REVIEWED_SHA
```

Parent owns the shared maintenance window: quiesce new dispatches, ensure no
active users are interrupted, take a restart/health baseline, apply only the
approved bundle, then restart once and verify health before installing the new
app. Never restart merely to test whether a patch works. Install the new app
locally using `kirocrew app install APPROVED_BUNDLE/app`, checking name/path
collisions first. Installation creates disabled metadata and its own secret;
it does not authorize backend execution. Use the operator-authenticated gateway
API for the remaining dynamic lifecycle steps, NOT a new app token:

- `POST /api/security/trusted-apps/ucam-synthetic-consumer` grants this app only.
- `POST /api/apps/ucam-synthetic-consumer/enable` enables it and wires live routes.

**Exact-version discovery:** the global `agent.apps_allow_third_party` flag is
false, but the deployed `apps/execution.py` ALSO supports `agent.apps_trusted`
and the security handler has the narrow POST/DELETE routes. The older checkout
does not have that per-app feature. The grant endpoint requires a real installed
or known-registry app, takes the app lifecycle lock, preserves other grants, and
refuses overlay-owned/corrupt config with 409. Install this new local app disabled
first; do not pre-grant an unowned name, edit config JSON wholesale, call the
`allow-all` endpoint, forge builtin provenance, or bypass admission/governance.
No new trust-gate code patch is needed. Actual install, per-app grant and enable
remain parent-authorized live changes, not actions already performed here.

Mint the NEW app token through
`POST /api/apps/ucam-synthetic-consumer/token` with its own `X-App-Secret`, then
call `/consume` using the port-scoped gateway cookie and trusted Origin. Do not
print token responses. Check exactly one fresh synthetic run and native-write /
fetched / injected / turn_result digest traces. A scheduled ID or unit test is
not a deployed consumer success.

Rollback: stop new synthetic requests and wait for its runs to end. Use operator
`DELETE /api/security/trusted-apps/ucam-synthetic-consumer` to revoke only this
app's execution trust; the deployed handler tears down its routes/backend and
disables it rather than merely changing metadata. Revoke its separate UCAM grant.
Run `deploy.py rollback` with the same source root/bundle/approval arguments,
then perform the separately approved shared restart/health check. The driver
restores original bytes but deliberately retains the inert added module until
the old patched process has exited; deleting it first could break late imports.
Remove only newly owned files/dependency path after verified restoration. Never
replace existing config, auth files, provider modules, or other app installs.

## Validation limits

Focused tests drive the actual `_send_request` method against fake stdin, not a
reimplementation. They cover exact-payload floats, identity/record-scope mismatch,
outage/timeout, final write-time expiry, drain failure, ACK failure, restart replay,
cross-task isolation, route restrictions, and driver drift/rollback. Broader
existing ACP/App Kit/subagent tests are separate regression evidence. No tests
constitute live Kiro authentication, app admission, model-memory use, or shared
restart proof. The focused suite has 56 passing tests. A broader ACP/App Kit
selection reports 576 passes and four process-basename test failures; the same
four fail when importing the untouched checkout, so they are not fixed here.
The separate subagent/resilience/Spawn SDK/consumer selection has 189 passes
(with existing mock-coroutine warnings). Counts overlap and must not be summed.
## Additive result contract (2026-09-10)

Shared-host rollout is superseded: deploy only the new owned isolated synthetic
host. Token minting remains the existing app-secret endpoint, with verified
subject and app both `ucam-synthetic-consumer`.

`POST /api/apps/ucam-synthetic-consumer/consume` requires exactly
`{"task":"synthetic task","request_id":"caller-stable-id"}`. A durable reservation
precedes Spawn SDK dispatch. A repeated identical request returns the same native
run ID; changed task or ambiguous interrupted dispatch returns 409 without retry.
Success is HTTP 202 with `{"run_id":"...","phase":"queued"}` (replays may be later
phases). The per-namespace proof budget is 32 reservations, including failures.

`GET /api/apps/ucam-synthetic-consumer/runs/{run_id}` requires the same verified
identity and a run registered in the exact scope/owner/workspace/generation
namespace. Unknown/foreign runs return 404. Success returns `run_id`, `phase`
(`queued`, `running`, `completed`, `failed`), `text` and `outcome`. Text is bounded
to 64 KiB; native streaming has a 120-second deadline. A stale queued/running
reservation is reported failed after 180 seconds, never automatically replayed.
`completed` with `outcome=degraded` means native completion but ACK failure, not
canonical acknowledgement. Persistent operator evidence includes actual native
write, prompt/digest/run hashes, adapter version and ACK status. No legacy ingest,
candidate capture or transcript-file fabrication occurs.

The additive `evidence` object is generated only by the native observer and
scoped run registry, never parsed from model text. It contains `adapter`,
`run_hash`, `digest`, `generation`, `epoch`, `prompt_hash`, `native_sent`,
`fetched_ack`, `injected_ack`, `turn_result_ack`, and `ack_failed`. A running result
may contain fetched-but-not-sent evidence. `native_sent` is set only after the
actual ACP stdin drain; fetched acknowledgement precedes prompt delivery, and
lease validity is checked again immediately before the actual write. Empty
evidence means no observer receipt, not a no-memory success. These additive
fields correlate the OWUI request/run with native projection delivery without
exposing secrets or granting memory authority to source output.

The host's native done probe ends a failed pre-observer run promptly with
`ucam_native_result_missing`. A delayed native startup cannot write after the
original 180-second reservation deadline: that deadline is checked again at the
actual stdin-write boundary. Expired requests never silently become fresh runs.
