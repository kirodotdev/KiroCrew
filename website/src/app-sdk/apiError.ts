/**
 * The error the scoped `AppApi` JSON helper throws for a non-2xx response.
 *
 * Deliberately NOT re-exported from the app-sdk barrel: the vendor stub apps
 * resolve mirrors that barrel value-for-value, so a new export there is an
 * SDK surface change. Apps keep seeing the same `Error` with the same
 * `API <status>: <body>` message they always did; this subclass only lets
 * in-tree callers (the app-sdk send wire) read the body back WITHOUT parsing
 * the message string. The status is not kept: the shared receipt classifier
 * needs only "non-2xx" (which the throw itself proves) plus the body.
 */
export class AppApiError extends Error {
  /** The response body as text -- JSON when the server answered JSON. */
  readonly bodyText: string

  constructor(status: number, bodyText: string) {
    super(`API ${status}: ${bodyText}`)
    this.name = 'AppApiError'
    this.bodyText = bodyText
  }
}

/**
 * The error the scoped `AppApi` throws when the host app never granted the
 * path -- raised by the permission check BEFORE any request leaves the
 * document. Same message apps always saw; the type lets the send wire report
 * it as a refusal that names the missing grant instead of as a network fault
 * ("check your connection" is advice that can never succeed here).
 */
export class AppApiPermissionError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'AppApiPermissionError'
  }
}
