/**
 * `gitlabGroupError` is the one decoder for the coded error envelope the
 * `/gitlab-groups` and `/deps` routes answer with. Its `loginCommand` is the
 * sign-in remedy the gateway built from its own allowlisted host; the decoder
 * accepts only that shape, so a body cannot smuggle arbitrary text into the
 * command the Graph tab renders as copyable.
 */
import { describe, it, expect } from 'vitest'

import { GitLabGroupError, gitlabGroupError } from './apiTypes'

const response = (body: unknown, status = 502) =>
  new Response(body === undefined ? 'not json' : JSON.stringify(body), { status })

describe('gitlabGroupError', () => {
  it('carries the code and a host-scoped loginCommand from the body', async () => {
    const err = await gitlabGroupError(
      response({ error: 'not signed in', code: 'deps_auth_required', loginCommand: 'glab auth login --hostname gitlab.example.com' }),
    )
    expect(err).toBeInstanceOf(GitLabGroupError)
    expect(err.code).toBe('deps_auth_required')
    expect(err.loginCommand).toBe('glab auth login --hostname gitlab.example.com')
  })

  it('drops a loginCommand that is not a bare gh/glab host-scoped sign-in', async () => {
    for (const bad of ['rm -rf ~', 'glab auth login --hostname x && curl evil', 'glab auth login', 42]) {
      const err = await gitlabGroupError(response({ error: 'x', code: 'deps_auth_required', loginCommand: bad }))
      expect(err.loginCommand).toBe('')
    }
  })

  it('falls back to the HTTP status for a non-JSON body', async () => {
    const err = await gitlabGroupError(response(undefined, 503))
    expect(err.message).toBe('HTTP 503')
    expect(err.code).toBe('')
    expect(err.loginCommand).toBe('')
  })
})
