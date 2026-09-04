/**
 * shared/config — MCP server normalization, the mode→instruction map, and the
 * deep merge that loads a saved config onto the defaults.
 *
 * `mergeConfig` is the interesting one: it is deliberately NOT a spread, because
 * three sub-objects have to merge one level deeper (tool overrides, colour maps,
 * window position) while two lists must be REPLACED rather than unioned. A
 * regression there silently resurrects settings the user cleared.
 */
import { describe, it, expect } from 'vitest'

import {
  BOTH_AGENT_DEFAULTS,
  DEFAULT_CONFIG,
  ELECTRON_MAP,
  MODIFIER_ORDER,
  PRODUCT_NAME,
  mergeConfig,
  normalizeMcpServerConfig,
  planningInstructionForMode,
} from '../src/shared/config'

describe('normalizeMcpServerConfig', () => {
  it('gives a plain string the chat agent only', () => {
    expect(normalizeMcpServerConfig('zzq-server')).toEqual({
      name: 'zzq-server',
      agents: ['chat'],
      autoApprove: [],
      disabledTools: [],
    })
  })

  it('gives the both-agent defaults chat AND bg', () => {
    const name = [...BOTH_AGENT_DEFAULTS][0]
    expect(normalizeMcpServerConfig(name).agents).toEqual(['chat', 'bg'])
  })

  it('keeps a structured entry, filtering agent values it does not know', () => {
    expect(
      normalizeMcpServerConfig({
        name: 'zzq-server',
        agents: ['bg', 'other' as unknown as 'bg'],
        autoApprove: ['tool_a'],
        disabledTools: ['tool_b'],
      }),
    ).toEqual({
      name: 'zzq-server',
      agents: ['bg'],
      autoApprove: ['tool_a'],
      disabledTools: ['tool_b'],
    })
  })

  it('falls back to the name-based default when agents is empty or not a list', () => {
    expect(normalizeMcpServerConfig({ name: 'zzq-server', agents: [], autoApprove: [], disabledTools: [] }).agents)
      .toEqual(['chat'])
    const bothName = [...BOTH_AGENT_DEFAULTS][0]
    expect(
      normalizeMcpServerConfig({
        name: bothName,
        agents: undefined as unknown as ('chat' | 'bg')[],
        autoApprove: [],
        disabledTools: [],
      }).agents,
    ).toEqual(['chat', 'bg'])
  })

  it('replaces non-array tool lists with empty ones instead of passing junk through', () => {
    const out = normalizeMcpServerConfig({
      name: 'zzq-server',
      agents: ['chat'],
      autoApprove: 'all' as unknown as string[],
      disabledTools: null as unknown as string[],
    })
    expect(out.autoApprove).toEqual([])
    expect(out.disabledTools).toEqual([])
  })
})

describe('planningInstructionForMode', () => {
  it('has a distinct instruction per mode, with balanced as the default', () => {
    const active = planningInstructionForMode('active')
    const quiet = planningInstructionForMode('quiet')
    const normal = planningInstructionForMode('normal')
    expect(new Set([active, quiet, normal]).size).toBe(3)
    expect(active).toMatch(/active/i)
    expect(quiet).toMatch(/minimal interruption/i)
    // Anything unrecognised must still get a usable instruction.
    expect(planningInstructionForMode('zzq' as 'normal')).toBe(normal)
  })
})

describe('mergeConfig', () => {
  it('returns the defaults untouched for an empty override', () => {
    const out = mergeConfig(DEFAULT_CONFIG, {})
    expect(out).toEqual(DEFAULT_CONFIG)
    expect(out).not.toBe(DEFAULT_CONFIG)
  })

  it('merges one level into each section rather than replacing it', () => {
    const out = mergeConfig(DEFAULT_CONFIG, {
      agentBackend: { url: 'http://localhost:1' } as never,
      mochi: { petName: 'zzq-pet' } as never,
    })
    expect(out.agentBackend.url).toBe('http://localhost:1')
    // Untouched sibling keys survive.
    expect(out.agentBackend.wsUrl).toBe(DEFAULT_CONFIG.agentBackend.wsUrl)
    expect(out.mochi.petName).toBe('zzq-pet')
    expect(out.mochi.theme).toBe(DEFAULT_CONFIG.mochi.theme)
  })

  it('merges the nested maps: tool overrides, colour maps and window position', () => {
    const base = mergeConfig(DEFAULT_CONFIG, {
      trust: { level: 'trust', toolOverrides: { keep: 'auto' } },
      mochi: { colorMaps: { keep: { '#F9A85F': '#111111' } } } as never,
    })
    const out = mergeConfig(base, {
      trust: { level: 'yolo', toolOverrides: { added: 'ask' } },
      mochi: { colorMaps: { added: { '#F9A85F': '#222222' } } } as never,
      window: { position: { x: 5 } } as never,
    })
    expect(out.trust.level).toBe('yolo')
    expect(Object.keys(out.trust.toolOverrides).sort()).toEqual(['added', 'keep'])
    expect(Object.keys(out.mochi.colorMaps).sort()).toEqual(['added', 'keep'])
    expect(out.window.position).toEqual({ x: 5, y: DEFAULT_CONFIG.window.position.y })
  })

  it('handles a trust/window/mochi override that omits its nested map', () => {
    const out = mergeConfig(DEFAULT_CONFIG, {
      trust: { level: 'trust_reads' } as never,
      window: { visible: false } as never,
    })
    expect(out.trust.toolOverrides).toEqual({})
    expect(out.window.position).toEqual(DEFAULT_CONFIG.window.position)
  })

  it('REPLACES custom presets rather than appending — a delete must stick', () => {
    const base = mergeConfig(DEFAULT_CONFIG, {
      mochi: {
        customPresets: [
          { id: 'p1', name: 'zzq', description: '', colorMap: {}, swatches: [], builtIn: false },
        ],
      } as never,
    })
    expect(mergeConfig(base, { mochi: { customPresets: [] } as never }).mochi.customPresets).toEqual([])
    // Omitted entirely — the base list stands.
    expect(mergeConfig(base, {}).mochi.customPresets).toHaveLength(1)
  })

  it('normalizes extraMcpServers from both sides of the merge', () => {
    const out = mergeConfig(DEFAULT_CONFIG, {
      mochi: { extraMcpServers: ['zzq-plain'] } as never,
    })
    expect(out.mochi.extraMcpServers).toEqual([
      { name: 'zzq-plain', agents: ['chat'], autoApprove: [], disabledTools: [] },
    ])
    // And when the override omits the key, the base's plain strings still come
    // back structured — the picker reads `.agents` unconditionally.
    const again = mergeConfig(out, {})
    expect(again.mochi.extraMcpServers[0]).toMatchObject({ name: 'zzq-plain', agents: ['chat'] })
  })
})

describe('accelerator constants', () => {
  it('maps both platform glyphs onto the portable accelerator token', () => {
    expect(ELECTRON_MAP['⌘']).toBe('CommandOrControl')
    expect(ELECTRON_MAP.Ctrl).toBe('CommandOrControl')
    // `Alt` is portable, `Option` is not — the map must never emit the latter.
    expect(Object.values(ELECTRON_MAP)).not.toContain('Option')
  })

  it('orders modifiers so one chord has exactly one spelling', () => {
    expect(MODIFIER_ORDER.indexOf('CommandOrControl')).toBeLessThan(MODIFIER_ORDER.indexOf('Shift'))
    expect(PRODUCT_NAME).toBe('Mochi')
  })
})
