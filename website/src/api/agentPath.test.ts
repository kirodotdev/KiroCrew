/**
 * `agentPath` keeps a crew name a proxy would mangle out of the URL path:
 * a name that needs percent-encoding becomes an unpadded base64url
 * segment, the spelling `kiro_crew/agent_url_path.py` decodes.
 */
import { describe, expect, it } from 'vitest'

import { agentPath, base64Url, needsProxySafePath } from './agentPath'

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

  it('flags the same names for the record save as for every other route', () => {
    for (const name of ['scout', 'my-crew_1']) expect(needsProxySafePath(name)).toBe(false)
    for (const name of ['Nova Sky', "Bob's", 'a(b)', 'x*', 'hi!']) expect(needsProxySafePath(name)).toBe(true)
  })

  it('encodes UTF-8 bytes, not UTF-16 code units, in the URL-safe alphabet', () => {
    expect(base64Url('café')).toBe('Y2Fmw6k')
    expect(base64Url('a/b?')).toBe('YS9iPw')
    expect(base64Url('\u00ff\u00fe')).toBe('w7_Dvg')
  })
})
