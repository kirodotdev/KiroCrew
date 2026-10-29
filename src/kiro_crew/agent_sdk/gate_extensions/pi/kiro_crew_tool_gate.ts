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
 * entry in pi's own `mcp.json` outranks an extension's registration. The file
 * also carries those servers' MCP instructions, which pi does not put in the
 * prompt for a directly declared server, so this extension appends them to the
 * system prompt of each run.
 *
 * And it carries a user's mid-turn message (a steer) into the running turn. pi-acp
 * has no steer verb, so the host writes each steer as its own file in the same
 * artifact directory, sealed with a key only this process's environment carries
 * and bound to this launch's nonce, this pi session's id and the steer's sequence
 * number. At each `turn_end` that did not abort or fail, this extension opens the
 * oldest steer it has not delivered and puts it on pi's steering queue with
 * `pi.sendMessage(message, { deliverAs: "steer" })`, which the agent loop reads
 * before its next model call in the same run and hands the model as a user message.
 * That call queues synchronously while pi is streaming; `pi.sendUserMessage` would
 * first await pi's input handlers, so its steer could miss that read and surface in
 * a later run, or start a run of its own once pi went idle. When pi starts the
 * message, this extension notifies an envelope naming the steer's sequence number,
 * which the host reads as "consumed". A steer still on disk when the run settles is
 * retired, never delivered later: the host queues it as the next turn.
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { createDecipheriv } from "node:crypto";
import { existsSync, readdirSync, readFileSync, statSync, unlinkSync } from "node:fs";
import { homedir } from "node:os";
import { basename, dirname, join } from "node:path";

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

/**
 * Names the host's steer channel for THIS launch: the artifact directory and the
 * file-name stem every steer file starts with (`<stem><10-digit seq>.json`).
 */
const STEER_ENV = "KIROCREW_PI_STEER";

/** The hex AES-256-GCM key that opens THIS launch's steer files, on this environment alone. */
const STEER_KEY_ENV = "KIROCREW_PI_STEER_KEY";

/** Marker a steer file and its consumed notification carry, beside the session nonce. */
const STEER_MARKER = "kiro-crew-steer";

/** A steer file's name after its stem: the zero-padded sequence number. */
const STEER_SEQ_RE = /^(\d{10})\.json$/;

/**
 * Larger than any steer the host writes: 64000 characters, UTF-8 (at most 4 bytes
 * each), sealed and base64'd.
 */
const MAX_STEER_FILE_BYTES = 512 * 1024;

/**
 * Open a host seal: AES-256-GCM under the hex key in `keyEnv`, `aad` as associated
 * data, tag appended. Null on any mismatch, so a forged or foreign file is ignored.
 */
