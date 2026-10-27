#!/usr/bin/env node
'use strict';
/*
 * ACP tap (passive recorder). Sits between Kiro Crew and a real ACP adapter.
 *
 * Crew starts the adapter as `node <entry> ...`. Pointing CODEX_ACP_BIN at this file makes Crew start
 * this script instead. It starts the real adapter, copies stdin to the adapter and the adapter's stdout
 * to Crew UNCHANGED, and records only the SHAPE of each JSON-RPC message (field names and types, opaque
 * IDs, enumerations, token counts). It never records prompts, tool input, tool output or message text.
 *
 * Fail-open: any error in the recording code disables recording. The byte stream is never altered.
 * Output: $ACP_TAP_OUT (default ~/.kiro/otel-traces/acp-frames.jsonl). Disable with ACP_TAP_DISABLE=1.
 *
 * The packaged shim enables content capture unless ACP_TAP_CONTENT=0. It records content redacted
 * and bounded to ACP_TAP_MAX_FIELD
 * characters (default 8192): tool input (command, arguments), tool output (terminal output stream,
 * MCP result), the tool title, and the assistant's message text. Prompt capture is independent and
 * stores only redacted text after the configured marker; thought chunks and replayed user messages
 * are never recorded.
 */
const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawn } = require('child_process');

const OUT = process.env.ACP_TAP_OUT || path.join(os.homedir(), '.kiro', 'otel-traces', 'acp-frames.jsonl');
const DISABLED = process.env.ACP_TAP_DISABLE === '1';
const MAX_RECORDS = parseInt(process.env.ACP_TAP_MAX_RECORDS || '20000', 10);
const MAX_LINE = 8 * 1024 * 1024;
const MAX_DEPTH = 4;
const CONTENT = process.env.ACP_TAP_CONTENT === '1';
const PROMPTS = process.env.ACP_TAP_PROMPTS === '1';  // opt-in: also write prompt text to a separate file (see recordPrompt)
const PROMPTS_OUT = process.env.ACP_TAP_PROMPTS_OUT || path.join(os.homedir(), '.kiro', 'otel-traces', 'acp-prompts.jsonl');
const REQUESTED_MAX_PROMPT = parseInt(process.env.ACP_TAP_MAX_PROMPT || '8192', 10);
const MAX_PROMPT = Number.isFinite(REQUESTED_MAX_PROMPT)
  ? Math.max(1, Math.min(8192, REQUESTED_MAX_PROMPT))
  : 8192;
const MAX_PROMPT_RECORDS = parseInt(process.env.ACP_TAP_MAX_PROMPT_RECORDS || '500', 10);
const MAX_FIELD = parseInt(process.env.ACP_TAP_MAX_FIELD || '8192', 10);
const REQUESTED_MAX_PROMPT_CONTEXTS = parseInt(process.env.ACP_TAP_MAX_PROMPT_CONTEXTS || '500', 10);
const MAX_PROMPT_CONTEXTS = Number.isFinite(REQUESTED_MAX_PROMPT_CONTEXTS)
  ? Math.max(1, Math.min(500, REQUESTED_MAX_PROMPT_CONTEXTS))
  : 500;
