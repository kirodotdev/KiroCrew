/**
 * The error the scoped `AppApi` throws when the host app never granted the
 * path -- raised by the permission check BEFORE any request leaves the
 * document. Same message apps always saw; the type lets the send wire report
 * it as a refusal that names the missing grant instead of as a network fault
 * ("check your connection" is advice that can never succeed here).
 */
export class AppApiPermissionError extends Error {
  /** The label the send wire's refusal row calls the app by -- the app's
   *  display name when the host knows one, else its id -- so "this app" is
   *  not a dead end for the user. */
  readonly appName: string
  constructor(message: string, appName: string) {
    super(message)
    this.name = 'AppApiPermissionError'
    this.appName = appName
  }
}
