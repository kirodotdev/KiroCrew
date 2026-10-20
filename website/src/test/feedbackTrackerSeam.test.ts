/**
 * The feedback-tracker seam (#22554): one exported setter retargets every
 * surface that sends a user to the issue tracker.
 *
 * Each test takes a FRESH module: the seam seals on its first read, so a shared
 * instance would let one test's read refuse the next test's set.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'

import {
  FEATURE_REQUEST_FORM_URL,
  FEATURE_REQUEST_PROMPT_FALLBACK,
  FEATURE_REQUEST_URL,
} from '../prompts/featureRequest'

type Seam = typeof import('../feedbackTracker')

async function freshSeam(): Promise<Seam> {
  vi.resetModules()
  return import('../feedbackTracker')
}

const PROMPT = 'The user clicked "Request a Feature". File it at https://tracker.example.test/new.'
const ISSUE_URL = 'https://tracker.example.test/new'

describe('feedbackTracker seam', () => {
  beforeEach(() => {
    vi.resetModules()
  })

  it('answers the public tracker in the stock build, byte-identical to the constants', async () => {
    const { feedbackTracker } = await freshSeam()
    expect(feedbackTracker()).toEqual({
      prompt: FEATURE_REQUEST_PROMPT_FALLBACK,
      issueUrl: FEATURE_REQUEST_URL,
      formUrl: FEATURE_REQUEST_FORM_URL,
    })
  })

  it('a prompt alone replaces the seeded prompt and leaves the links on the public tracker', async () => {
    const { setFeedbackTracker, feedbackTracker } = await freshSeam()
    setFeedbackTracker({ prompt: PROMPT })
    expect(feedbackTracker()).toEqual({
      prompt: PROMPT,
      issueUrl: FEATURE_REQUEST_URL,
      formUrl: FEATURE_REQUEST_FORM_URL,
    })
  })

  it('a prompt with issueUrl points every link at the edition tracker, the spent-plan exit included', async () => {
    const { setFeedbackTracker, feedbackTracker } = await freshSeam()
    setFeedbackTracker({ prompt: PROMPT, issueUrl: ISSUE_URL })
    expect(feedbackTracker()).toEqual({ prompt: PROMPT, issueUrl: ISSUE_URL, formUrl: ISSUE_URL })
  })

  it('refuses a set after the first read rather than half-applying it', async () => {
    const { setFeedbackTracker, feedbackTracker } = await freshSeam()
    feedbackTracker()
    // Vitest runs with import.meta.env.DEV true, so the late call throws.
    expect(() => setFeedbackTracker({ prompt: PROMPT, issueUrl: ISSUE_URL })).toThrow(/before the first render/)
    expect(feedbackTracker().issueUrl).toBe(FEATURE_REQUEST_URL)
  })

  it.each([
    ['an empty prompt', { prompt: '   ', issueUrl: ISSUE_URL }],
    ['a relative issue path', { prompt: PROMPT, issueUrl: '/issues/new' }],
    ['a javascript: issue URL', { prompt: PROMPT, issueUrl: 'javascript:alert(1)' }],
  ])('rejects %s through the shared seam policy and keeps the stock tracker', async (_name, bad) => {
    const { setFeedbackTracker, feedbackTracker } = await freshSeam()
    // reportSeamCollision throws in dev/test (and warns in production).
    expect(() => setFeedbackTracker(bad)).toThrow(/\[feedbackTracker\]/)
    expect(feedbackTracker().issueUrl).toBe(FEATURE_REQUEST_URL)
    expect(feedbackTracker().prompt).toBe(FEATURE_REQUEST_PROMPT_FALLBACK)
  })

  it('is what the consuming surfaces read', async () => {
    // The surfaces import the seam, not the public constants: a retarget
    // reaches the hidden prompt, the usage_limit form route, the About link and
    // the redaction false-positive report alike.
    const { setFeedbackTracker } = await freshSeam()
    setFeedbackTracker({ prompt: PROMPT, issueUrl: ISSUE_URL })
    const { falsePositiveIssueUrl } = await import('../prompts/redactionReport')
    const url = new URL(falsePositiveIssueUrl('rule-x', 'link'))
    expect(url.origin + url.pathname).toBe(ISSUE_URL)
    expect(url.searchParams.get('title')).toBe('Redaction false positive: rule-x')
  })
})