function openSeal(record: Record<string, unknown>, aad: string, keyEnv: string): unknown {
  const key = Buffer.from(process.env[keyEnv] ?? "", "hex");
  if (key.length !== 32 || typeof record.iv !== "string" || typeof record.sealed !== "string") {
    return null;
  }
  const iv = Buffer.from(record.iv, "base64");
  const sealed = Buffer.from(record.sealed, "base64");
  if (iv.length !== 12 || sealed.length < 16) return null;
  try {
    const decipher = createDecipheriv("aes-256-gcm", key, iv, { authTagLength: 16 });
    decipher.setAAD(Buffer.from(aad, "utf8"));
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

/** A steer's associated data: the marker, this launch's nonce, the pi session, the sequence. */
function steerAad(nonce: string, sessionId: string, seq: number): string {
  return [STEER_MARKER, nonce, sessionId, String(seq)].join("\u0000");
}

type Steer = { seq: number; text: string };

/**
 * The steers the host wrote for THIS launch and THIS pi session with a sequence
 * number above `floor`, oldest first. A file is counted only when its name, its
 * body, and its seal all agree on the nonce, the session and the number; the seal
 * binds all three, so a file copied from another session or renamed does not open.
 */
function readSteers(sessionId: string, floor: number): Steer[] {
  const stemPath = process.env[STEER_ENV];
  const nonce = process.env[NONCE_ENV] ?? "";
  if (!stemPath || !nonce || !sessionId) return [];
  const dir = dirname(stemPath);
  const stem = basename(stemPath);
  let names: string[];
  try {
    names = readdirSync(dir);
  } catch {
    return [];
  }
  const out: Steer[] = [];
  for (const name of names) {
    if (!name.startsWith(stem)) continue;
    const match = STEER_SEQ_RE.exec(name.slice(stem.length));
    if (!match) continue;
    const seq = Number(match[1]);
    if (!Number.isSafeInteger(seq) || seq <= floor) continue;
    const path = join(dir, name);
    let record: Record<string, unknown>;
    try {
      if (statSync(path).size > MAX_STEER_FILE_BYTES) continue;
      const parsed = JSON.parse(readFileSync(path, "utf8"));
      if (!parsed || typeof parsed !== "object") continue;
      record = parsed as Record<string, unknown>;
    } catch {
      continue;
    }
    if (record[STEER_MARKER] !== 1 || record.nonce !== nonce || record.seq !== seq) continue;
    const opened = openSeal(record, steerAad(nonce, sessionId, seq), STEER_KEY_ENV);
    if (!opened || typeof opened !== "object") continue;
    const body = opened as Record<string, unknown>;
    if (body.seq !== seq || body.session !== sessionId || typeof body.text !== "string") continue;
    if (!body.text) continue;
    out.push({ seq, text: body.text });
  }
  return out.sort((a, b) => a.seq - b.seq);
}

/** The sequence number a steer message this extension queued carries, or null. */
function steerMessageSeq(message: unknown): number | null {
  const m = message as { role?: unknown; customType?: unknown; details?: unknown } | undefined;
  if (m?.role !== "custom" || m.customType !== STEER_MARKER) return null;
  const seq = (m.details as { seq?: unknown } | undefined)?.seq;
  return typeof seq === "number" && Number.isSafeInteger(seq) ? seq : null;
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

/** What the host handed this session: its servers and their MCP instructions. */
interface McpHandoff {
  servers: Record<string, Record<string, unknown>>;
  instructions: Record<string, string>;
}

/** The servers (and their instructions) the host handed this session, or null when it handed none. */
function readMcpHandoff(): McpHandoff | null {
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
  const opened = openSeal(record, nonce, MCP_KEY_ENV);
  if (!opened || typeof opened !== "object") return null;
  const servers = (opened as Record<string, unknown>).servers;
  if (!servers || typeof servers !== "object" || Array.isArray(servers)) return null;
  const instructions: Record<string, string> = {};
  const given = (opened as Record<string, unknown>).instructions;
  if (given && typeof given === "object" && !Array.isArray(given)) {
    for (const [name, text] of Object.entries(given)) {
      if (typeof text === "string" && text && name in servers) instructions[name] = text;
    }
  }
  return { servers: servers as Record<string, Record<string, unknown>>, instructions };
}

// Heads the section this extension adds to pi's system prompt. pi lists a server's
// MCP instructions only for servers it does not declare directly, and Kiro Crew
// declares its own directly, so the extension carries them in itself.
const MCP_INSTRUCTIONS_HEADING = "# MCP Server Instructions";
// Marks the section as this extension's own, so the once-guard below never
// mistakes an agent prompt that happens to carry the heading for it.
const MCP_INSTRUCTIONS_SENTINEL = "<!-- kiro-crew:mcp-instructions -->";

/** The system-prompt section for *instructions* of the servers not in *shadowed*, or "". */
function mcpInstructionsSection(instructions: Record<string, string>, shadowed: Set<string>): string {
  const parts = Object.entries(instructions)
    .filter(([name]) => !shadowed.has(name))
    .map(([name, text]) => `## ${name}\n${text}`);
  return parts.length === 0 ? "" : `${MCP_INSTRUCTIONS_SENTINEL}\n${MCP_INSTRUCTIONS_HEADING}\n\n${parts.join("\n\n")}`;
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
  for (const [name, config] of Object.entries(handoff?.servers ?? {})) {
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
      const expected = JSON.stringify(handoff?.servers[name] ?? null);
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

  // Each run's system prompt gets the instructions of the Kiro Crew servers this
  // session mounted, once: a prompt that already carries the section is left alone.
  pi.on("before_agent_start", async (event) => {
    const section = mcpInstructionsSection(handoff?.instructions ?? {}, shadowed);
    if (!section || event.systemPrompt.includes(MCP_INSTRUCTIONS_SENTINEL)) return;
    return { systemPrompt: `${event.systemPrompt}\n\n${section}` };
  });

  pi.registerCommand(PROBE_COMMAND, {
    description: "Kiro Crew's tool gate is loaded in this session",
    handler: async (_args, ctx) => {
      ctx.ui.notify("Kiro Crew tool gate: active", "info");
    },
  });

  // Steers. `steerFloor` is the highest sequence number that can no longer be
  // delivered: raised to each steer as it is handed to pi (so a file is delivered
  // once) and, when a run settles, to every steer still on disk (so a steer is
  // never delivered into a later turn than the one it was written for).
  let steerFloor = 0;
  // Sequence numbers of steers queued on pi and not yet started.
  const steersInFlight = new Set<number>();

  pi.on("turn_end", async (event, ctx) => {
    // An aborted or failed model call ends the run without reading pi's steering
    // queue, and a steer queued then would surface at the start of the NEXT run.
    const stop = (event.message as { stopReason?: unknown } | undefined)?.stopReason;
    if (event.outcome !== "completed" || stop === "error" || stop === "aborted") return;
    if (ctx.signal?.aborted || ctx.isIdle()) return;
    const next = readSteers(ctx.sessionManager.getSessionId(), steerFloor)[0];
    if (!next) return;
    steerFloor = next.seq;
    steersInFlight.add(next.seq);
    // Synchronous from the streaming check above to pi's steering queue: pi
    // converts a custom message to a user message for the model, and nothing
    // awaits before it is queued, so it is queued before this handler returns
    // and pi reads the queue.
    pi.sendMessage(
      {
        customType: STEER_MARKER,
        content: [{ type: "text", text: next.text }],
        display: true,
        details: { seq: next.seq },
      },
      { deliverAs: "steer" },
    );
  });

  pi.on("message_start", async (event, ctx) => {
    const seq = steerMessageSeq(event.message);
    if (seq === null || !steersInFlight.delete(seq)) return;
    if (ctx.hasUI) {
      ctx.ui.notify(
        JSON.stringify({ [STEER_MARKER]: 1, nonce: process.env[NONCE_ENV] ?? "", seq }),
        "info",
      );
    }
  });

  pi.on("agent_settled", async (_event, ctx) => {
    steersInFlight.clear();
    for (const steer of readSteers(ctx.sessionManager.getSessionId(), steerFloor)) {
      steerFloor = Math.max(steerFloor, steer.seq);
    }
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
