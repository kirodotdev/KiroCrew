import { hiddenHtmlValues } from './detectOpenFileMarker'

/**
 * Whether an assistant reply offers the in-chat browser-support install card.
 *
 * The agent writes `<!-- kirocrew:browser-install -->` on its own line when the
 * `browser` tool reports that browsing is not set up on this host. Like the
 * open-file and preview markers, it is an HTML comment the transcript drops, so
 * the reader sees only the card.
 *
 * The marker only decides that the card is SHOWN. Nothing installs until the
 * user clicks Install, and that click goes through the same owner-gated
 * `POST /api/browser/install` the Settings -> Browser button uses. A marker the
 * reader can see as code (a fence, inline code) does not count.
 */
const MARKER_RE = /<!--\s*kirocrew:browser-install\s*-->/i

export function hasBrowserInstallMarker(content: string): boolean {
  return hiddenHtmlValues(content, 'kirocrew:browser-install').some(value => MARKER_RE.test(value))
}
