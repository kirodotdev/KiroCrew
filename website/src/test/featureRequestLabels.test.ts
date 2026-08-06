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
  resolve(__dirname, '../../../skills/feature-request/SKILL.md'),
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

  it('no longer pins the pre-filled URL to a single label', () => {
    expect(FEATURE_REQUEST_PROMPT_FALLBACK).not.toMatch(/labels=enhancement/)
    expect(skill).not.toMatch(/labels=enhancement/)
  })

  it('no longer pins the gh create command to a single label', () => {
    expect(FEATURE_REQUEST_PROMPT_FALLBACK).not.toMatch(/--label enhancement/)
    expect(skill).not.toMatch(/--label enhancement/)
  })
})
