/**
 * The feedback tracker: where the dashboard sends a user who wants to file
 * something.
 *
 * Three surfaces point at the project's issue tracker: the top bar's "Request
 * a Feature" (a hidden prompt that walks the agent through drafting and filing
 * the request, plus the non-inference exit the `usage_limit` row shows when
 * the plan is spent, #13342), the prerelease "report it" link in Settings ›
 * About, and the false-positive report a redaction card offers. The stock
 * build points all of them at the public GitHub repository and tells the agent
 * to file with `gh`.
 *
 * A downstream edition may run where that tracker is unreachable, and then
 * every one of those surfaces dead-ends: the seeded prompt tells the agent to
 * search and file on GitHub, `gh` fails, and the agent improvises a fallback
 * nobody designed. This module is the seam that retargets them.
 *
 * Like `setProductName`, it is an **exported setter, not a registry**: the core
 * consumes the value itself, there is nothing to enumerate, and a registry the
 * core never iterates would add stringly-typed surface for no composition
 * benefit. The edition calls `setFeedbackTracker(...)` from its composition
 * root (`extensions.tsx`), which `main.tsx` imports before anything renders.
 *
 * The prompt is the whole fix for "Request a Feature": the public prompt
 * already carries its own tracker URL and its own filing steps (`gh issue
 * list` for duplicates, `gh label list` for the live taxonomy, `gh issue
 * create`, the 200-character pre-filled-URL rule), all specific to GitHub and
 * to this repository's label vocabulary. So it is replaced WHOLESALE, never
 * templated: an edition writes the complete prompt, URL included, in the
 * agent's language. The optional `issueUrl` is only for the three links that
 * do not go through a prompt.
 */
import { reportSeamCollision } from './apps/seamCollision'
import {
  FEATURE_REQUEST_FORM_URL,
  FEATURE_REQUEST_PROMPT_FALLBACK,
  FEATURE_REQUEST_URL,
} from './prompts/featureRequest'

export interface FeedbackTracker {
  /**
   * The hidden context "Request a Feature" seeds into the new session: the
   * complete conversational filing workflow, tracker URL included.
   */
  prompt: string
  /**
   * The tracker's blank new-issue page. The About panel's "report it" link and
   * the redaction false-positive report open it.
   */
  issueUrl: string
  /**
   * The non-inference route offered on the `usage_limit` row when the
   * conversational flow is refused for a spent plan allowance (#13342): a
   * plain link the user can open with no agent turn. The public repository's
   * feature-request form; an edition's `issueUrl`.
   */
  formUrl: string
}

/**
 * What an edition supplies: its prompt, and optionally where the three
 * prompt-less links should point. Without `issueUrl` those links keep the
 * public tracker.
 */
export interface FeedbackTrackerOverride {
  prompt: string
  issueUrl?: string
}

/** The stock build's tracker: the public repository, filed with `gh`. */
const PUBLIC_TRACKER: FeedbackTracker = Object.freeze({
  prompt: FEATURE_REQUEST_PROMPT_FALLBACK,
  issueUrl: FEATURE_REQUEST_URL,
  formUrl: FEATURE_REQUEST_FORM_URL,
})

let tracker: FeedbackTracker = PUBLIC_TRACKER
// Flipped by the first read. A set after that would retarget some surfaces and
// not others, depending on which component rendered first, so it is refused
// the same way a late `setProductName` is.
let read = false

/** Only an absolute http(s) URL reaches a tracker; anything else is a typo or
 *  a `javascript:` smuggle, and the stock target is safer than either. */
function isTrackerUrl(value: unknown): value is string {
  if (typeof value !== 'string') return false
  try {
    const { protocol } = new URL(value)
    return protocol === 'https:' || protocol === 'http:'
  } catch {
    return false
  }
}

/**
 * Retarget the feedback surfaces at an edition's tracker. Call it once, from
 * the edition composition root, before anything renders. A call after the
 * first read throws in dev and is ignored in production (mirroring
 * `setProductName`); a malformed value is reported through the shared seam
 * policy and leaves the stock tracker in place, so the surfaces keep working.
 */
export function setFeedbackTracker(override: FeedbackTrackerOverride): void {
  if (read) {
    if (import.meta.env.DEV) {
      throw new Error('setFeedbackTracker() must be called before the first render')
    }
    return
  }
  const { prompt, issueUrl } = override
  if (typeof prompt !== 'string' || !prompt.trim()) {
    reportSeamCollision('feedbackTracker', 'prompt must be a non-empty string; keeping the stock tracker')
    return
  }
  if (issueUrl !== undefined && !isTrackerUrl(issueUrl)) {
    reportSeamCollision('feedbackTracker', 'issueUrl must be an absolute http(s) URL; keeping the stock tracker')
    return
  }
  tracker = Object.freeze({
    prompt,
    issueUrl: issueUrl ?? PUBLIC_TRACKER.issueUrl,
    // An edition tracker has no public-repo template form, so its new-issue
    // page is the non-inference exit too.
    formUrl: issueUrl ?? PUBLIC_TRACKER.formUrl,
  })
}

/** The tracker in effect: the edition's if one was set in time, else the public one. */
export function feedbackTracker(): FeedbackTracker {
  read = true
  return tracker
}
