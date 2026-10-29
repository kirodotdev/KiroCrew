# Hook execution correlation contract

Hook consumers can correlate execution records without depending on an
observability vendor. Kiro Crew supplies execution identities; a consumer decides
whether to turn them into logs, audit records, metrics, or traces.

The goal is to expose enough explicit context in hooks, workflow records, and
ACP prompt metadata for an external ACP tap and hook consumer to reconstruct
chat/workflow relationships without timing heuristics or response-text parsing.

This contract does not install a recorder, transport tap, hook runner, Collector,
or exporter. It does not generate OpenTelemetry trace/span IDs, configure an AWS
destination, or change the native metrics export path.

## Division of responsibility

| Producer or consumer | Responsibility |
|---|---|
| Kiro Crew hooks | Event identities and applicable session, turn, workflow, agent, and native tool-call identities. |
| Kiro Crew workflow records | Workflow/agent lifecycle, session bindings, and explicit execution relationships. |
| Kiro Crew ACP prompt metadata | The same execution context used by hooks, copied per prompt into `_meta.kirocrew`. |
| External ACP tap | Native protocol session/request/tool IDs, tool lifecycle and results, errors, and model/usage information actually exposed on the wire. |
| External adapter | Exact-ID joins, trace/span ID assignment, storage, export, and backend configuration. |

Hooks do not need to duplicate all ACP content. The tap does not replace missing
producer relationships: it cannot establish a chat-to-workflow parent by parsing
a launch response or selecting the closest timestamp.

## Event identity versus execution identity

`ScriptHookStore.fire()` adds `event_id` to every firing. A supplied value is
preserved; otherwise the store generates a UUID hex string. A caller deliberately
redelivering the same event must reuse its original `event_id`.

The other fields are optional: they appear when the producer supplies them.
Consumers must tolerate their absence, particularly for non-ACP providers,
non-chat entry points, and events without a tool or workflow scope.

| Field | Scope and meaning |
|---|---|
| `event_id` | One hook firing; use for event deduplication, not to group a whole turn. |
| `session_key` | The firing session's own key, distinct from its parent session. |
| `parent_session_key` | Parent session identity when the producer supplies one. |
| `turn_id` | One prompt execution; shared across that turn's applicable hooks and prompt metadata. |
| `tool_call_id` | Native identity of one logical tool call, shared by its start/result notifications. |
| `workflow_run_id` | One workflow execution, when applicable. |
| `launched_workflow_run_id` | Workflow execution explicitly created by this tool call; distinct from any enclosing run. |
| `agent_id` | One workflow agent call; interpret together with `workflow_run_id`. |
| `acp_session_id` | ACP execution session, which can be reused across multiple calls or turns. |
| `subagent_id` | Spawned subagent identity, distinct from a workflow agent call. |

Use composite identities where necessary. An ACP session alone does not identify
one workflow agent invocation, and a session key alone does not identify a turn.
Timestamps describe ordering and duration; they are not an execution identity.

## Required joins for complete correlation

The following table defines the structured joins required for complete coverage.

| Relationship | Required structured bridge |
|---|---|
| Chat hooks to ACP prompt | Identical own `session_key` and `turn_id` in hooks and prompt metadata. |
| ACP prompt to native notifications | Native ACP session and exact prompt context; tool lifecycle records retain their native `tool_call_id`. |
| Workflow agent to ACP prompt | Identical `workflow_run_id`, `agent_id`, and `turn_id` in the workflow execution context and prompt metadata. |
| Workflow agent to ACP session | Binding of run + agent invocation to native `acp_session_id`, with an unambiguous corresponding turn. |
| Chat/tool to launched workflow | The `workflow_run` MCP result carries `launched_workflow_run_id` in namespaced `structuredContent`; its ACP result hook carries that ID beside the native `tool_call_id`, own `session_key`, `turn_id`, and ACP session. |
| Workflow to completion chat turn | The completion turn carries the completed `workflow_run_id` in hook payloads and ACP prompt metadata, alongside its newly minted `turn_id`. |
| Child invocation to parent invocation | Explicit parent invocation or turn identity; a parent session alone is insufficient. |

