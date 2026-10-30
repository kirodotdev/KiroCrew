/**
 * Which frame an html or widget artifact opens in.
 *
 * Every artifact opens in the library's own frame, except a crewmate's weekly
 * report or monthly review (`goal_report`, set by the gateway when
 * `save_goal_report` files it). A report opens in the same sandbox as the
 * crewmate Dashboard page (pages/members/CrewDynamicDashboard.tsx): the grant
 * set `allow-scripts` and nothing else, and a document CSP without the CDN
 * script origins. A report is a static snapshot, so its own scripts never run:
 * the CSP admits only the host's injected scripts (height reporter, comment
 * bridge), by a nonce minted per document. Its links are inert, and a refresh
 * `meta`, a `link` hint or a `noscript` in the page is dropped. A report
 * therefore cannot navigate its frame anywhere, whatever the crewmate's egress
 * rules say.
 *
 * Keyed on the server-side field and never on a tag, because tags are free
 * text anyone can edit.
 */
import type { Artifact } from '../types'
import { CREW_DASHBOARD_SANDBOX } from './crewDashboardSandbox'
import { AGENT_PAGE_SANDBOX } from './widgetSrcdoc'

/** The library frame's grants for every artifact that is not a report: the
 * agent-page grant, with links leaving through the host's link bridge. */
export const ARTIFACT_FRAME_SANDBOX = AGENT_PAGE_SANDBOX

export interface ArtifactFrame {
  /** The iframe's `sandbox` attribute. */
  sandbox: string
  /** `buildSrcdoc`'s `offlineScripts`: drop the CDN script origins. */
  offlineScripts: boolean
  /** `buildSrcdoc`'s `linkBridge`: a web link opens through the host after a real
   * click. Off for a report, whose links are inert. */
  linkBridge: boolean
  /** `buildSrcdoc`'s `rewriteBareLinks`: off where popups are withheld, since a
   * `_blank` link would then be a dead click. */
  rewriteBareLinks: boolean
  /** `buildSrcdoc`'s `pageScripts`: false admits only the host's own scripts. */
  pageScripts: boolean
}

export function artifactFrame(artifact: Pick<Artifact, 'goal_report'> | null | undefined): ArtifactFrame {
  if (artifact?.goal_report === true) {
    return { sandbox: CREW_DASHBOARD_SANDBOX, offlineScripts: true, linkBridge: false, rewriteBareLinks: false, pageScripts: false }
  }
  return { sandbox: ARTIFACT_FRAME_SANDBOX, offlineScripts: false, linkBridge: true, rewriteBareLinks: false, pageScripts: true }
}
