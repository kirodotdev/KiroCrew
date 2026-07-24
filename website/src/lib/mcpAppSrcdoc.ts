// MCP App (SEP-1865) srcdoc builder + CSP hardening for the sandboxed iframe.
//
// SECURITY MODEL
// ==============
// An MCP App resource is a FULL HTML5 document authored by the MCP server
// (not the LLM, not the user). Like widgetSrcdoc.ts we render it inside a
// null-origin sandboxed iframe (see McpAppFrame.tsx: sandbox="allow-scripts
// allow-forms", NO allow-same-origin) so it cannot touch the parent DOM,
// cookies, or storage. The app document is embedded verbatim — we do NOT
// re-parse/re-serialize it (that would risk mangling a 400KB+ author-tuned
// document); the only things we synthesize and inject are (1) the per-app CSP
// <meta> tag, whose value is fully sanitized here, and (2) a tiny trusted
// bridge-guard <script> (contains no secret; only signals navigation-start so
// the host retires the callback bridge — see buildMcpAppSrcdoc).
//
// CSP CAVEAT (honest note): a <meta http-equiv="Content-Security-Policy">
// tag is WEAKER than a header-delivered CSP — it cannot use some directives
// (frame-ancestors, sandbox, report-uri) and only binds from the point it is
// parsed. We therefore emit the meta BEFORE every untrusted byte of the app
// document (see buildMcpAppSrcdoc), so no author markup can parse ahead of
// the policy. Header enforcement for srcdoc is not available to us (there is
// no HTTP response for a srcdoc document), so meta injection is the accepted
// M1 approach and matches the existing widgetSrcdoc.ts model, which also
// relies on a <meta> CSP inside srcdoc. The hard isolation boundary is the
// sandboxed null-origin iframe, not the CSP; the CSP is defense-in-depth on
// top of that boundary.

/** Per-app CSP allowance sets sourced from the MCP App's resource metadata.
 * Every entry is an origin token that MUST be sanitized before use. */
export interface McpAppCsp {
  /** Extra hosts allowed for connect-src (fetch/XHR/WebSocket). */
  connectDomains?: string[]
  /** Extra hosts allowed for script/style/img/font/media (resource loads). */
  resourceDomains?: string[]
  /** Extra hosts allowed for frame-src (nested iframes). */
  frameDomains?: string[]
  /** Extra hosts allowed for base-uri. */
  baseUriDomains?: string[]
}

/** Permission grants requested by the app, mapped to iframe `allow` tokens.
 * Presence of the key (truthy object) means "requested"; the object body is
 * unused for M1. */
export interface McpAppPermissions {
  camera?: object
  microphone?: object
  geolocation?: object
  clipboardWrite?: object
}

/** The `mcp_app_render` WebSocket payload broadcast by the gateway. */
export interface McpAppRenderPayload {
  session_key: string
  tool_call_id: string
  server: string
  tool: string
  /** Full HTML5 document for the app (may be 400KB+). */
  html: string
  csp: McpAppCsp | null
  permissions: McpAppPermissions | null
  spool_id: string
  /** #418 callback capability, delivered ONLY over the owner-WS render frame.
   * The iframe relays it on every tools/call; the model-visible spool_id
   * authorizes nothing without it. */
  callback_secret?: string
  /** Optional structured tool result forwarded to the app via the
   * `ui/notifications/tool-result` notification. */
  structured_content?: unknown
  /** Originating tools/call arguments, forwarded via ui/notifications/tool-input. */
  tool_input?: unknown
  /** Originating tools/call result `content` array, forwarded via tool-result. */
  result_content?: unknown
}

