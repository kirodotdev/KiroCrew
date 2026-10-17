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
 *
 * It also carries Kiro Crew's MCP servers into the session. pi-acp accepts the
 * ACP `mcpServers` array and never hands it to pi, so the host writes the same
 * servers to a per-session handoff file in its owner-only artifact directory,
 * sealed with a key only this process's environment carries, and names it there
 * too. This extension reads and opens it once at load,
 * registers each server with pi's built-in MCP support, and refuses any call to
 * a Kiro Crew server name that pi resolved to a different server: a same-named
 * entry in pi's own `mcp.json` outranks an extension's registration.
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { createDecipheriv } from "node:crypto";
import { existsSync, readFileSync, unlinkSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";

/** Registered so the host can ask pi's command registry whether this file loaded. */
const PROBE_COMMAND = "kiro-crew-gate";

/** Envelope marker the ACP client keys on. */
const ENVELOPE_MARKER = "kiro-crew-gate";

/** The per-session nonce the host places in this process's environment. */
const NONCE_ENV = "KIROCREW_PI_GATE_SESSION";

/** Names the host's per-session MCP handoff file in this process's environment. */
const MCP_HANDOFF_ENV = "KIROCREW_PI_MCP_HANDOFF";

/** Marker the handoff file carries, beside the session nonce. */
const MCP_HANDOFF_MARKER = "kiro-crew-mcp";

/**
 * The hex AES-256-GCM key that opens THIS process's handoff file. The file sits in
 * a directory every pi session can read; the key is in this environment alone.
 */
const MCP_KEY_ENV = "KIROCREW_PI_MCP_KEY";

/** Open the host's seal: AES-256-GCM, the nonce as associated data, tag appended. */
function openSeal(record: Record<string, unknown>, nonce: string): unknown {
  const key = Buffer.from(process.env[MCP_KEY_ENV] ?? "", "hex");
  if (key.length !== 32 || typeof record.iv !== "string" || typeof record.sealed !== "string") {
    return null;
  }
  const iv = Buffer.from(record.iv, "base64");
  const sealed = Buffer.from(record.sealed, "base64");
  if (iv.length !== 12 || sealed.length < 16) return null;
  try {
    const decipher = createDecipheriv("aes-256-gcm", key, iv, { authTagLength: 16 });
    decipher.setAAD(Buffer.from(nonce, "utf8"));
    decipher.setAuthTag(sealed.subarray(sealed.length - 16));
    const plain = Buffer.concat([
      decipher.update(sealed.subarray(0, sealed.length - 16)),
      decipher.final(),
    ]);
    return JSON.parse(plain.toString("utf8"));
  } catch {
    return null;
  }
}

/** pi's agent directory override; `~/.pi/agent` otherwise. */
const AGENT_DIR_ENV = "PI_CODING_AGENT_DIR";

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

/** pi's tool-name prefix for a server: every character but [A-Za-z0-9_] becomes `_`. */
function mcpToolPrefix(server: string): string {
  return `mcp__${server.replace(/[^A-Za-z0-9_]/g, "_")}__`;
}

/** pi counts two server names that differ only in `-` and `_` as one server. */
function serverKey(server: string): string {
  return server.replace(/-/g, "_");
}

/** The servers the host handed this session, or null when it handed none. */
function readMcpHandoff(): Record<string, Record<string, unknown>> | null {
  const path = process.env[MCP_HANDOFF_ENV];
  if (!path) return null;
  let raw: string;
  try {
    raw = readFileSync(path, "utf8");
  } catch {
    return null;
  }
  // Read once. Where the artifact directory is writable to this process the file
  // goes now; inside Kiro Crew's sandbox that directory is read-only here, and
  // the host removes the file once the session is established.
  try {
    unlinkSync(path);
  } catch {
    // read-only mount: the host removes it
  }
  let body: unknown;
  try {
    body = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!body || typeof body !== "object") return null;
  const record = body as Record<string, unknown>;
  const nonce = process.env[NONCE_ENV] ?? "";
  if (record[MCP_HANDOFF_MARKER] !== 1 || !nonce) return null;
  // The session binding is the seal itself: this process's nonce is the
  // associated data, so a file sealed for any other nonce does not open.
  const opened = openSeal(record, nonce);
  if (!opened || typeof opened !== "object") return null;
  const servers = (opened as Record<string, unknown>).servers;
  if (!servers || typeof servers !== "object" || Array.isArray(servers)) return null;
  return servers as Record<string, Record<string, unknown>>;
}

/**
 * The server names pi's own `mcp.json` files declare, by `serverKey`, or null when
 * one exists and cannot be read -- which the caller treats as shadowing every
 * Kiro Crew server, since what it declares is unknown.
 */
function fileConfiguredServerKeys(cwd: string): Set<string> | null {
  const agentDir = process.env[AGENT_DIR_ENV] || join(homedir(), ".pi", "agent");
  const files = [join(agentDir.replace(/^~(?=\/|$)/, homedir()), "mcp.json")];
  // Project files load under project trust, which a user can grant a folder; a
  // project's same-named server would outrank Crew's exactly as a user-level one.
  for (let dir = cwd; ; dir = dirname(dir)) {
    files.push(join(dir, ".pi", "mcp.json"));
    if (dirname(dir) === dir) break;
  }
  const keys = new Set<string>();
  for (const file of files) {
    if (!existsSync(file)) continue;
    try {
      const parsed = JSON.parse(readFileSync(file, "utf8"));
      const servers = parsed && typeof parsed === "object" ? parsed.mcpServers : undefined;
      if (servers && typeof servers === "object") {
        for (const name of Object.keys(servers)) keys.add(serverKey(name));
      }
    } catch {
      return null;
    }
  }
  return keys;
}

export default function (pi: ExtensionAPI) {
  // Kiro Crew's servers by name, each with the tool-name prefix pi gives its tools.
  const crewServers = new Map<string, string>();
  // Kiro Crew server names whose tools this session must not run.
  const shadowed = new Set<string>();
  const handoff = readMcpHandoff();
  for (const [name, config] of Object.entries(handoff ?? {})) {
    crewServers.set(name, mcpToolPrefix(name));
    try {
      pi.registerMcpServer(name, config as never);
    } catch {
      // Another extension holds the name, or pi refused it: not Kiro Crew's server.
      shadowed.add(name);
    }
  }

  pi.on("session_start", async (_event, ctx) => {
    if (crewServers.size === 0) return;
    const registered = new Map(
      pi.getMcpServers().map((server) => [server.name, server] as const),
    );
    const fileKeys = fileConfiguredServerKeys(ctx.cwd);
    for (const name of crewServers.keys()) {
      const mine = registered.get(name);
      const expected = JSON.stringify(handoff?.[name] ?? null);
      if (!mine || JSON.stringify(mine.config) !== expected) shadowed.add(name);
      if (fileKeys === null || fileKeys.has(serverKey(name))) shadowed.add(name);
    }
    if (shadowed.size > 0 && ctx.hasUI) {
      ctx.ui.notify(
        `Kiro Crew tool gate: pi's own MCP config declares ${[...shadowed].join(", ")}, ` +
          "which outranks the server Kiro Crew placed, so those tools are refused. " +
          "Rename or remove that entry in pi's mcp.json.",
        "warning",
      );
    }
  });

  pi.registerCommand(PROBE_COMMAND, {
    description: "Kiro Crew's tool gate is loaded in this session",
    handler: async (_args, ctx) => {
      ctx.ui.notify("Kiro Crew tool gate: active", "info");
    },
  });

  pi.on("tool_call", async (event, ctx) => {
    const toolName = String(event.toolName ?? "tool");
    if (toolName.startsWith("mcp__")) {
      // Compared in pi's tool-name spelling on both sides, so a release that keeps
      // `-` in the server part is matched as well as one that writes `_`.
      const spelled = toolName.replace(/[^A-Za-z0-9_]/g, "_");
      for (const name of shadowed) {
        if (spelled.startsWith(crewServers.get(name) ?? mcpToolPrefix(name))) {
          return {
            block: true,
            reason: `Kiro Crew tool gate: "${name}" is not the server Kiro Crew placed in this session`,
          };
        }
      }
    }
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
