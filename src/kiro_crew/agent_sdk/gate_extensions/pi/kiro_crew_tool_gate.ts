/**
 * Kiro Crew tool gate for the pi coding agent.
 *
 * pi runs every tool call without asking. pi-acp forwards a pi extension's
 * confirm dialog to the ACP client as `session/request_permission`. This
 * extension joins the two: every tool call raises a confirm dialog whose title is
 * the tool name and whose message is a JSON envelope carrying the call's own
 * identity and arguments, so the ACP client decides with the real input in hand
 * and its answer blocks or releases the call.
 *
 * Loaded by Kiro Crew on pi's command line (`--extension <this file>`), never
 * from the operator's own extension directories, and verified after load through
 * the probe command registered below.
 *
 * The envelope carries a per-session nonce Kiro Crew places in this process's
 * environment. The client accepts an envelope only with the nonce it issued, so
 * text that merely LOOKS like an envelope -- a model's tool arguments relayed by
 * some other dialog -- is read as the dialog it is, not as a tool call.
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

/** Registered so the host can ask pi's command registry whether this file loaded. */
const PROBE_COMMAND = "kiro-crew-gate";

/** Envelope marker the ACP client keys on. */
const ENVELOPE_MARKER = "kiro-crew-gate";

/** The per-session nonce the host places in this process's environment. */
const NONCE_ENV = "KIROCREW_PI_GATE_SESSION";

/** pi built-in tool name -> ACP tool-call kind. Unknown tools are `other`. */
const KIND_BY_TOOL: Record<string, string> = {
  bash: "execute",
  read: "read",
  write: "edit",
  edit: "edit",
  grep: "search",
  find: "search",
  ls: "search",
};

/**
 * Longest string VALUE forwarded intact under a key not named in WHOLE_KEYS. A
 * longer one is cut and marked, so the keys the host's path checks read --
 * `path`, `file_path` -- always survive, and the host treats the marked call's
 * arguments as untrusted. Only an envelope too large to carry at all is refused.
 */
const MAX_STRING_CHARS = 4000;
const MAX_ENVELOPE_CHARS = 200000;

/**
 * Argument keys forwarded WHOLE, per built-in tool: text the host judges on its
 * content and never on a cut. A shell command, because the deny rules read its
 * text verbatim and a command they did not see cannot be judged; a document body
 * (`write`'s `content`, `edit`'s two halves), because the host skips those keys in
 * its command-line scan only while the call's arguments are intact -- a cut body
 * would be read as a command line and an ordinary large write refused for quoting
 * a denied pattern. Spellings are pi's own tool schemas (`core/tools`).
 */
const WHOLE_KEYS: Record<string, ReadonlySet<string>> = {
  bash: new Set(["command"]),
  write: new Set(["content"]),
  edit: new Set(["oldText", "newText"]),
};

type Bounded = { value: unknown; truncated: boolean };

function bound(value: unknown, depth = 0): Bounded {
  if (typeof value === "string") {
    if (value.length <= MAX_STRING_CHARS) return { value, truncated: false };
    const omitted = value.length - MAX_STRING_CHARS;
    return {
      value: `${value.slice(0, MAX_STRING_CHARS)}…[${omitted} chars omitted by the Kiro Crew gate]`,
      truncated: true,
    };
  }
  if (Array.isArray(value)) {
    let truncated = false;
    const out = value.map((item) => {
      const b = bound(item, depth + 1);
      truncated = truncated || b.truncated;
      return b.value;
    });
    return { value: out, truncated };
  }
  if (value && typeof value === "object" && depth < 8) {
    let truncated = false;
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(value as Record<string, unknown>)) {
      const b = bound(v, depth + 1);
      truncated = truncated || b.truncated;
      out[k] = b.value;
    }
    return { value: out, truncated };
  }
  return { value, truncated: false };
}

/** Bound every value except the tool's WHOLE_KEYS strings, judged whole or not at all. */
function boundInput(toolName: string, input: unknown): Bounded {
  const whole = WHOLE_KEYS[toolName];
  if (!whole || !input || typeof input !== "object" || Array.isArray(input)) {
    return bound(input);
  }
  let truncated = false;
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(input as Record<string, unknown>)) {
    if (whole.has(k) && typeof v === "string") {
      out[k] = v;
      continue;
    }
    const b = bound(v, 1);
    truncated = truncated || b.truncated;
    out[k] = b.value;
  }
  return { value: out, truncated };
}

function envelope(toolCallId: unknown, toolName: string, input: unknown): string | null {
  const kind = KIND_BY_TOOL[toolName] ?? "other";
  const bounded = boundInput(toolName, input);
  const rendered = JSON.stringify({
    [ENVELOPE_MARKER]: 1,
    nonce: process.env[NONCE_ENV] ?? "",
    toolCallId: typeof toolCallId === "string" ? toolCallId : "",
    tool: toolName,
    kind,
    input: bounded.value,
    truncated: bounded.truncated,
  });
  // Still oversize after bounding (a shell command longer than the cap, or too
  // many keys to carry): refuse, because a deny rule cannot judge text it did
  // not see.
  return rendered.length > MAX_ENVELOPE_CHARS ? null : rendered;
}

export default function (pi: ExtensionAPI) {
  pi.registerCommand(PROBE_COMMAND, {
    description: "Kiro Crew's tool gate is loaded in this session",
    handler: async (_args, ctx) => {
      ctx.ui.notify("Kiro Crew tool gate: active", "info");
    },
  });

  pi.on("tool_call", async (event, ctx) => {
    const toolName = String(event.toolName ?? "tool");
    if (!ctx.hasUI) {
      // No dialog channel means no way to ask, so the call does not run.
      return { block: true, reason: "Kiro Crew tool gate: no permission channel" };
    }
    const message = envelope(event.toolCallId, toolName, event.input);
    if (message === null) {
      return { block: true, reason: "Kiro Crew tool gate: tool arguments too large to judge" };
    }
    const allowed = await ctx.ui.confirm(toolName, message);
    if (!allowed) {
      return { block: true, reason: "Denied by the Kiro Crew tool gate" };
    }
    return undefined;
  });
}
