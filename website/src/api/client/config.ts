/**
 * Configuration endpoints: the kiro agent config, the default agent, the
 * kirocrew config read/save/patch, the ACP backend probes behind
 * `agent.acp_backend`, and the dashboard settings blob.
 */

import type { ClientTransport } from './transport'

/**
 * Whether THIS MACHINE has an ACP backend's components installed.
 *
 * `'unknown'` means the check itself failed, and it is NOT a synonym for
 * `'missing'`: telling someone to install what they may already have costs them a
 * global package install for nothing. Consumers must keep the three apart.
 */
export type AcpBackendInstalled = 'installed' | 'missing' | 'unknown'

/**
 * One row of `GET /api/acp-backends` — a backend the code knows about, paired
 * with what this build can serve and what this machine actually has.
 *
 * `selectable` is a BUILD/edition-and-policy fact (the same set
 * `PATCH /api/config/kirocrew` validates against); `installed` is a machine fact.
 * They are independent: a build can serve a backend whose binary is absent, and a
 * machine can hold a backend this build refuses to run.
 *
 * `missing_components` is non-empty only when `installed === 'missing'`;
 * `install_command` is `''` when there is nothing to suggest.
 */
export interface AcpBackendProbe {
  id: string
  policy_id: string
  selectable: boolean
  /** Explicit gateway capability: this harness can complete setup without Kiro CLI. */
  independent_setup: boolean
  installed: AcpBackendInstalled
  missing_components: string[]
  install_command: string
  /**
   * Installed on disk, but the RUNNING gateway already resolved its absence and
   * cached that for the process's life — so a session started now would still
   * fail. Disables the option and says restart, rather than offering a control
   * that is guaranteed to error.
   */
  restart_required: boolean
  /**
   * How this harness gets its credential, and what to tell an operator who has
   * not given it one. OPTIONAL because a gateway that predates this field sends
   * no `auth` at all, and the panel already treats absent probe information as
   * "say nothing, gate nothing".
   *
   * `sign_in_remedy` is a complete sentence rendered VERBATIM: the server owns
   * the wording, so it carries no placeholder to interpolate and is not
   * translated here. `signs_in_separately` is what decides whether the sentence
   * is shown at all -- a harness authenticating through Crew's own identity
   * store has no separate sign-in to finish.
   */
  auth?: {
    sign_in_remedy: string
    signs_in_separately: boolean
  }
  /**
   * The capability card: what this harness can do, projected on the server from
   * the capability memberships it already declared
   * (`agent_sdk/backend_cards.py`). OPTIONAL, like `auth`, because a gateway
   * that predates it sends none and the panel says nothing about what it was
   * not told.
   *
   * A LIST and not a map, so the SERVER owns the order: a new line appears in
   * the right place with no edit here. `id` is a stable machine key and the
   * LABEL is this frontend's, which is what makes the labels translatable at
   * all -- a label is written once per capability and every harness reuses it.
   * An id with no label here is SKIPPED rather than rendered raw, the opposite
   * of the `policy_id` fallback for a name: a bare `private_memory_mcp` in
   * front of a reader is worse than one line fewer, while a chip with no text
   * at all is worse than a policy id.
   *
   * `available` is a BOOL on every line, including an unmeasured one, where it is
   * false: a reader that ignores `measured` gets the fail-closed not-available
   * answer and never a promise. `measured` is absent from a gateway that predates
   * the third state, which is why the panel tests it for `=== false` rather than for
   * falsiness -- absent means measured, the two-level answer that gateway is sending.
   * `unmeasured_reason` is a machine CODE (`no_driven_capture`), labelled in the
   * panel like every other id on the card, and is `''` on a measured line.
   */
  capabilities?: {
    id: string
    available: boolean
    measured?: boolean
    unmeasured_reason?: string
  }[]
  /**
   * Ids of the SECURITY notes that hold for this harness: which layer confines
   * the agent, whether Crew hands its own credential to the child, how an
   * unclassifiable approval is answered.
   *
   * A separate list from `operator_notes` because the panel renders them in two
   * places. These go OUTSIDE every disclosure, beside `tool_approval`: "Crew's
   * sandbox is not confining this child" is as material as how the harness is
   * made to ask, and a fact behind a closed disclosure is one an operator
   * comparing harnesses does not see. The split is the SERVER's, so this frontend
   * cannot promote or bury a note by accident.
   */
  security_notes?: string[]
  /**
   * Ids of the where-it-lives notes that hold: whose disk holds the transcript,
   * which side supplies the model list, which channel carries a command. Ids
   * only, because a note is either raised or absent -- "this harness does not
   * relocate its home on a pod" is not a line anyone reads.
   */
  operator_notes?: string[]
  /**
   * How this harness is made to ask before it runs a tool: the core's own
   * `Routing` value, as the string. The one GRADED line on the card, and the
   * reason the card is otherwise two-level -- a membership set carries one bit,
   * while this enum already names five mechanisms and one "not established".
   */
  tool_approval?: string
  /**
   * Whether the BUILD offers this harness as a choice, before deployment policy
   * narrows anything. False means known-but-never-offered, which is a different
   * state from policy-denied and is the one the panel shows rather than hides:
   * a policy denial is not the reader's to fix, and a build exclusion is a
   * standing fact the tool-approval line explains.
   */
  offered_by_build?: boolean
  /**
   * What switching to this harness COSTS the reader, from the mirror declarations
   * `providers/mirrors/registry.py` already carries (`agent_sdk/backend_mcp_ability.py`).
   * OPTIONAL, like every other card field, because a gateway that predates it sends
   * none.
   *
   * A user-consequence subset rather than a projection dump: a line is here only where
   * switching costs a feature, adds a risk, or makes one of the reader's own agent-file
   * settings ineffective. Which ROUTE Crew takes to the harness -- native, mirror,
   * external -- is deliberately absent: it is true, and it costs the reader nothing.
   * `kirocrew doctor` states it for the reader diagnosing a route.
   *
   * `per_tool_deny` is `PerToolDeny`'s value (`settings-file` / `per-call` /
   * `whole-server`), `''` where the declaration carries none, and
   * `costs_whole_server` is the server's own classification of it: `true` where
   * switching ONE tool off can cost a whole server rather than that tool, which is
   * `whole-server` and `per-call` alike. The panel renders the rule only where that
   * flag holds, and the `per-call` exception under it.
   *
   * `ineffective` lists the spec settings that will not take effect here, as one list
   * however the core ruled them -- a withhold is a settled decision, a no-channel an
   * open gap, and both answer the reader's one question the same way. Stated only where
   * they hold, so a harness that honours the whole file renders nothing.
   *
   * It is ADVISORY. Per-tool MCP deny is not a requirement on every provider: a harness
   * with no per-call deny channel withholds the whole server instead, and this is where
   * it says so before a session runs. Nothing here refuses a selection.
   */
  mcp?: {
    per_tool_deny: string
    costs_whole_server?: boolean
    ineffective: string[]
  }
}

