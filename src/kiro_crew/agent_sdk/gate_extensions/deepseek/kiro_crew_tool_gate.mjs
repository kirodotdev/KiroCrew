/**
 * Kiro Crew tool gate for the DeepSeek Harness (`dsh`).
 *
 * dsh decides its own tool calls: its sandbox permits an in-policy action
 * silently and denies an out-of-policy one with the denial inside the tool
 * result, and `session/request_permission` carries only a MODEL-INITIATED ask to
 * escalate past that sandbox. So a session runs every in-policy side effect
 * without Kiro Crew's PreToolUse gate ever executing. This plugin closes that:
 * it answers dsh's own `tools/pre-execute` waterfall with `{kind: 'ask'}` for
 * every call, passive reads included, which makes dsh's tools core resolve
 * the call through `ctx.approval.request` -- and the ACP bridge answers that
 * waterfall by emitting `session/request_permission` to the client.
 *
 * Fail-closed is dsh's OWN contract on both hops and is not re-implemented here:
 * `tools/pre-execute` documents that `ask` "runs only after an approval service
 * returns `allowed-once` and otherwise denies" (packages/core/tools), and
 * `ApprovalOutcome` is closed, with a missing, non-owning, throwing or
 * non-conforming answerer normalized to `unavailable` rather than to a grant (the
 * harness's own user-approval subsystem documentation).
 *
 * Composed by Kiro Crew through a per-launch patch file passed as
 * `dsh --profile acp --patch <file>`, never from the operator's own
 * configuration, and verified after load through the marker written below. The
 * marker also snapshots every root-bus `approval/request` answerer and the
 * composed approval policy, so loading this asker is not mistaken for owning
 * the decision route when another plugin can answer before the ACP bridge; the
 * tool presentation the tools service composed, which must be `native` for the
 * gate to read the calls it is asked about; and a PROOF, taken on a real child
 * spawned through the harness's own subprocess service, that each provider-key
 * name Kiro Crew feeds this harness is withheld from the shells the model drives.
 *
 * NO tool-call envelope, unlike the pi gate beside it. That one exists because
 * pi-acp can only forward a generic confirm dialog, so the tool call has to ride
 * inside the dialog's message. dsh streams the real ACP `tool_call` update --
 * `title`, `kind` and `rawInput` -- immediately BEFORE the permission request,
 * and its permission frame names the same `toolCallId`, which is the shape
 * Kiro Crew's dispatch already resolves tool input, shell identity and raw
 * params from. Writing a second copy into the frame would add a channel the
 * host does not need and a parser it already has.
 */

