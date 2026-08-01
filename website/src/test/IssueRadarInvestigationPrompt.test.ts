/** The Investigate seed prompt's findings-write instruction.
 *
 * This prompt used to tell the agent to `PUT /api/apps/issue-radar/investigation`
 * directly. An agent session holds no dashboard credential — the access cookie is
 * httpOnly, `KIROCREW_INTERNAL_SECRET` is stripped from agent env, and
 * `.local_secret` is on the sensitive-path denylist — so that call was refused
 * with 403 EVERY time and no investigation ever recorded its findings (the
 * verdict/summary the card renders was dead code). The write now goes through the
 * `issue_radar_record_investigation` MCP tool, whose server holds the internal
 * secret legitimately.
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
