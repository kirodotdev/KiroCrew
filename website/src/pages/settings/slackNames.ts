/**
 * The three names a Slack app manifest gives the app (`slack_manifest.AppNames`):
 * the browser-side mirror of the server's checks, so the setup wizard and the
 * Slack panel refuse a bad name at the field. The server checks again on save.
 */

/** What a fresh install calls the app, its bot and its command (`DEFAULT_NAME`). */
export const DEFAULT_SLACK_NAME = 'kirocrew'

/** Slack's limits on the app name and the bot's display name. */
export const APP_NAME_MAX = 35
export const BOT_NAME_MAX = 80

/** Characters Slack names refuse here, as `slack_manifest._REFUSED_CATEGORIES`:
 *  controls, invisible format characters, surrogates, private use, unassigned. */
const REFUSED_NAME_CHAR = /[\p{Cc}\p{Cf}\p{Cs}\p{Co}\p{Cn}\u2028\u2029]/u

/** Whether *name* (as typed; empty means the default) is one the server takes:
 *  Slack's limit of *max* characters and no refused character. */
export function slackNameOk(name: string, max: number): boolean {
  const n = name.trim()
  return Array.from(n).length <= max && !REFUSED_NAME_CHAR.test(n)
}

/** Whether *command* (as typed, `/` optional; empty means the default) is one
 *  Slack accepts: lowercase letters, digits, `-` and `_`, 31 at most. */
export function slackCommandOk(command: string): boolean {
  const c = command.trim().replace(/^\//, '')
  return c === '' || /^[a-z0-9_-]{1,31}$/.test(c)
}
