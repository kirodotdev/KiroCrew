/**
 * Shared parsing for provider (pull-request / issue) API failures.
 *
 * The api client unwraps error envelopes into a human message, which discards
 * every other field; the raw body survives on `ApiError.body`, and this is the
 * single place that reads the machine-readable markers back out of it. Every
 * surface that renders a provider-mutation failure (the Changes panel actions,
 * review threads, Code Review Sage publishing) routes through here so a coded
 * refusal reads the same everywhere instead of only on the surface that
 * happened to be fixed first.
 */
export function pullRequestErrorDetails(error: unknown): {
  message: string
  /** The command to run: `gh auth login`, `glab auth login`, or `glab auth login --hostname <host>` for a self-managed GitLab. */
  loginCommand: string
  /** The server refused pending an acknowledgement the client may now offer. */
  confirmationRequired: boolean
  /** The gateway was at its concurrent-fetch ceiling; the same request may succeed later. */
  sourceBusy: boolean
} {
  let message = error instanceof Error ? error.message : String(error || '')
  let confirmationRequired = false
  let sourceBusy = false
  let hostScoped = ''
  // ApiError already unwraps the human message, which discards every other
  // field, so the structured marker is read from the raw body it preserves.
  const raw = typeof (error as { body?: unknown })?.body === 'string'
    ? (error as { body: string }).body
    : message
  try {
    const payload = JSON.parse(raw) as {
      error?: unknown
      confirmationRequired?: unknown
      code?: unknown
      loginCommand?: unknown
    }
    if (typeof payload.error === 'string') message = payload.error
    if (typeof payload.loginCommand === 'string' && payload.loginCommand.startsWith('glab auth login --hostname ')) {
      hostScoped = payload.loginCommand
    }
    confirmationRequired = payload.confirmationRequired === true
    sourceBusy = payload.code === 'source_busy'
  } catch {
    // Provider and network errors may already be plain text.
  }
  // `401 Unauthorized` / `HTTP 401` is GitLab's wording for an expired sign-in;
  // the server only appends a sign-in hint to a glab failure carrying it.
  const authenticationFailure = /\b(?:not logged in(?:to)?|unauthenticated|authentication (?:failed|required)|requires authentication|401 unauthorized|http 401)\b/i.test(message)
  // The bare command literals pass the i18n gate via the enumerated
  // `^(?:gh|glab) auth login$` exclusion in eslint.i18n.config.js — terminal
  // commands are wire strings, never display copy. A self-managed GitLab's
  // host-scoped command arrives as its own `loginCommand` field instead: the
  // gateway builds it from the allowlisted host, so nothing is parsed out of
  // provider-controlled message text.
  const loginCommand: string = hostScoped
    ? hostScoped
    : authenticationFailure && /(?:`|\b)gh auth login(?:`|\b)/i.test(message)
      ? 'gh auth login'
      : authenticationFailure && /(?:`|\b)glab auth login(?:`|\b)/i.test(message)
        ? 'glab auth login'
        : ''
  return { message, loginCommand, confirmationRequired, sourceBusy }
}
