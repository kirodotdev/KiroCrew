/** The Investigate seed prompt's findings-write instruction.
 *
 * The write goes through the `issue_radar_record_investigation` MCP tool, whose
 * server holds the internal secret legitimately — NOT a direct
 * `PUT /api/apps/issue-radar/investigation`. An agent session holds no dashboard
 * credential (the access cookie is httpOnly, `KIROCREW_INTERNAL_SECRET` is
 * stripped from agent env, and `.local_secret` is on the sensitive-path
 * denylist), so a raw-HTTP write is refused with 403 every time and records no
 * findings (the verdict/summary the card renders).
 *
 * These tests pin the contract in both directions: the tool must be named, and
 * the raw-HTTP instruction must never come back.
 */

import { describe, it, expect } from 'vitest'

import { buildInvestigationPrompt } from '../apps/issue-radar/lib/investigate.prompt'
import { type Issue, type RepoRef } from '../apps/issue-radar/api'

const GH: RepoRef = { owner: 'acme', repo: 'widget', provider: 'github', host: 'github.com' }
const GL: RepoRef = { owner: 'group/sub', repo: 'proj', provider: 'gitlab', host: 'gitlab.com' }

const ISSUE = {
  number: 1039,
  title: 'Add per-app approval',
  labels: ['enhancement'],
  state: 'open',
  author: 'someone',
  url: 'https://github.com/acme/widget/issues/1039',
} as unknown as Issue

describe('buildInvestigationPrompt — findings write channel', () => {
  it('names the MCP tool', () => {
    const p = buildInvestigationPrompt(GH, GH.owner, GH.repo, ISSUE)
    expect(p).toContain('issue_radar_record_investigation')
  })

  it('never instructs a raw HTTP write to the record endpoint', () => {
    const p = buildInvestigationPrompt(GH, GH.owner, GH.repo, ISSUE)
    expect(p).not.toMatch(/PUT\s+\/api\/apps/)
    expect(p).not.toContain('/api/apps/issue-radar/investigation')
  })

  it('says why a direct call fails, so the agent does not retry it as curl', () => {
    const p = buildInvestigationPrompt(GH, GH.owner, GH.repo, ISSUE)
    expect(p).toContain('403')
  })

  it('gives a fallback for a failing tool call instead of leaving the card blank', () => {
    // Without this the agent has no instructed recovery when the tool errors
    // (e.g. the app is disabled), and its only other idea is the curl that 403s.
    const p = buildInvestigationPrompt(GH, GH.owner, GH.repo, ISSUE)
    expect(p).toMatch(/if the tool itself errors/i)
    expect(p).toMatch(/do not fall back to curl/i)
  })

  it('carries the flat findings fields, not a nested findings object', () => {
    // The tool schema validates scalars + string lists; a nested `findings`
    // dict would reach the gateway unvalidated, so the prompt must show the
    // flat shape the tool actually accepts.
    const p = buildInvestigationPrompt(GH, GH.owner, GH.repo, ISSUE)
    for (const field of ['verdict', 'root_cause', 'suggested_labels', 'next_action', 'summary']) {
      expect(p).toContain(`"${field}"`)
    }
    expect(p).not.toContain('"findings"')
  })

  it('echoes provider + host + kind so a GitLab item is not recorded as GitHub', () => {
    const p = buildInvestigationPrompt(GL, GL.owner, GL.repo, ISSUE)
    expect(p).toContain('"provider":"gitlab"')
    expect(p).toContain('"host":"gitlab.com"')
    expect(p).toContain('"kind":"issue"')
  })

  it('passes the item number through to the record args', () => {
    const p = buildInvestigationPrompt(GH, GH.owner, GH.repo, ISSUE)
    expect(p).toContain('"number":1039')
  })
})

describe('buildInvestigationPrompt — reply ordering', () => {
  it('asks for the explanation first, ahead of the verdict', () => {
    // Without the ordering the agent opens on remediation, which is unreadable
    // to anyone who has not already read the thread.
    const p = buildInvestigationPrompt(GH, GH.owner, GH.repo, ISSUE)
    const explainAt = p.search(/Open your reply with the issue EXPLANATION/)
    const verdictAt = p.search(/report a short verdict/)
    expect(explainAt).toBeGreaterThan(-1)
    expect(verdictAt).toBeGreaterThan(-1)
    expect(explainAt).toBeLessThan(verdictAt)
  })

  it('names all four parts of the explanation, so its shape is fixed', () => {
    const p = buildInvestigationPrompt(GH, GH.owner, GH.repo, ISSUE)
    expect(p).toMatch(/what this issue is about/i)
    expect(p).toMatch(/what happens today versus what should happen/i)
    expect(p).toMatch(/who hits it and when/i)
    expect(p).toMatch(/why it is worth doing/i)
  })

  it('bounds the explanation in sentences and keeps the fix out of it', () => {
    // Sentences, not lines: line count depends on render width, which the writing
    // agent cannot see, so a line budget is a promise nothing can keep.
    const p = buildInvestigationPrompt(GH, GH.owner, GH.repo, ISSUE)
    expect(p).toMatch(/two sentences per part/i)
    expect(p).not.toMatch(/lines per part/i)
    expect(p).toMatch(/no proposed fix/i)
  })

  it('requires the break inside the diagram and forbids bare node ids by example', () => {
    // Mermaid node ids never render, so "the B->C hop" names nothing the reader
    // can find -- and that sentence is the whole point of drawing the diagram.
    // The abstract rule alone did not hold, so the prompt carries an example.
    const p = buildInvestigationPrompt(GH, GH.owner, GH.repo, ISSUE)
    expect(p).toMatch(/INSIDE the diagram/)
    expect(p).toMatch(/labelled or styled edge/i)
    expect(p).toMatch(/never write "the B hop" or "the B->C hop"/)
    expect(p).toMatch(/write the labels/i)
  })

  it('makes the diagram conditional on a multi-hop issue, not routine', () => {
    // An unconditional diagram would be drawn for single-site defects too,
    // where a one-box flowchart costs tokens and teaches nothing.
    const p = buildInvestigationPrompt(GH, GH.owner, GH.repo, ISSUE)
    expect(p).toContain('```mermaid')
    expect(p).toMatch(/spans more than one component or more than one hop/i)
    expect(p).toMatch(/skip the diagram/i)
  })

  it('leads the recorded summary plainly, without the four-part block', () => {
    // `findings.summary` has exactly one consumer -- the status pill's `title`
    // (a native tooltip). It gets the ordering fix in the smallest form that
    // reader can use, so the queue view stops opening on root cause; shaping it
    // as the full four parts would serve a body surface that does not exist.
    const p = buildInvestigationPrompt(GH, GH.owner, GH.repo, ISSUE)
    expect(p).toContain('"summary":"one plain-language sentence on what the issue is about')
    expect(p).not.toContain('"summary":"one paragraph"')
  })
})
