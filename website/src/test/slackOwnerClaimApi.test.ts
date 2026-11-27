/**
 * `pages/settings/slackOwnerClaimApi.ts`: each call reaches its route through
 * the installed transport, with the body the route reads.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'

import { apiTransport } from '../api/apiTransport'
import { slackOwnerClaimApi } from '../pages/settings/slackOwnerClaimApi'

afterEach(() => {
  vi.restoreAllMocks()
})

function stubTransport() {
  const res = new Response('{}')
  const get = vi.spyOn(apiTransport, 'get').mockResolvedValue(res)
  const post = vi.spyOn(apiTransport, 'post').mockResolvedValue(res)
  vi.spyOn(apiTransport, 'j').mockResolvedValue({ open: false })
  return { get, post }
}

describe('slackOwnerClaimApi', () => {
  it('reads the window with a GET', async () => {
    const { get } = stubTransport()
    expect(await slackOwnerClaimApi.get()).toEqual({ open: false })
    expect(get).toHaveBeenCalledWith('/api/slack/owner-claim')
  })

  it('starts, cancels and confirms with POSTs to their own routes', async () => {
    const { post } = stubTransport()
    await slackOwnerClaimApi.start()
    await slackOwnerClaimApi.cancel()
    await slackOwnerClaimApi.confirm('U0OWNER')
    expect(post.mock.calls).toEqual([
      ['/api/slack/owner-claim/start', {}],
      ['/api/slack/owner-claim/cancel', {}],
      ['/api/slack/owner-claim/confirm', { user_id: 'U0OWNER' }],
    ])
  })
})
