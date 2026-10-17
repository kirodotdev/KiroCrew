import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { FEATURE_REQUEST_PROMPT_FALLBACK } from '../prompts/featureRequest'

// The "Request a Feature" flow has two copies of the same instructions: the
// `feature-request` skill (preferred) and the fallback prompt (self-contained
// when the skill is unavailable). Both read the live label list via
// `gh label list` rather than baking `enhancement`/`bug` in as the entire label
// vocabulary, so issues filed through the flow carry the repo's grouping labels
// and a taxonomy change does not mean editing prose in two files.
//
// These tests lock that contract on BOTH copies so they cannot drift apart.

const skill = readFileSync(
  resolve(__dirname, '../../../src/kiro_crew/builtin_skills/feature-request/SKILL.md'),
  'utf-8',
)

// Forward guard only: no concrete grouping-label value may be written into
// either copy. On its own this does NOT fail for content that names no grouping
// label at all — the base-failing guards are the submit-path and single-mention
// assertions below. Matches `area: x` / `platform: x` in prose, code fences, or
// URLs. No leading `\b`: in an encoded URL the value is preceded
// by `%2C`, whose `C` is a word char, so a boundary would miss `%2Carea%3A%20x`.
const CONCRETE_GROUPING_LABEL = /(area|platform)(:\s*|%3A%20)[a-z]/i

describe('feature-request label selection', () => {
  describe.each([
    ['prompt fallback', FEATURE_REQUEST_PROMPT_FALLBACK],
    ['skill', skill],
  ])('%s', (_name, text) => {
    it('tells the agent to read the live label list', () => {
      expect(text).toMatch(/gh label list/)
    })

    it('does not hard-code any concrete area/platform label value', () => {
      expect(text).not.toMatch(CONCRETE_GROUPING_LABEL)
    })

    // The load-bearing regression guard. `enhancement` may appear exactly once,
    // in the degraded no-`gh` path: the upper bound is what a re-baked
    // vocabulary would break, and the lower bound keeps the degraded path from
    // silently losing its type label.
    it('names a concrete type label exactly once, for the no-gh path', () => {
      expect(text.match(/\benhancement\b/gi) ?? []).toHaveLength(1)
      expect(text.toLowerCase()).toMatch(/if `?gh`? is unavailable/)
    })

    it('forbids creating new labels', () => {
      expect(text.toLowerCase()).toMatch(/never create a new label/)
    })

    it('keeps the type label mutually exclusive', () => {
      expect(text.toLowerCase()).toMatch(/mutually exclusive/)
    })

    it('caps grouping labels at one per dimension', () => {
      expect(text.toLowerCase()).toMatch(/at most one/)
    })
  })

  // The pre-filled URL (Option 2) must carry no `labels=` query param, not
  // merely no `labels=enhancement`: GitHub answers 404 to a `labels` query from
  // anyone without permission to label issues in this repo, which is most
  // reporters. Labels apply only through the `gh issue create` path (Option 3),
  // and triage labels the rest. Both copies may still *mention* `labels=` in
  // prose to explain why not to add it, so match only a real query param
  // (`?labels=` / `&labels=`), not a backtick-quoted mention.
  it('does not put a labels= query param in the pre-filled URL', () => {
    expect(FEATURE_REQUEST_PROMPT_FALLBACK).not.toMatch(/[?&]labels=/)
    expect(skill).not.toMatch(/[?&]labels=/)
  })

  // The pre-filled URL (Option 2) is offered only when its query string is
  // under 200 characters; above that Crew redacts it in chat, so the agent —
  // which knows the length because it builds the URL — leaves it out rather
  // than handing the user a placeholder (the dead-end #12847 reports). Both
  // copies must carry this gate so they agree.
  it('gates the pre-filled URL on an under-200-character query string', () => {
    expect(FEATURE_REQUEST_PROMPT_FALLBACK).toMatch(/under 200 characters/)
    expect(skill).toMatch(/under 200 characters/)
  })

  it('no longer pins the gh create command to a single label', () => {
    expect(FEATURE_REQUEST_PROMPT_FALLBACK).not.toMatch(/--label enhancement/)
    expect(skill).not.toMatch(/--label enhancement/)
  })
})