// Each map keeps the 500 most recently set contexts; overflow drops inherited
// identity, never recovering it from time, names, or message content.
const REDACTIONS = [
  [/\b(AKIA|ASIA)[0-9A-Z]{16}\b/gi, '[REDACTED_AWS_KEY]'],
  [/(bearer\s+)[A-Za-z0-9._~+\/-]+=*/gi, '$1[REDACTED]'],
  [/\b(password|passwd|token|secret|api[_-]?key)\s*([:=])\s*([^\s,;"}]+)/gi, '$1$2[REDACTED]'],
];
function clean(value) {
  let text = typeof value === 'string' ? value : JSON.stringify(value);
  if (text === undefined) return '';
  for (const [re, rep] of REDACTIONS) text = text.replace(re, rep);
  return text.length > MAX_FIELD ? text.slice(0, MAX_FIELD) : text;
}
function contentOf(upd) {
  const c = {};
  const kind = upd.sessionUpdate;
  if (kind === 'tool_call') {
    if (upd.rawInput !== undefined) c.input = clean(upd.rawInput);
    if (upd.rawOutput !== undefined) c.output = clean(upd.rawOutput);
    if (typeof upd.title === 'string') c.title = clean(upd.title).slice(0, 200);
  } else if (kind === 'tool_call_update') {
    const m = upd._meta || {};
    if (m.terminal_output_delta && typeof m.terminal_output_delta.data === 'string') c.output_delta = clean(m.terminal_output_delta.data);
    if (upd.rawOutput !== undefined) c.output = clean(upd.rawOutput);
  } else if (kind === 'agent_message_chunk' && upd.content && typeof upd.content.text === 'string') {
    c.text = clean(upd.content.text);
  }
  return Object.keys(c).length ? c : undefined;
}
const SKIP = new Set(['rawInput', 'rawOutput', 'content', 'prompt', 'arguments', 'structuredContent', 'result', 'text']);
const ID_KEYS = new Set(['exit_code', 'signal', 'terminal_id', 'messageId', 'name', 'toolCallId', 'sessionId', 'session_id', 'session_key', 'sessionKey', 'parentSessionKey', 'tool_call_id', 'requestId', 'stopReason', 'status', 'kind',
  'workflow_run_id', 'agent_id', 'parent_session_key', 'evaluation_session_id', 'turn_id', 'hook_trace_id', 'hook_span_id',
  'sessionUpdate', 'protocolVersion', 'mode', 'modeId', 'optionId']);
const NUM_HINT = /token|count|duration|cost/i;
const CREW_HOME = process.env.KIROCREW_HOME || os.homedir();
const SESSION_MAP = path.join(CREW_HOME, 'session_map.json');
const TRACE_ROOT = process.env.KIRO_OTEL_TRACE_DIR || path.join(os.homedir(), '.kiro', 'otel-traces');
const HOOK_STATE = path.join(process.env.KIRO_OTEL_STATE_DIR || path.join(TRACE_ROOT, 'state-v2'), 'state.json');
const MAX_IDENTITY_FILE = 4 * 1024 * 1024;

function readIdentityFile(file) {
  try {
    const stat = fs.statSync(file);
    if (!stat.isFile() || stat.size > MAX_IDENTITY_FILE) return null;
    const value = JSON.parse(fs.readFileSync(file, 'utf8'));
    return value && typeof value === 'object' && !Array.isArray(value) ? value : null;
  } catch (e) { return null; }
}

function activeHookIdentity(acpSessionId) {
  const mapping = readIdentityFile(SESSION_MAP);
  if (!mapping) return null;
  let sessionKey = null;
  for (const [key, entry] of Object.entries(mapping)) {
    if (!entry || entry.sid !== acpSessionId) continue;
    if (sessionKey) return null; // Two owners of one ACP session are ambiguous.
    sessionKey = key;
  }
  if (!sessionKey || !sessionKey.startsWith('dashboard:')) return null;
  const sessions = readIdentityFile(HOOK_STATE)?.sessions;
  const turn = sessions && sessions[sessionKey] && sessions[sessionKey].turn;
  if (!turn || turn.session_key !== sessionKey) return null;
  const context = { session_key: sessionKey };
  if (typeof turn.turn_id === 'string' && turn.turn_id && turn.turn_id.length <= 80) {
    context.turn_id = turn.turn_id;
  }
  if (/^[0-9a-f]{32}$/.test(turn.trace_id || '') && /^[0-9a-f]{16}$/.test(turn.span_id || '')) {
    context.hook_trace_id = turn.trace_id;
    context.hook_span_id = turn.span_id;
  }
  return context.turn_id || context.hook_trace_id ? context : null;
}

let recording = !DISABLED;
let written = 0;
let dropped = 0;
let stream = null;

function openOut() {
  try {
    // One level only: a recursive mkdir on an odd path can loop, and a tap must never hang the adapter.
    const dir = path.dirname(OUT);
    if (!fs.existsSync(dir)) fs.mkdirSync(dir);
    stream = fs.createWriteStream(OUT, { flags: 'a', mode: 0o600 });
    stream.on('error', () => { recording = false; });
  } catch (e) { recording = false; }
}

let promptStream = null, promptsWritten = 0;
const activePromptContexts = new Map();
const promptContextsByRpcId = new Map();
// Crew puts the real request after this line, behind about 58K of its own system prompt and session context.
const PROMPT_MARKER = process.env.ACP_TAP_PROMPT_MARKER || '[CURRENT USER REQUEST -- respond to this]';
const PROMPTS_FULL = process.env.ACP_TAP_PROMPTS_FULL === '1';  // debugging: keep the whole text when there is no marker
function setBoundedContext(map, key, value) {
  if (map.has(key)) map.delete(key);
  map.set(key, value);
  while (map.size > MAX_PROMPT_CONTEXTS) map.delete(map.keys().next().value);
}
function recordPrompt(msg) {
  // Prompt text is sensitive (Crew's system prompt plus the user's words). It goes to its own 0600 file, never the frames file,
  // redacted and bounded, and only the part after the marker: the request itself. Without a marker nothing but the length is kept.
  try {
    if (promptsWritten >= MAX_PROMPT_RECORDS) return;
    const p = msg.params || {};
    const text = (Array.isArray(p.prompt) ? p.prompt : []).filter(b => b && typeof b.text === 'string').map(b => b.text).join('\n');
    const at = text.lastIndexOf(PROMPT_MARKER);
    let t = at >= 0 ? text.slice(at + PROMPT_MARKER.length).replace(/^\n/, '') : (PROMPTS_FULL ? text : '');
    for (const [re, rep] of REDACTIONS) t = t.replace(re, rep);
    if (!promptStream) { promptStream = fs.createWriteStream(PROMPTS_OUT, { flags: 'a', mode: 0o600 }); promptStream.on('error', () => { promptsWritten = MAX_PROMPT_RECORDS; }); }
    promptStream.write(JSON.stringify({ ts_ns: (BigInt(Date.now()) * 1000000n).toString(), pid: process.pid, requestId: msg.id, sessionId: p.sessionId, len: text.length,
      marker: at >= 0, truncated: t.length > MAX_PROMPT, text: t.slice(0, MAX_PROMPT) }) + '\n');
    promptsWritten++;
  } catch (e) { promptsWritten = MAX_PROMPT_RECORDS; }
}

function emit(rec) {
  if (!recording) return;
  if (written >= MAX_RECORDS) { dropped++; return; }
  try { stream.write(JSON.stringify(rec) + '\n'); written++; } catch (e) { recording = false; }
}

function shape(value, prefix, out, depth) {
  if (value === null) { out[prefix] = 'null'; return; }
  if (Array.isArray(value)) {
    out[prefix] = 'array[' + value.length + ']';
    if (depth < MAX_DEPTH && value.length) shape(value[0], prefix + '[]', out, depth + 1);
    return;
  }
  switch (typeof value) {
    case 'object':
      if (depth >= MAX_DEPTH) { out[prefix || '$'] = 'object'; return; }
      for (const k of Object.keys(value)) shape(value[k], prefix ? prefix + '.' + k : k, out, depth + 1);
      return;
    case 'string': out[prefix] = 'string[' + value.length + ']'; return;
    case 'number': out[prefix] = 'number'; return;
    case 'boolean': out[prefix] = 'bool'; return;
    default: out[prefix] = typeof value;
  }
}

function collect(value, ids, nums, depth) {
  if (!value || typeof value !== 'object' || depth > MAX_DEPTH) return;
  for (const k of Object.keys(value)) {
    if (SKIP.has(k)) continue; // never walk into user or tool content
    const v = value[k];
    if (ID_KEYS.has(k) && (typeof v === 'string' || typeof v === 'number')) ids[k] = String(v).slice(0, 80);
    else if (typeof v === 'number' && NUM_HINT.test(k)) nums[k] = v;
    else if (v && typeof v === 'object') collect(v, ids, nums, depth + 1);
  }
}

function record(dir, line) {
  if (!recording || line.length === 0 || line.length > MAX_LINE) return;
  let msg;
  try { msg = JSON.parse(line); } catch (e) { emit({ ts_ns: process.hrtime.bigint().toString(), pid: process.pid, dir, kind: 'unparsed', len: line.length }); return; }
  try {
    const hasMethod = typeof msg.method === 'string';
    const hasId = msg.id !== undefined && msg.id !== null;
    const kind = hasMethod ? (hasId ? 'request' : 'notification') : (hasId ? 'response' : 'other');
    const fields = {}; const ids = {}; const nums = {};
    shape(msg.params !== undefined ? msg.params : (msg.result !== undefined ? msg.result : msg.error), '', fields, 0);
    collect(msg.params || msg.result || msg.error, ids, nums, 0);
    if (PROMPTS && dir === 'client_to_agent' && msg.method === 'session/prompt') recordPrompt(msg);
    const sessionId = ids.sessionId;
    const rpcKey = hasId ? `${process.pid}:${String(msg.id)}` : null;
    if (dir === 'client_to_agent' && msg.method === 'session/prompt' && sessionId) {
      let identitySource = null;
      if (!ids.turn_id && !ids.session_key && !ids.workflow_run_id && !ids.agent_id && !ids.parent_session_key
          && !ids.hook_trace_id && !ids.hook_span_id) {
        const hookIdentity = activeHookIdentity(sessionId);
        if (hookIdentity) {
          Object.assign(ids, hookIdentity);
          identitySource = 'active_hook_state';
        }
      }
      const context = Object.fromEntries(['turn_id', 'hook_trace_id', 'hook_span_id', 'workflow_run_id', 'agent_id', 'session_key', 'sessionKey', 'session_id', 'parent_session_key', 'parentSessionKey']
        .filter((key) => ids[key]).map((key) => [key, ids[key]]));
      setBoundedContext(activePromptContexts, `${process.pid}:${sessionId}`, { requestId: String(msg.id), context });
      setBoundedContext(promptContextsByRpcId, rpcKey, { sessionId, context });
      if (identitySource) ids.identity_source = identitySource;
    } else {
      const promptContext = dir === 'agent_to_client' && kind === 'response'
        ? promptContextsByRpcId.get(rpcKey)
        : activePromptContexts.get(`${process.pid}:${sessionId}`);
      if (promptContext) Object.assign(ids, promptContext.context);
      if (dir === 'agent_to_client' && kind === 'response' && promptContext) {
        const key = `${process.pid}:${promptContext.sessionId}`;
        const active = activePromptContexts.get(key);
        if (active && active.requestId === String(msg.id)) activePromptContexts.delete(key);
        promptContextsByRpcId.delete(rpcKey);
      }
    }
    const upd = msg.params && msg.params.update;
    if (upd && upd.rawInput && typeof upd.rawInput === 'object') { // MCP identity only
      if (typeof upd.rawInput.server === 'string') ids.server = upd.rawInput.server.slice(0, 60);
      if (typeof upd.rawInput.tool === 'string') ids.tool = upd.rawInput.tool.slice(0, 80);
    }
    // Kiro CLI dialect: the tool name is in _meta.kiro.toolName; credits are in meteringUsage (no token counts)
    const km = upd && upd._meta && upd._meta.kiro;
    if (km && typeof km.toolName === 'string' && !ids.name) ids.name = km.toolName.slice(0, 80);
    const mu = msg.params && Array.isArray(msg.params.meteringUsage) ? msg.params.meteringUsage : null;
    if (mu) {
      const vals = mu.filter((m) => m && typeof m.value === 'number');
      if (vals.length) nums.meteringCredits = Math.round(vals.reduce((a, m) => a + m.value, 0) * 1e6) / 1e6;
      if (mu[0] && typeof mu[0].unit === 'string') ids.meteringUnit = mu[0].unit.slice(0, 20);
    }
    const content = CONTENT && upd && typeof upd === 'object' ? contentOf(upd) : undefined;
    emit({ ts_ns: (BigInt(Date.now()) * 1000000n).toString(), pid: process.pid, dir, kind, method: msg.method || null,
      id: hasId ? msg.id : null, update: upd && upd.sessionUpdate ? upd.sessionUpdate : null, ids, nums, fields,
      content, error: msg.error ? { code: msg.error.code } : undefined });
  } catch (e) { recording = false; }
}

function framer(dir) {
  let buf = Buffer.alloc(0);
  return (chunk) => {
    if (!recording) return;
    try {
      buf = Buffer.concat([buf, chunk]);
      let idx;
      while ((idx = buf.indexOf(0x0a)) !== -1) {
        const line = buf.subarray(0, idx).toString('utf8').replace(/\r$/, '');
        buf = buf.subarray(idx + 1);
        if (line.trim()) record(dir, line);
      }
      if (buf.length > MAX_LINE) buf = Buffer.alloc(0);
    } catch (e) { recording = false; }
  };
}

function findReal() {
  const tried = [];
  if (process.env.ACP_TAP_REAL) return process.env.ACP_TAP_REAL;
  const self = fs.realpathSync(__filename);
  const cands = [path.join(path.dirname(process.execPath), '..', 'lib', 'node_modules', '@agentclientprotocol', 'codex-acp', 'dist', 'index.js')];
  for (const dir of (process.env.PATH || '').split(path.delimiter)) if (dir) cands.push(path.join(dir, 'codex-acp'));
  for (const c of cands) {
    try {
      const real = fs.realpathSync(c);
      if (real !== self && fs.statSync(real).isFile()) return real;
    } catch (e) { tried.push(c); }
  }
  return null;
}

function main() {
  // ACP_TAP_REAL_BIN: run an executable directly (for example `kiro-cli`, started by Crew as `<bin> acp --agent ...`)
  const realBin = (process.env.ACP_TAP_REAL_BIN || '').trim();
  const real = realBin || findReal();
  if (!real) {
    process.stderr.write('acp-tap: could not find the real codex-acp adapter; set ACP_TAP_REAL to its entry script\n');
    process.exit(127);
  }
  if (recording) {
    openOut();
    emit({ ts_ns: (BigInt(Date.now()) * 1000000n).toString(), pid: process.pid, dir: 'tap', kind: 'process_start', ppid: process.ppid, content_mode: CONTENT,
      cwd: path.basename(process.cwd()), real, argv: process.argv.slice(2).map((a) => a.slice(0, 40)),
      env_names: Object.keys(process.env).filter((k) => /^KIROCREW_|^CODEX_|^ACP_/.test(k)).sort() });
  }
  const child = spawn(realBin ? realBin : process.execPath, realBin ? process.argv.slice(2) : [real, ...process.argv.slice(2)], { stdio: ['pipe', 'pipe', 'inherit'], env: process.env });
  child.on('error', (e) => { process.stderr.write('acp-tap: failed to start adapter: ' + e.message + '\n'); process.exit(127); });

  const inTap = framer('client_to_agent');
  const outTap = framer('agent_to_client');
  process.stdin.on('data', inTap);
  child.stdout.on('data', outTap);
  process.stdin.pipe(child.stdin);
  child.stdout.pipe(process.stdout);
  child.stdin.on('error', () => {});
  process.stdout.on('error', () => {});
  process.stdin.on('end', () => { try { child.stdin.end(); } catch (e) {} });

  for (const sig of ['SIGTERM', 'SIGINT', 'SIGHUP']) process.on(sig, () => { try { child.kill(sig); } catch (e) {} });
  child.on('exit', (code, signal) => {
    if (recording) emit({ ts_ns: (BigInt(Date.now()) * 1000000n).toString(), pid: process.pid, dir: 'tap', kind: 'process_exit', code, signal, dropped });
    const finish = () => process.exit(signal ? 128 + ({ SIGINT: 2, SIGTERM: 15, SIGHUP: 1 }[signal] || 1) : (code === null ? 1 : code));
    const done = () => (stream ? stream.end(finish) : finish());
    // let the adapter's last output drain to Crew before we exit
    if (child.stdout.readableEnded) done(); else child.stdout.on('end', done);
    setTimeout(done, 1500).unref();
  });
}

main();
