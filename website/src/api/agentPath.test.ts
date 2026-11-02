/**
 * `agentPath` keeps a crew name a proxy would mangle out of the URL path
 * (#20083): a name that needs percent-encoding becomes an unpadded base64url
 * segment, the spelling `kiro_crew/agent_url_path.py` decodes.
 */
import { describe, expect, it } from 'vitest'

import { agentPath, base64Url } from './agentPath'

describe('agentPath', () => {
  it('keeps a plain name in path form', () => {
    expect(agentPath('scout')).toBe('/api/agents/scout')
    expect(agentPath('scout', '/avatar')).toBe('/api/agents/scout/avatar')
    expect(agentPath('b64', '/capabilities')).toBe('/api/agents/b64/capabilities')
  })

  it('moves a name with a space into a base64url segment', () => {
    // The server's canonical spelling of "Nova Sky" (base64url, no padding).
    expect(agentPath('Nova Sky', '/worker-kit')).toBe('/api/agents/b64/Tm92YSBTa3k/worker-kit')
    expect(agentPath('Nova Sky')).not.toMatch(/[ %]/)
  })

  it("agrees with the server on names encodeURIComponent leaves alone", () => {
    expect(agentPath("Bob's")).toBe(`/api/agents/b64/${base64Url("Bob's")}`)
  })

  it('encodes UTF-8 bytes, not UTF-16 code units, in the URL-safe alphabet', () => {
    expect(base64Url('café')).toBe('Y2Fmw6k')
    expect(base64Url('a/b?')).toBe('YS9iPw')
    expect(base64Url('\u00ff\u00fe')).toBe('w7_Dvg')
  })
})
