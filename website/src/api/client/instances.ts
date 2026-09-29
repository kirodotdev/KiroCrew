/**
 * Remote crew instances: registry CRUD, connect/token/disconnect/restart,
 * session transfer (file export, file import, send to an instance), and a
 * connected peer's federated session search, capabilities and live slots.
 */

import type { RemoteCrewCapabilities } from '../../hooks/useRemoteCapabilities'
import { toApiError } from '../apiError'
import type { ClientTransport } from './transport'

export interface InstanceTunnelStatus {
  instance_id: string
  state: 'disconnected' | 'connecting' | 'connected' | 'error' | 'stopped'
  local_port?: number
  remote_port?: number
  error?: string
  connected_at?: number
  token_ttl_remaining?: number
  /** Seconds the CURRENT token was issued for -- what `token_ttl_remaining`
   *  counts down from. Read it, never the row's `ttl`, when measuring how far a
   *  token has run: a chained crew's token is minted by the crew holding the
   *  hop, so the row's figure is a different number. See `lib/tokenTtl`. */
  token_ttl_total?: number
  /** Fargate only: the loopback URL of the crew's turn API through the open
   *  forward. Present only while connected; never accompanied by a token. */
  turn_url?: string
  diagnosis?: {
    code:
      | 'ok'
      | 'not_connected'
      | 'ssh_unreachable'
      | 'ssm_unreachable'
      | 'remote_down'
      | 'tunnel_down'
      | 'unknown'
    ok: boolean
    reason: string
    probes: { name: string; ok: boolean }[]
  }
}

export interface SsoStatus {
  state: 'ok' | 'expiring' | 'expired' | 'unknown'
  seconds_remaining: number | null
  expires_at: number | null
  reason: string
}

export interface InstanceView {
  id: string
  name: string
  ssh_host: string
  remote_port: number
  local_port: number
  ttl: string
  remote_bin: string
  /** Transport used to reach the instance. Older records default to 'ssh'.
   *  'fargate' forwards SSM to an ECS task that serves a turn API and no
   *  dashboard, so its status carries `turn_url` and never a token. */
  connection_method: 'ssh' | 'ssm' | 'fargate'
  /** SSM: EC2 instance id (i-...) or SSM managed-instance id (mi-...).
   *  Fargate: ECS task target (ecs:<cluster>_<task-id>_<runtime-id>). */
  ssm_target: string
  /** SSM/fargate: named AWS profile ('' = default credential chain). */
  aws_profile: string
  /** SSM/fargate: AWS region ('' = profile/environment default). */
  aws_region: string
  ssm_run_as: string
  /** Provisioner that created this crew, when it came from a launcher. */
  provisioner_id?: string
  /** Set when this crew is reached by riding another crew's hop: that crew's id,
   *  and the loopback port ON THAT CREW where its own forward listens. All empty
   *  for a top-level crew, which is every crew added before chaining existed.
   *  `via_remote_id` is this crew's id in the PARENT's registry -- the id the
   *  parent looks it up by when it mints this crew's token. */
  via_instance_id?: string
  via_remote_port?: number
  via_remote_id?: string
  was_connected: boolean
  status: InstanceTunnelStatus
}

export interface AddInstanceBody {
  name: string
  /** Required when connection_method is 'ssh' (the default). */
  ssh_host?: string
  remote_port?: number
  ttl?: string
  remote_bin?: string
  /** Transport to reach the instance. Defaults to 'ssh' when omitted. */
  connection_method?: 'ssh' | 'ssm' | 'fargate'
  /** Required when connection_method is 'ssm' (i-... / mi-... instance id)
   *  or 'fargate' (ecs:<cluster>_<task-id>_<runtime-id> task target). */
  ssm_target?: string
  aws_profile?: string
  aws_region?: string
  ssm_run_as?: string
  /** Chain this crew behind one already configured here: that crew's id, plus the
   *  loopback port on it where its own forward to this crew listens, plus this
   *  crew's own id IN THAT CREW's registry, which is what the parent looks it up
   *  by when it mints the token. The gateway decides whether the chain is allowed
   *  (depth cap, parent transport) and refuses with a `chain_*` code. */
  via_instance_id?: string
  via_remote_port?: number
  via_remote_id?: string
  id?: string
}