// A CSP source token we accept: https:// only, an optional single `*.`
// wildcard-subdomain prefix, a DNS host (labels of [a-z0-9-], not starting or
// ending with a hyphen), and an optional :port. No path, no query, no scheme
// other than https. Case-insensitive host.
const DOMAIN_RE =
  /^https:\/\/(\*\.)?[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*(?::[0-9]{1,5})?$/i

/**
 * Sanitize a single CSP source token. Returns the trimmed token if it is a
 * safe `https://[*.]host[:port]` origin, else null. This is the CSP-injection
 * hardening boundary: any token containing a semicolon, quote, backtick,
 * comma, or whitespace (all of which could break out of one directive and
 * inject another) is rejected outright, and the remainder must match the
 * strict origin grammar above.
 */
export function sanitizeCspDomain(raw: unknown): string | null {
  if (typeof raw !== 'string') return null
  const trimmed = raw.trim()
  if (!trimmed) return null
  // Reject CSP-structural / quoting / whitespace characters before the grammar
  // check so a token like `https://ok.com ;script-src *` can never slip an
  // extra directive into the serialized policy.
  if (/[;'"`,\s]/.test(trimmed)) return null
  if (!DOMAIN_RE.test(trimmed)) return null
  return trimmed
}

/** Sanitize a domain list, dropping anything that fails validation. */
function sanitizeDomains(list: string[] | undefined): string[] {
  if (!Array.isArray(list)) return []
  const out: string[] = []
  for (const d of list) {
    const s = sanitizeCspDomain(d)
    if (s && !out.includes(s)) out.push(s)
  }
  return out
}

/**
 * Build the per-app Content-Security-Policy string from (sanitized) csp
 * metadata, following the MCP Apps SEP-1865 defaults. `resourceDomains` widen
 * script/style/img/font/media; `connectDomains` replace the default
 * `connect-src 'none'`; `frameDomains` replace `frame-src 'none'`;
 * `baseUriDomains` widen base-uri. When csp is null/omitted the strictest
 * default policy is returned.
 */
export function buildMcpAppCsp(csp: McpAppCsp | null | undefined): string {
  const resource = sanitizeDomains(csp?.resourceDomains)
  const connect = sanitizeDomains(csp?.connectDomains)
  const frame = sanitizeDomains(csp?.frameDomains)
  const base = sanitizeDomains(csp?.baseUriDomains)
  const join = (...parts: string[]) => parts.filter(Boolean).join(' ')
  const directives = [
    "default-src 'none'",
    join("script-src 'self' 'unsafe-inline'", ...resource),
    join("style-src 'self' 'unsafe-inline'", ...resource),
    join("img-src 'self' data:", ...resource),
    join("font-src 'self' data:", ...resource),
    join("media-src 'self' data:", ...resource),
    connect.length ? join('connect-src', ...connect) : "connect-src 'none'",
    frame.length ? join('frame-src', ...frame) : "frame-src 'none'",
    join("base-uri 'self'", ...base),
    // form-action does NOT fall back to default-src, and the iframe sandbox
    // grants allow-forms (apps may use <form> UI with JS interception).
    // Lock submission targets to 'none' so a form can never navigate/exfil
    // to an attacker origin. Not app-configurable in M1.
    "form-action 'none'",
  ]
  // Directives are ';'-joined; the CSP string itself never contains a double
  // quote, so it is safe to place inside content="…" without escaping.
  return directives.join('; ') + ';'
}

/**
 * Build the iframe `allow` attribute (Permissions Policy) from the app's
 * requested permissions. Returns '' when no permissions are requested.
 */
export function buildAllowAttribute(permissions: McpAppPermissions | null | undefined): string {
  if (!permissions) return ''
  const tokens: string[] = []
  // camera/microphone are DELIBERATELY not advertised: the app runs in a
  // null-origin (opaque) sandboxed iframe with no allow-same-origin, and
  // powerful capture features are gated to non-opaque secure origins — the
  // browser denies them regardless, so advertising them only misleads. Re-add
  // when apps are hosted on an isolated non-opaque origin (Milestone 2).
  if (permissions.geolocation) tokens.push('geolocation')
  if (permissions.clipboardWrite) tokens.push('clipboard-write')
  return tokens.join('; ')
}

/**
 * Produce the srcdoc for an MCP App iframe: the synthesized per-app CSP
 * <meta> followed by the app's full HTML5 document.
 *
 * INJECTION-ORDER GUARANTEE: the CSP meta is emitted BEFORE every untrusted
 * byte of the app document. We never splice into the untrusted HTML (a
 * previous revision injected after `<head>`, which let any markup the author
 * placed ahead of `<head>` — an <img>, <script>, or auto-submitting <form> —
 * parse and fire before the policy existed). The HTML parser hoists a
 * leading <meta> into the implicit <head>, enforces the CSP from that point
 * on, and then merges the app's own <html>/<head>/<body> structure per the
 * standard tree-construction rules (head-only elements such as <title>,
 * <meta>, <link>, <style> that appear later are still re-routed into the
 * head), so the author's document keeps working.
 *
 * The ONLY thing hoisted ahead of the meta is a LEADING doctype (anchored:
 * nothing but whitespace may precede it, and `[^>]*` cannot smuggle markup
 * past the closing `>`), because a doctype after other content is ignored
 * and would drop the document into quirks mode, breaking app CSS. A doctype
 * cannot load resources, so it is safe ahead of the policy.
 */
export function buildMcpAppSrcdoc(payload: McpAppRenderPayload): string {
  const csp = buildMcpAppCsp(payload.csp)
  const meta = `<meta http-equiv="Content-Security-Policy" content="${csp}">`
  // Trusted bridge-guard bootstrap (ours, not the app's). Emitted right after
  // the CSP meta so the policy governs it, and BEFORE any untrusted app byte
  // so its capture-phase listeners register first. On the original document's
  // pagehide/beforeunload — which fire at navigation START, before a
  // navigated-to page's <head> scripts run — it signals the host to retire the
  // callback bridge, closing the pre-`load` window in which a replacement
  // document could otherwise post a tools/call with the host's capability.
  // It contains no secret and only ever fires an invalidation; forging it is a
  // no-op-to-self-DoS, so no nonce is needed. `script-src` includes
  // 'unsafe-inline', so this inline script is permitted by the emitted CSP.
  const bridgeGuard =
    '<script>(function(){' +
    "function n(){try{parent.postMessage({__kirocrew_nav__:1},'*')}catch(e){}}" +
    "addEventListener('pagehide',n,{capture:true});" +
    "addEventListener('beforeunload',n,{capture:true});" +
    '})();</script>'
  const html = payload.html ?? ''

  const doctype = html.match(/^\s*<!doctype[^>]*>/i)
  if (doctype) {
    return doctype[0] + meta + bridgeGuard + html.slice(doctype[0].length)
  }
  return meta + bridgeGuard + html
}
