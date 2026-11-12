import { describe, expect, it } from 'vitest'

import { ghProxyNote } from './ghProxyNote'

describe('ghProxyNote', () => {
  it('reads the login from the tool server line that leads the result', () => {
    const output = '[gh-proxy · ran on your machine as @octo-owner: gh pr create (acme/widgets)]\nhttps://github.com/acme/widgets/pull/7\n[exit 0]'
    expect(ghProxyNote(output)).toEqual({ login: 'octo-owner' })
  })

  it('reads it inside an MCP text content, with the dot escaped or not', () => {
    expect(ghProxyNote('{"content":[{"type":"text","text":"[gh-proxy \\u00b7 ran on your machine as @a-b: gh pr checks (x/y)]\\n"}]}')).toEqual({ login: 'a-b' })
    expect(ghProxyNote('[{"type":"text","text":"[gh-proxy · ran on your machine as @a: gh pr view (x/y)]"}]')).toEqual({ login: 'a' })
  })

  it('ignores the line anywhere but first, so command output cannot pose as it', () => {
    expect(ghProxyNote('\n[gh-proxy · ran on your machine as @someone: gh pr merge (x/y)]')).toBeNull()
    expect(ghProxyNote('hello\n[gh-proxy · ran on your machine as @someone: gh pr view (x/y)]')).toBeNull()
    expect(ghProxyNote('gh-proxy: ran on the gateway machine as @someone for x/y')).toBeNull()
    expect(ghProxyNote('')).toBeNull()
    expect(ghProxyNote(undefined)).toBeNull()
  })
})