/**
 * The saved filename a `Content-Disposition` asks for, or `fallback`.
 *
 * Prefers the RFC 5987 `filename*=UTF-8''<percent-encoded>` form, because that is
 * what the dashboard's own download handlers emit so a non-Latin name survives an
 * ASCII header. The bare `filename=` form is still read for anything that sends
 * it. Exported so the precedence can be unit-tested without a DOM.
 *
 * A malformed percent sequence falls through to the next candidate rather than
 * throwing: `decodeURIComponent` raises on bad input, and a broken header must not
 * take the download with it.
 */
export function filenameFromDisposition(disposition: string, fallback: string): string {
  const star = /filename\*=(?:UTF-8'')?([^;]+)/i.exec(disposition)
  if (star) {
    try {
      const decoded = decodeURIComponent(star[1].trim())
      if (decoded) return decoded
    } catch {
      // fall through to the bare form
    }
  }
  const plain = /filename="?([^";]+)"?/.exec(disposition)
  return (plain && plain[1].trim()) || fallback
}

export function createInstancesEndpoints({ get, post, del, patch, j, jInstancesDisabled, sessionKeyHeader: _sk }: ClientTransport) {
  const registryAndTransfer = {
    // Instances (multi-instance management) — owner-only, gated by instances.enabled.
    // listInstances throws ApiError(403) when the feature is disabled; callers
    // should catch and render the enable toggle rather than an error. `active`
    // is true only when the SSH manager is actually running (the flag was on at
    // gateway startup) — enabled-but-not-active means a restart is required.
    //
    // `jInstancesDisabled` rather than `j`: because the plane is deny-by-default,
    // that 403 is the EXPECTED answer on most installs, so journaling it published a
    // spurious "/api/instances -> HTTP 403" error report on whatever route
    // mounted the sidebar that runs this probe. The parser is pre-bound to the
    // gateway's own `instances_disabled` code, not to the status, so this
    // endpoint's other 403s (a non-owner caller, a Slack-origin request) still
    // journal — they are real authorization failures and a reader needs them.
    listInstances: () => get('/api/instances').then(jInstancesDisabled) as Promise<{ active: boolean; instances: InstanceView[]; warm_set_cap: number; sso: SsoStatus }>,
    addInstance: (body: AddInstanceBody) => post('/api/instances', body).then(j) as Promise<InstanceView>,
    updateInstance: (id: string, body: Partial<AddInstanceBody>, opts?: { signal?: AbortSignal }) =>
      patch('/api/instances/' + encodeURIComponent(id), body, undefined, opts?.signal).then(j) as Promise<InstanceView>,
    removeInstance: (id: string) => del('/api/instances/' + encodeURIComponent(id)).then(j),
    instanceStatus: (id: string, diagnose = false) =>
      get('/api/instances/' + encodeURIComponent(id) + '/status' + (diagnose ? '?diagnose=1' : '')).then(j) as Promise<InstanceTunnelStatus>,
    connectInstance: (id: string, opts?: { rebuild?: boolean; onlyIfConnected?: boolean }) =>
      post(
        '/api/instances/' +
          encodeURIComponent(id) +
          '/connect' +
          (opts?.rebuild ? '?rebuild=1' : opts?.onlyIfConnected ? '?only_if_connected=1' : ''),
      ).then(j) as Promise<InstanceTunnelStatus & { token?: string }>,
    refreshInstanceToken: (id: string) =>
      post('/api/instances/' + encodeURIComponent(id) + '/refresh-token').then(j) as Promise<
        InstanceTunnelStatus & { token?: string }
      >,
    disconnectInstance: (id: string) =>
      post('/api/instances/' + encodeURIComponent(id) + '/disconnect').then(j) as Promise<{
        disconnected: string
        was_connected: boolean
      }>,
    restartInstance: (id: string) =>
      post('/api/instances/' + encodeURIComponent(id) + '/restart').then(j) as Promise<{
        ok: boolean
        message: string
      }>,
    // Copies a session to another instance. The local session is left untouched:
    // the peer allocates its own key, so this is a copy and never a move.
    /** Download one session as a single gzipped file.
     *
     *  Fetches rather than navigating, so a refusal (an incognito session, an
     *  empty one) raises here and the menu row can report it, instead of replacing
     *  the dashboard with a raw JSON error body. The saved filename comes from the
     *  endpoint's own Content-Disposition, whose slug is built from the REDACTED
     *  title — the frontend must not reconstruct a name from `slot.title`, which
     *  is the unredacted copy. */
    exportSession: async (slot: string) => {
      const r = await get('/api/chat/slots/' + encodeURIComponent(slot) + '/export')
      if (!r.ok) {
        throw await toApiError(r)
      }
      const blob = await r.blob()
      const filename = filenameFromDisposition(
        r.headers.get('Content-Disposition') || '',
        `${slot}.kcsession.json.gz`,
      )
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = filename
      document.body.appendChild(a)
      a.click()
      a.remove()
      URL.revokeObjectURL(url)
    },
    /** Install a session from an exported file, as its own new session.
     *
     *  Posts the file's BYTES unchanged — no gunzip, no re-encode, no JSON wrapper.
     *  The endpoint reads the format off the first two bytes, so a `.gz` straight
     *  off disk and a plain `.json` a user unpacked by hand both work, and the
     *  browser never has to know which it was handed. Sending
     *  `application/octet-stream` is honest for the same reason: the file's type is
     *  whatever the platform recorded, and it is not this call's to assert.
     *
     *  An install ADDS a session and touches no existing one, so a repeat needs no
     *  confirm step: installing the same file twice is two sessions, which is the
     *  documented behaviour rather than an accident to guard against. */
    importSessionFromFile: async (file: Blob) => {
      const r = await fetch('/api/chat/slots/import', {
        method: 'POST',
        headers: { 'Content-Type': 'application/octet-stream', ..._sk },
        body: file,
      })
      return (await j(r)) as {
        ok: boolean
        key: string
        title: string
        messages: number
        resume_mode: string
      }
    },
    sendSessionToInstance: (id: string, slot: string) =>
      post('/api/instances/' + encodeURIComponent(id) + '/send-session', { slot }).then(j) as Promise<{
        ok: boolean
        instance: string
        remote_key: string
        messages: number
        // '' when the peer is too old to report it — treated as unknown.
        resume_mode?: 'session_load' | 'prefix' | ''
      }>,
  }

  const peerReads = {
    // Federated session search across the local gateway + every CONNECTED remote
    // instance (backend rank-interleaves; remote rows carry instance_id/_name).
    // 403 = instances feature disabled — callers fall back to sessionsSearch.
    instancesSearchSessions: (q: string, limit = 50) => fetch('/api/instances/search-sessions?q=' + encodeURIComponent(q) + '&limit=' + limit).then(j),
    /** What a connected crew can do: its version, agent roster, model list, effort
     *  levels and workspaces. The per-instance counterpart to `/api/agents`,
     *  `/api/models`, `/api/effort-levels` and `/api/workspaces`, which are all
     *  same-origin reads of THIS machine — a session bound to a peer for execution
     *  must offer the peer's options, since picking a crew or model that only exists
     *  here would fail on the first send.
     *
     *  `version_match` is the gate the backend enforces on every dispatch, surfaced
     *  so the UI can explain a refusal before the user types rather than after.
     *  `unavailable` names the reads that failed, per field, so one unreachable
     *  roster disables exactly its own control instead of blanking the shelf. */
    instancesCapabilities: (instanceId: string) =>
      fetch('/api/instances/' + encodeURIComponent(instanceId) + '/capabilities').then(j) as Promise<RemoteCrewCapabilities>,

    // A CONNECTED remote instance's LIVE sessions, read through an owner-only,
    // GET-only hub route. NOT the generic instance proxy, which this first used: the
    // peer also lists the slots THIS hub drives for its own remote-EXECUTION
    // bindings (a local session with `executor: 'remote'`), and those must not come
    // back as peer rows or one conversation renders twice — once as the local row
    // the user can chat in, once as a read-only row pointing at the instance pane.
    // Only the gateway can tell them apart, because the correlating `remote_slot`
    // is deliberately never projected to the browser, so the dedupe lives there.
    // READ ONLY on purpose: remote rows offer no rename or close, because those are
    // local-slot operations that cannot reach a session on another machine — so no
    // peer mutation method is defined here either.
    // A remote instance's OLDER sessions are deliberately absent: they live under the
    // peer's /api/sessions, and the prefix row that would admit them would also admit
    // clear-all, session-restart, a memory read and a token-spending summarize.
    instanceChatSlots: (id: string) =>
      fetch('/api/instances/' + encodeURIComponent(id) + '/chat-slots').then(j),
  }

  return { registryAndTransfer, peerReads }
}