import { readFileSync, realpathSync, renameSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";

/** What Kiro Crew's load read-back looks for in the marker. */
const PROBE = "kiro-crew-tool-gate";

/** The waterfall whose sole answerer must be the ACP bridge. */
const APPROVAL_EVENT = "approval/request";

/** Absolute path Kiro Crew names for the marker, and the per-session nonce. */
const MARKER_ENV = "KIROCREW_DSH_GATE_MARKER";
const NONCE_ENV = "KIROCREW_DSH_GATE_SESSION";

/**
 * The provider-key names Kiro Crew feeds this harness, `:`-joined. Each is a POSIX
 * identifier (the harness's own credential-reference grammar), so the separator
 * cannot occur inside one. Kiro Crew sets every listed name in the PROBE's
 * environment to a canary rather than to the key -- the probe boots a plugin host
 * that needs no provider key -- because the property below can only be observed
 * for a name that is set: an absent name proves nothing about the scrub.
 */
const SCRUB_NAMES_ENV = "KIROCREW_DSH_GATE_SCRUB_NAMES";

/** Bound on the child-env check, well under Kiro Crew's read-back budget. */
const CHILD_CHECK_TIMEOUT_MS = 20_000;

export const name = PROBE;

/**
 * Every service this plugin needs, declared rather than probed.
 *
 * Cordis applies the plugin only once all are available, so `apply` RUNNING is
 * itself the evidence that dsh composed a tool runtime, an approval service and a
 * subprocess service -- which is what makes the marker below worth reading.
 * Without `approval` every gated call would resolve `unavailable` and deny, which
 * is safe but useless, and Kiro Crew would have no way to tell that apart from a
 * gate that works. `subprocess` is the seam the child-env proof spawns through:
 * the property under test belongs to THAT service (its `scrubbedParentEnv()` is
 * what every bash and terminal child starts from), so a child spawned any other
 * way would say nothing about it.
 */
export const inject = ["tools", "approval", "appReady", "subprocess"];

/**
 * Describe one Cordis listener by its owning loader entry and plugin runtime.
 *
 * Cordis exposes no public listener-enumeration API. Its root `_hooks` records
 * do retain the exact owning context, while callbacks are reflection proxies
 * with no stable source identity. Loader entry id + module and runtime name are
 * therefore the strongest identity available. Any Cordis shape change produces
 * null fields that Kiro Crew rejects instead of silently trusting the route.
 */
function approvalOwner(hook) {
  const fiber = hook?.ctx?.fiber;
  const entry = fiber?.entry;
  return {
    entry: typeof entry?.id === "string" ? entry.id : null,
    module: typeof entry?.options?.name === "string" ? entry.options.name : null,
    plugin: typeof fiber?.runtime?.name === "string" ? fiber.runtime.name : null,
  };
}

/** The complete approval route visible before any session is created. */
function approvalRouting(ctx) {
  const hooks = ctx.root?.events?._hooks?.[APPROVAL_EVENT];
  const policy = ctx.approval?.config?.policy;
  return {
    answerers: Array.isArray(hooks) ? hooks.map(approvalOwner) : null,
    // No session exists during plugin load, so only the composed default policy
    // is reachable here; a later per-session override can only narrow to `never`.
    policy: typeof policy === "string" ? policy : null,
  };
}

/**
 * The tool presentation the tools service COMPOSED, read off the service itself.
 *
 * Kiro Crew's patch pins `tools.mode: native`, and the pin holds because a
 * `--patch` overlay is applied after the operator layer -- an ordering observed at
 * one harness version. This reads what the service actually resolved
 * (`defaultMode`, the process-global presentation the `mode` row selects), so a
 * release that reorders the layers, or a plugin that rewrites the mode after
 * composition, is refused by Kiro Crew rather than trusted to precedence. A
 * per-agent `presentAs()` scope cannot exist yet at load and is not visible here.
 */
function toolsSnapshot(ctx) {
  const mode = ctx.tools?.defaultMode;
  return { mode: typeof mode === "string" ? mode : null };
}

/**
 * The harness's own version, read from the package the running entry script
 * belongs to. Recorded so a refusal or an accepted proof names the release it was
 * observed on; `null` when it cannot be read, which Kiro Crew records rather than
 * judges -- the property below is the decision, the version is its label.
 */
function harnessVersion() {
  try {
    const entry = realpathSync(process.argv[1] ?? "");
    const manifest = JSON.parse(readFileSync(join(dirname(entry), "..", "package.json"), "utf8"));
    return typeof manifest?.version === "string" ? manifest.version : null;
  } catch {
    return null;
  }
}

/** The `:`-joined name list, or `[]` when unset; blanks are dropped, order kept. */
function scrubNames() {
  const raw = process.env[SCRUB_NAMES_ENV] ?? "";
  return raw.split(":").filter((entry) => entry.length > 0);
}

/**
 * Prove, on a real child, which of `names` the harness withholds from its children.
 *
 * The child is spawned through `ctx.subprocess` -- the service every bash and
 * terminal tool of this harness spawns through, whose `childEnv()` starts from
 * `scrubbedParentEnv()` -- and does one thing: print the names in its own
 * environment. Node itself is the program, because it is the one executable this
 * process is certain to have (it is running on it) and it needs no shell. The
 * result is per name: which were not even set here (nothing verified for them)
 * and which reached the child (the key would reach the model's shells). Any
 * failure to run or read the child is recorded as `error`; Kiro Crew refuses on
 * every non-clean field, so nothing here has to decide.
 */
async function childEnvProof(ctx, names) {
  const proof = {
    version: harnessVersion(),
    names,
    parent_missing: names.filter((entry) => process.env[entry] === undefined),
    child_visible: null,
    error: null,
  };
  const controller = new AbortController();
  const timer = setTimeout(
    () => controller.abort(new Error(`child-env check exceeded ${CHILD_CHECK_TIMEOUT_MS}ms`)),
    CHILD_CHECK_TIMEOUT_MS,
  );
  try {
    const handle = ctx.subprocess.spawn({
      argv: [process.execPath, "-e", "process.stdout.write(JSON.stringify(Object.keys(process.env)))"],
      cwd: process.cwd(),
      stdio: { stdin: "ignore", stdout: { maxBytes: 1 << 20 }, stderr: { maxBytes: 64 << 10 } },
      graceMs: 5_000,
      signal: controller.signal,
    });
    const outcome = await handle.done;
    const read = handle.collected.stdout?.readFrom(0);
    if (outcome.exitCode !== 0 || read === undefined || read.lossy) {
      throw new Error(
        `child exited ${outcome.exitCode ?? outcome.signal}` +
          (read?.lossy ? " with lossy output" : "") +
          (read === undefined ? " with no collected stdout" : ""),
      );
    }
    const visible = JSON.parse(read.text);
    if (!Array.isArray(visible) || !visible.every((entry) => typeof entry === "string")) {
      throw new Error("child printed something other than its environment names");
    }
    const inChild = new Set(visible);
    proof.child_visible = names.filter((entry) => inChild.has(entry));
  } catch (error) {
    proof.error = error instanceof Error ? error.message : String(error);
  } finally {
    clearTimeout(timer);
  }
  return proof;
}

/**
 * Write the marker in one publish: the bytes land beside the path and are renamed
 * onto it, so Kiro Crew -- which polls for the file while this process is still
 * alive -- never reads a half-written one.
 */
function publishMarker(marker, body) {
  const staging = `${marker}.tmp`;
  writeFileSync(staging, JSON.stringify(body) + "\n", { encoding: "utf8" });
  renameSync(staging, marker);
}

export function apply(ctx) {
  // EVERY tool asks. There is no name allowlist, and that is deliberate on two
  // counts. A name list would have to spell this harness's passive reads, and a
  // scoped tool registered later under one of those names shadows the global one,
  // so the list would exempt an implementation nobody checked. And Kiro Crew's
  // PreToolUse gate is the ONLY governance point this harness has: its own sandbox
  // self-decides every in-policy action, so a call that never becomes a
  // permission frame is a call no rule of Kiro Crew's -- the denied-command floor,
  // the sensitive-path block -- ever sees. That holds for the reads as much as
  // for bash: the harness's own credential files stay masked for its whole
  // process tree (its key arrives from Kiro Crew's vault instead), but the read
  // gate is what stands in front of every OTHER sensitive path, and it only
  // stands there for a read that asks. A read that Kiro Crew judges benign is
  // auto-approved on its side, AFTER that check, which is where the decision
  // belongs.
  //
  // `prepend` so this runs ahead of listeners registered normally, and `next()`
  // is AWAITED rather than skipped: the downstream verdict is the harness's own
  // policy, and a `deny` from it is preserved as a deny. Only an `allow` is
  // upgraded to `ask`. Returning `ask` without awaiting would have discarded
  // every later deny listener, turning this harness's own refusal into a call
  // Kiro Crew is merely asked about.
  ctx.on(
    "tools/pre-execute",
    async (exec, next) => {
      const downstream = await next();
      const kind = downstream && typeof downstream === "object" ? downstream.kind : undefined;
      if (kind !== undefined && kind !== "allow") return downstream;
      const tool = String(exec?.name ?? "");
      return {
        kind: "ask",
        reason: `Kiro Crew's tool gate decides this ${tool} call`,
      };
    },
    { prepend: true },
  );

  // Scheduled only AFTER the gate listener is registered and run only once the
  // profile's readiness barrier says every loader entry has settled. Without
  // that barrier the gate can load before the ACP bridge and snapshot an empty
  // answerer set even though the bridge registers later in the same boot.
  //
  // Written ONLY when Kiro Crew named a marker path: the read-back probe does,
  // the session it speaks for does not, and a session without one runs the gate
  // exactly the same way. The barrier calls the listener without awaiting it, so
  // the async proof is wrapped to write a marker on EVERY outcome -- a rejection
  // escaping here would be an unhandled rejection, not a refusal Kiro Crew names.
  const marker = process.env[MARKER_ENV];
  if (!marker) return;
  return ctx.appReady.onReady(() => {
    const body = {
      plugin: PROBE,
      nonce: process.env[NONCE_ENV] ?? "",
      module: import.meta.url,
      approval: approvalRouting(ctx),
      tools: toolsSnapshot(ctx),
      child_env: null,
    };
    void childEnvProof(ctx, scrubNames())
      .then(
        (proof) => {
          body.child_env = proof;
        },
        (error) => {
          body.child_env = {
            version: harnessVersion(),
            names: scrubNames(),
            parent_missing: [],
            child_visible: null,
            error: error instanceof Error ? error.message : String(error),
          };
        },
      )
      .then(() => publishMarker(marker, body))
      .catch(() => {
        // The marker could not be published at all; Kiro Crew reads its absence as
        // a refusal, and there is nowhere else this plugin may report to.
      });
  });
}