export function createConfigEndpoints({ post, put, j }: ClientTransport) {
  const settings = {
    // Agent config
    agentConfig: () => fetch('/api/agent/config').then(j),
    saveAgentConfig: (config: object) => put('/api/agent/config', { config }).then(j),
    defaultAgent: () => fetch('/api/config/default-agent').then(j),
    setDefaultAgent: (agent: string) => put('/api/config/default-agent', { agent }).then(j),
    kirocrewConfig: () => fetch('/api/config/kirocrew').then(j),
    saveKirocrewConfig: (agent: object) => put('/api/config/kirocrew', { agent }).then(j) as Promise<{ ok?: boolean; restart_required?: boolean; error?: string }>,
    patchConfig: (path: string, value: unknown) => fetch('/api/config/kirocrew', { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ path, value }) }).then(j),
    // Owner-only, and absent (404) on an older gateway. Both of those reach the
    // caller as a rejection, which is the intended signal: "no probe information",
    // to be treated as fail-open rather than as a verdict.
    acpBackends: () => fetch('/api/acp-backends').then(j) as Promise<{ backends: AcpBackendProbe[] }>,
    // Re-take ONE backend's verdict with this gateway's cached absence dropped first,
    // and answer with that backend's row in the shape `acpBackends` sends -- so the
    // caller splices it into the list it already holds rather than keeping a second
    // notion of a row. A POST, because it mutates spawn-path state; the GET above can
    // only report the divergence, which is what `restart_required` says.
    acpBackendRecheck: (backend: string) => post('/api/acp-backends/recheck', { backend }).then(j) as Promise<{ backend: AcpBackendProbe }>,
  }

  const dashboardRead = {
    // Dashboard config
    dashboardConfig: () => fetch('/api/dashboard/config').then(j),
  }

  const dashboardWrite = {
    updateDashboardConfig: (body: object) => put('/api/dashboard/config', body).then(j),
  }

  return { settings, dashboardRead, dashboardWrite }
}
