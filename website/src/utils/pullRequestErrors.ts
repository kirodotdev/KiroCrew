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
  loginCommand: 'gh auth login' | 'glab auth login' | ''
  /** The server refused pending an acknowledgement the client may now offer. */
  confirmationRequired: boolean
  /** The gateway was at its concurrent-fetch ceiling; the same request may succeed later. */
  sourceBusy: boolean
} {
  let message = error instanceof Error ? error.message : String(error || '')
  let confirmationRequired = false
  let sourceBusy = false
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
    }
    if (typeof payload.error === 'string') message = payload.error
    confirmationRequired = payload.confirmationRequired === true
    sourceBusy = payload.code === 'source_busy'
  } catch {
    // Provider and network errors may already be plain text.
  }
  const authenticationFailure = /\b(?:not logged in(?:to)?|unauthenticated|authentication (?:failed|required)|requires authentication)\b/i.test(message)
  // The command literals pass the i18n gate via the enumerated
  // `^(?:gh|glab) auth login$` exclusion in eslint.i18n.config.js — terminal
  // commands are wire strings, never display copy.
  const loginCommand = authenticationFailure && /(?:`|\b)gh auth login(?:`|\b)/i.test(message)
    ? 'gh auth login'
    : authenticationFailure && /(?:`|\b)glab auth login(?:`|\b)/i.test(message)
      ? 'glab auth login'
      : ''
  return { message, loginCommand, confirmationRequired, sourceBusy }
}
