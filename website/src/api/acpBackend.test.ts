/**
 * `isKiroBackend` decides whether the pill's Kiro-only surfaces (the no-reading
 * dash) render. It must be POSITIVE — true only for the kiro id, and never for a
 * config that has not loaded — because a guess here puts a "could not read your
 * balance; open to refresh" control in front of a user whose harness has no
 * balance to read.
 */
import { describe, expect, it } from 'vitest'

import { ACP_BACKEND_KAS, ACP_BACKEND_KIRO, acpBackendName, acpBackendShortName, configuredAcpBackend, DEFAULT_ACP_BACKEND, isKiroBackend, mcpServerDeny, mcpWholeServerDeny, reportsCredits, isKiroHarness } from './acpBackend'
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
    expect(out.rule).toMatch(/^Turning off one MCP tool on goose stops every tool on the same server/)
    expect(out.exception).toBe('')
  })

  it('adds the exception on a per-call reach', () => {
    const out = mcpWholeServerDeny(row({ per_tool_deny: 'per-call', costs_whole_server: true, ineffective: [] }, 'codex', 'Codex'))
    expect(out.rule).toContain('on OpenAI Codex')
    expect(out.exception).toMatch(/^One exception: .*kirocrew-core.* on OpenAI Codex\.$/)
  })

  it('says nothing on a reach that stops only the named tool', () => {
    expect(mcpWholeServerDeny(row({ per_tool_deny: 'settings-file', costs_whole_server: false, ineffective: [] })).rule).toBe('')
  })

  it('reads the server flag, not the reach name: no flag (an older gateway) is no rule', () => {
    expect(mcpWholeServerDeny(row({ per_tool_deny: 'whole-server', ineffective: [] })).rule).toBe('')
    expect(mcpWholeServerDeny(row(undefined)).rule).toBe('')
  })
})

describe('mcpServerDeny', () => {
  // The tool list's wording for ONE server, from the gateway's spared list.
  const row = (mcp: AcpBackendProbe['mcp']): AcpBackendProbe =>
    ({ id: 'codex', policy_id: 'Codex', mcp } as AcpBackendProbe)
  const perCall = (spared?: string[]) => row({
    per_tool_deny: 'per-call', costs_whole_server: true, ineffective: [],
    ...(spared ? { spared_servers: spared } : {}),
  })

  it('gives a spared server only the tool-by-tool fact', () => {
    const out = mcpServerDeny(perCall(['kirocrew-core']), 'kirocrew-core')
    expect(out.rule).toBe('')
    expect(out.exception).toMatch(/on OpenAI Codex turning off one tool here stops only that tool\.$/)
  })

  it('gives every other server only the rule', () => {
    const out = mcpServerDeny(perCall(['kirocrew-core']), 'github')
    expect(out.rule).toContain('on OpenAI Codex')
    expect(out.exception).toBe('')
  })

  it('reads the names from the gateway, not from a list of its own', () => {
    expect(mcpServerDeny(perCall(['someone-else']), 'kirocrew-core').rule).toContain('on OpenAI Codex')
  })

  it('keeps the pair on a gateway that sends no list', () => {
    expect(mcpServerDeny(perCall(), 'kirocrew-core')).toEqual(mcpWholeServerDeny(perCall()))
  })

  it('says nothing where the reach costs only the tool', () => {
    const out = mcpServerDeny(row({ per_tool_deny: 'settings-file', costs_whole_server: false, spared_servers: [], ineffective: [] }), 'github')
    expect(out).toEqual({ rule: '', exception: '' })
    expect(mcpServerDeny(undefined, 'github')).toEqual({ rule: '', exception: '' })
  })
})

describe('isKiroHarness', () => {
  it('is false while the config has not loaded', () => {
    expect(isKiroHarness(undefined)).toBe(false)
  })

  it('is true for kiro-cli, the KAS relay, and "no choice" (absent or empty), which resolves to the default', () => {
    expect(isKiroHarness({ agent: { acp_backend: ACP_BACKEND_KIRO } })).toBe(true)
    expect(isKiroHarness({ agent: { acp_backend: ACP_BACKEND_KAS } })).toBe(true)
    expect(isKiroHarness({})).toBe(true)
    expect(isKiroHarness({ agent: { acp_backend: '' } })).toBe(true)
  })

  it('is false for every other harness', () => {
    for (const id of ['claude', 'codex', 'opencode', 'goose', 'pi', 'deepseek']) {
      expect(isKiroHarness({ agent: { acp_backend: id } })).toBe(false)
    }
  })
})

describe('acpBackendName / acpBackendShortName', () => {
  it('calls the KAS harness "Kiro Agent" everywhere, badges included, without renaming its id', () => {
    // A display rename only: the id the config stores and the gateway reads stays `kas`.
    expect(ACP_BACKEND_KAS).toBe('kas')
    expect(acpBackendName({ id: ACP_BACKEND_KAS })).toBe('Kiro Agent')
    expect(acpBackendShortName(ACP_BACKEND_KAS)).toBe('Kiro Agent')
    expect(acpBackendName({ id: ACP_BACKEND_KIRO })).toBe('Kiro CLI')
  })
})
