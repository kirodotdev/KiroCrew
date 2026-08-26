/**
 * Copyable request examples for the Webhooks page.
 *
 * Split out of `WebhooksPage.tsx` because everything here is RUNNABLE SHELL TEXT,
 * not user-visible copy: the output is meant to be pasted into a terminal and
 * executed. Translating a `curl` invocation, an `openssl` flag or a header name
 * would produce a snippet that fails, so this module is exempt from the i18n
 * string lint by path (see `eslint.i18n.config.js`) rather than by scattering
 * per-line suppressions through it. Keeping it in a separate file is what makes
 * that exemption narrow and auditable — the page itself stays fully translated.
 */

/** Wrap a value for safe use as a POSIX shell single-quoted word.
 *
 *  These snippets are meant to be copied and RUN, and the values interpolated
 *  into them are not ours: `sessionKey` comes from whatever id `register_hook`
 *  was called with (an agent, or whatever drove it), and the endpoint URL is
 *  derived from the request Host header. `JSON.stringify` escapes double quotes
 *  but not apostrophes, so a single `'` would close the shell quote and
 *  everything after it would be interpreted as command text.
 *
 *  The POSIX idiom is to end the quote, emit an escaped quote, and reopen:
 *  `it's` becomes `'it'\''s'`. There is no way to escape `'` inside single
 *  quotes, so this is the only correct form. */
function shellQuote(value: string): string {
  return `'${value.replaceAll("'", "'\\''")}'`
}

/** The gateway's own origin, taken from the endpoint URL.
 *
 *  `csrf_middleware` runs `check_origin` on every non-safe method with no path
 *  exemption, and it trusts a missing Origin only when the caller is loopback.
 *  The documented callers here are not loopback — CI runners, bots, deploy
 *  pipelines — so a snippet without this header is rejected with 403 before it
 *  ever reaches bearer/HMAC authentication. The gateway's own origin is in the
 *  allowed set, so echoing it back is the form that works. */
function originFor(url: string): string {
  const scheme = url.indexOf('://')
  if (scheme === -1) return '<gateway-url>'
  const afterScheme = url.slice(scheme + 3)
  const path = afterScheme.indexOf('/')
  return url.slice(0, scheme + 3) + (path === -1 ? afterScheme : afterScheme.slice(0, path))
}

/** The curl a caller would run. `<token>` is a placeholder on purpose — the real
 *  secret is never recoverable from this page. */
function curlFor(url: string, sessionKey: string, message: string): string {
  const target = url || '<gateway-url>/api/hooks/agent'
  const body = [
    '{',
    `    "message": ${JSON.stringify(message)},`,
    `    "sessionKey": ${JSON.stringify(sessionKey)},`,
    '    "name": "My Bot",',
    '    "deliver": true',
    '  }',
  ].join('\n')
  return [
    `curl -X POST ${shellQuote(target)}`,
    "  -H 'Authorization: Bearer <token>'",
    `  -H ${shellQuote(`Origin: ${originFor(target)}`)}`,
    "  -H 'Content-Type: application/json'",
    `  -d ${shellQuote(body)}`,
  ].join(' \\\n')
}

/** Which request example a pane shows. A token that requires signatures cannot
 *  be called with the bearer-only form at all, so the example follows the mode
 *  rather than presenting a snippet that would be rejected with 401. */
export type ExampleMode = 'signed' | 'bearer'

/** Runnable signing snippet. The body is held in one shell variable and sent
 *  with `--data-raw`, because the signature covers the RAW bytes — re-formatting
 *  the JSON between signing and sending is the one mistake that makes a
 *  correct-looking implementation always fail. */
function signedCurlFor(
  url: string, sessionKey: string, message: string, windowSeconds: number,
): string {
  const target = url || '<gateway-url>/api/hooks/agent'
  const body = JSON.stringify({ message, sessionKey, name: 'My Bot', deliver: true })
  return [
    '# Both secrets were shown once, when the token was generated.',
    "TOKEN='kc_whk_…'      # bearer token — proves who is calling",
    "SECRET='kc_whs_…'     # signing secret — proves the body was not tampered with",
    '',
    `BODY=${shellQuote(body)}`,
    'TS=$(date +%s)',
    '# The signed string is exactly "<timestamp>.<raw body>".',
    `SIG=$(printf '%s.%s' "$TS" "$BODY" \\`,
    '  | openssl dgst -sha256 -hmac "$SECRET" -hex | sed \'s/^.* //\')',
    '',
    `curl -X POST ${shellQuote(target)} \\`,
    '  -H "Authorization: Bearer $TOKEN" \\',
    `  -H ${shellQuote(`Origin: ${originFor(target)}`)} \\`,
    '  -H "X-KiroCrew-Timestamp: $TS" \\',
    '  -H "X-KiroCrew-Signature: sha256=$SIG" \\',
    "  -H 'Content-Type: application/json' \\",
    '  --data-raw "$BODY"',
    '',
    `# TS must be within ${windowSeconds} seconds of gateway time, and each`,
    '# signature is accepted only once per gateway process — a captured request',
    '# cannot be replayed inside the window unless the gateway restarts first.',
  ].join('\n')
}

export function exampleFor(
  mode: ExampleMode, url: string, sessionKey: string, message: string, windowSeconds: number,
): string {
  return mode === 'signed'
    ? signedCurlFor(url, sessionKey, message, windowSeconds)
    : curlFor(url, sessionKey, message)
}
