/**
 * `isKiroBackend` decides whether the pill's Kiro-only surfaces (the no-reading
 * dash) render. It must be POSITIVE — true only for the kiro id, and never for a
 * config that has not loaded — because a guess here puts a "could not read your
 * balance; open to refresh" control in front of a user whose harness has no
 * balance to read.
 */
import { describe, expect, it } from 'vitest'

import { ACP_BACKEND_KAS, ACP_BACKEND_KIRO, configuredAcpBackend, DEFAULT_ACP_BACKEND, isKiroBackend, mcpWholeServerDeny, reportsCredits } from './acpBackend'
import type { AcpBackendProbe } from './client/config'

describe('isKiroBackend', () => {
  it('is false while the config has not loaded: nothing Kiro-only renders on a guess', () => {
    expect(isKiroBackend(undefined)).toBe(false)
  })

  it('names kiro by its own id only: an unset or empty key means KAS on the gateway', () => {
    expect(ACP_BACKEND_KIRO).toBe('kiro')
    expect(isKiroBackend({ agent: { acp_backend: ACP_BACKEND_KIRO } })).toBe(true)
    expect(isKiroBackend({ agent: { acp_backend: '' } })).toBe(false)
    expect(isKiroBackend({ agent: {} })).toBe(false)
    expect(isKiroBackend({})).toBe(false)
  })
})

describe('configuredAcpBackend', () => {
  it('reads "no choice" (absent or empty) as the default, KAS', () => {
    expect(DEFAULT_ACP_BACKEND).toBe(ACP_BACKEND_KAS)
    expect(configuredAcpBackend({})).toBe(ACP_BACKEND_KAS)
    expect(configuredAcpBackend({ agent: { acp_backend: '' } })).toBe(ACP_BACKEND_KAS)
    expect(configuredAcpBackend({ agent: { acp_backend: 'kiro' } })).toBe(ACP_BACKEND_KIRO)
    expect(configuredAcpBackend({ agent: { acp_backend: 'claude' } })).toBe('claude')
  })

  it('is false for every other harness, not just claude', () => {
    for (const backend of ['claude', 'kas', 'codex', 'opencode', 'pi', 'goose', 'deepseek', 'some-future-harness']) {
      expect(isKiroBackend({ agent: { acp_backend: backend } }), backend).toBe(false)
    }
  })
})

describe('reportsCredits', () => {
  it('is false while the config has not loaded: a credits chart does not render on a guess', () => {
    expect(reportsCredits(undefined)).toBe(false)
  })

  it('is true for kiro-cli and for its KAS relay (also what an unset key means)', () => {
    expect(reportsCredits({ agent: { acp_backend: ACP_BACKEND_KIRO } })).toBe(true)
    expect(reportsCredits({})).toBe(true)
    expect(reportsCredits({ agent: { acp_backend: ACP_BACKEND_KAS } })).toBe(true)
  })

  it('is false for a harness that bills in tokens or dollars', () => {
    for (const backend of ['claude', 'codex', 'opencode', 'pi', 'goose', 'deepseek', 'some-future-harness']) {
      expect(reportsCredits({ agent: { acp_backend: backend } }), backend).toBe(false)
    }
  })
})

describe('mcpWholeServerDeny', () => {
  // The one reader of a probe row's deny reach. Settings → Agent Harness and the
  // MCP Servers tool list both render what it returns, so these cases are what
  // both surfaces say (#11969).
  const row = (mcp: AcpBackendProbe['mcp'], id = 'goose', policy_id = 'Goose'): AcpBackendProbe =>
    ({ id, policy_id, mcp } as AcpBackendProbe)

  it('says nothing without a row: in flight, 403 and 404 all look like this', () => {
    expect(mcpWholeServerDeny(undefined)).toEqual({ rule: '', exception: '' })
  })

  it('states the rule, naming the harness, where the server classed the reach as whole-server', () => {
    const out = mcpWholeServerDeny(row({ per_tool_deny: 'whole-server', costs_whole_server: true, ineffective: [] }))
    expect(out.rule).toMatch(/^Turning off one MCP tool on Goose stops every tool on the same server/)
    expect(out.exception).toBe('')
  })

  it('adds the exception on a per-call reach', () => {
    const out = mcpWholeServerDeny(row({ per_tool_deny: 'per-call', costs_whole_server: true, ineffective: [] }, 'codex', 'Codex'))
    expect(out.rule).toContain('on Codex')
    expect(out.exception).toMatch(/^One exception: .*kirocrew-core.* on Codex\.$/)
  })

  it('says nothing on a reach that stops only the named tool', () => {
    expect(mcpWholeServerDeny(row({ per_tool_deny: 'settings-file', costs_whole_server: false, ineffective: [] })).rule).toBe('')
  })

  it('reads the server flag, not the reach name: no flag (an older gateway) is no rule', () => {
    expect(mcpWholeServerDeny(row({ per_tool_deny: 'whole-server', ineffective: [] })).rule).toBe('')
    expect(mcpWholeServerDeny(row(undefined)).rule).toBe('')
  })
})
