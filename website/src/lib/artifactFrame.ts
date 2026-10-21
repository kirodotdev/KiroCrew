/**
 * Which frame an html or widget artifact opens in.
 *
 * Every artifact opens in the library's own frame, except a crewmate's weekly
 * report or monthly review (`goal_report`, set by the gateway when
 * `save_goal_report` files it). A report opens in the same sandbox as the
 * crewmate Dashboard page (pages/members/CrewDynamicDashboard.tsx): the grant
 * set `allow-scripts` and nothing else, and a document CSP without the CDN
 * script origins. The host's own injected scripts (height reporter, comment
 * bridge) are inline, so they keep working there.
 *
 * Keyed on the server-side field and never on a tag, because tags are free
 * text anyone can edit.
 */
import type { Artifact } from '../types'

/** The library frame's grants, unchanged for every artifact that is not a report. */
export const ARTIFACT_FRAME_SANDBOX = 'allow-scripts allow-popups allow-popups-to-escape-sandbox'

/** A report's grants: the crewmate Dashboard page's `CREW_DASHBOARD_SANDBOX`.
 * Pinned equal by artifactFrame.test.ts. */
export const REPORT_FRAME_SANDBOX = 'allow-scripts'

export interface ArtifactFrame {
  /** The iframe's `sandbox` attribute. */
  sandbox: string
  /** `buildSrcdoc`'s `offlineScripts`: drop the CDN script origins. */
  offlineScripts: boolean
  /** `buildSrcdoc`'s `rewriteBareLinks`: off where popups are withheld, since a
   * `_blank` link would then be a dead click. */
  rewriteBareLinks: boolean
}

export function artifactFrame(artifact: Pick<Artifact, 'goal_report'> | null | undefined): ArtifactFrame {
  if (artifact?.goal_report === true) {
    return { sandbox: REPORT_FRAME_SANDBOX, offlineScripts: true, rewriteBareLinks: false }
  }
  return { sandbox: ARTIFACT_FRAME_SANDBOX, offlineScripts: false, rewriteBareLinks: true }
}