An API/UI-launched workflow has that origin and no invented chat parent.
Completion injection creates a new chat turn caused by the workflow; it does not
reuse the initiating turn's identity. Consumers may represent causality as a
trace link rather than forcing every relationship into one parent tree.

JSON-RPC request IDs are scoped to a transport instance, not globally unique.
Native session and tool-call IDs must also retain their documented scope. An
external tap must not assign an ambiguous notification to the last active prompt
on a reused session. It must preserve an exact association or report the
relationship as unresolved.

## Consumer acceptance criterion

Using only structured hook payloads, workflow records, and ACP records, a test
consumer must reconstruct the supported execution relationships under concurrent
turns, named/pooled session reuse, rerun, failure, rejection, and cancellation.
It must use no timestamp proximity, response-text parsing, prompt/tool-content
matching, or tool-name matching to decide ownership.

Start/result records use the same scoped tool-call identity but different event
identities. Reruns are new executions, not a reuse of the previous run's identity.
Replayed history must not be presented as fresh execution. An incomplete
execution remains incomplete unless a producer supplies a terminal outcome.

An external tap can expose only what Kiro puts on the ACP wire. Aggregate turn
usage is not evidence of individual model-request boundaries; consumers must not
label inferred model calls as native request spans.

## Producer propagation

Ordinary non-slash dashboard chat forwards the turn's existing
`_recovery_turn_id` as `turn_id` and its own effective `session_key` through the
provider's optional metadata capability. Prompt, tool, and Stop hooks use that
same turn identity. Generic providers retain their existing stream path, and
slash commands retain their existing command path.

Tool hook producers forward their native tool-call ID and applicable turn,
workflow, agent, and ACP-session identities. Start and result are distinct events
with their own event IDs, joined through the execution and tool-call identities,
not through matching tool names or arguments.

The `workflow_run` tool returns its run identity as MCP `structuredContent`
under `kirocrew.launched_workflow_run_id`. The ACP result parser reads only that
explicit field from the adapter's structured `rawOutput`; it does not parse the
human-readable response. The PostToolUse hook keeps any enclosing
`workflow_run_id` separate from the newly launched run ID.

Workflow agents forward call-local `workflow_run_id`, `agent_id`, `turn_id`, and
`parent_session_key`. Named and pooled sessions report `agent_session_bound`
after obtaining their ACP session ID and before sending the prompt. Consumers
can join that event with the workflow agent using the run and agent IDs.
Providers without an ACP session ID omit the binding event.

The full workflow event envelope and ordering rules remain documented in
[workflows](../system-specs/modules/workflows.md#run-event-stream).

## ACP transport metadata

Direct and shared-runtime prompt paths accept an optional
`telemetry_context: Mapping[str, str]`. A non-empty mapping is copied into
`session/prompt.params._meta.kirocrew`. It contains identity strings, not prompt
blocks, tool arguments, or outputs.

Metadata is local to the send or active prompt record. It is not retained on a
reusable provider, named session, or pooled worker. Compaction prompts do not
inherit it, and correlation metadata does not change resume fingerprints.
See [ACP per-prompt identity metadata](../system-specs/modules/acp-client.md#per-prompt-identity-metadata).

## Compatibility and boundaries

The fields are additive. Existing hook matching, approval behavior, and hook
failure handling remain unchanged. The correlation contract does not introduce
additional prompt or tool-content capture.

Presence of identities is not a guarantee of hook delivery or tool completion.
If an execution is interrupted before a result hook, a consumer must not invent
a successful result. Workflow journals expose terminal outcomes separately.

Parent invocation relationships and terminal identity coverage still need a
separate audit against the required-joins table, including cancellation and
rejection. Consumers must continue to represent unsupported relationships as
unresolved rather than infer them from timestamps or session reuse.

For the hook payload details, see
[memory, skills, and hooks](../system-specs/modules/memory-skills-hooks.md).
