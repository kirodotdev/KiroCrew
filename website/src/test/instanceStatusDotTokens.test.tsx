/** The remote-crew status dot must use color tokens that tailwind.config.js
 * actually defines.
 *
 * `STATE_DOT` in InstancesPanel.tsx mapped `connected` to `bg-success` and
 * `connecting` to `bg-warning`. Neither `success` nor `warning` is a key in the
 * Tailwind color palette (the palette has `ok` / `warn` / `danger`), so those
 * utilities were never emitted into the stylesheet: the dot rendered with no
 * background at all. The bug was invisible in the two states that happen to use
 * real tokens (`disconnected` -> `bg-muted`, `error` -> `bg-danger`), which is
 * why the "Crews you can switch to" list showed a dot next to Disconnected and
 * nothing next to Connected.
 *
 * The allow-list here is READ OUT of tailwind.config.js (the config is
 * importable, as `tailwindAlphaTokens.test.ts` already does) rather than
 * hardcoded, so renaming or dropping a theme token fails this test instead of
 * silently reintroducing an invisible dot.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { screen } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import tailwindConfig from '../../tailwind.config.js'

const __dirname = dirname(fileURLToPath(import.meta.url))

const panelSrc = readFileSync(resolve(__dirname, '../pages/settings/InstancesPanel.tsx'), 'utf-8')

/** Color token names declared in tailwind.config.js's `theme.extend.colors`. */
function themeColorTokens(): Set<string> {
  const colors = (tailwindConfig as { theme: { extend: { colors: Record<string, unknown> } } })
    .theme.extend.colors
  return new Set(Object.keys(colors))
}

/** The class string each tunnel state maps to, read out of the STATE_DOT map. */
function stateDotClasses(): Record<string, string> {
  const block = /const STATE_DOT[^=]*=\s*\{([\s\S]*?)\n\}/.exec(panelSrc)
  expect(block, 'could not locate STATE_DOT in InstancesPanel.tsx').toBeTruthy()
  const out: Record<string, string> = {}
  for (const m of block![1].matchAll(/^\s*([a-z]+):\s*'([^']+)',/gm)) out[m[1]] = m[2]
  return out
}

describe('remote-crew status dot colors', () => {
  it('maps every tunnel state to a color token the theme defines', () => {
    const tokens = themeColorTokens()
    // The premise the whole fix rests on: these two names are the real tokens
    // and the two the panel used are not in the palette at all.
    expect(tokens.has('ok')).toBe(true)
    expect(tokens.has('warn')).toBe(true)
    expect(tokens.has('success')).toBe(false)
    expect(tokens.has('warning')).toBe(false)

    const dots = stateDotClasses()
    expect(Object.keys(dots).sort()).toEqual(
      ['connected', 'connecting', 'disconnected', 'error', 'stopped'],
    )
    for (const [state, cls] of Object.entries(dots)) {
      const token = /^bg-(.+)$/.exec(cls)?.[1]
      expect(token, `${state} -> ${cls} is not a bg-<token> class`).toBeTruthy()
      expect(tokens.has(token!), `${state} -> ${cls} is not a theme color token`).toBe(true)
    }
  })

  it('connected is green (ok) and distinguishable from the neutral states', () => {
    const dots = stateDotClasses()
    expect(dots.connected).toBe('bg-ok')
    expect(dots.connecting).toBe('bg-warn')
    expect(dots.connected).not.toBe(dots.disconnected)
    expect(dots.connected).not.toBe(dots.stopped)
  })

  // The third assertion this file used to carry — "uses no phantom
  // success/warning utility anywhere in the panel" — is gone: the repo-wide
  // `phantom-classes` gate asks Tailwind whether a class is emitted, for every
  // file, so a per-file name ban is now its third spelling and the one most
  // likely to drift. What stays here is what the gate CANNOT check: that the map
  // covers every tunnel state, and that `connected` is visually distinct from
  // the neutral ones. Both classes could be real and the dot still wrong.
})

vi.mock('../api/client', () => ({
  ApiError: class extends Error {},
  api: {
    listInstances: vi.fn(),
    addInstance: vi.fn(),
    connectInstance: vi.fn(),
    disconnectInstance: vi.fn(),
    removeInstance: vi.fn(),
    instanceStatus: vi.fn(),
    restartInstance: vi.fn(),
    patchConfig: vi.fn(),
  },
}))
import { api } from '../api/client'
import { InstancesPanel } from '../pages/settings/InstancesPanel'

beforeEach(() => vi.clearAllMocks())

describe('the rendered status badge', () => {
  it('gives the connected row a dot element carrying the ok token', async () => {
    const inst = {
      id: 'i1', name: 'box', ssh_host: 'box', remote_port: 7777, local_port: 7801,
      ttl: '20h', connection_method: 'ssh', ssm_target: '', aws_profile: '', aws_region: '',
      ssm_run_as: '', remote_bin: '', was_connected: true,
      status: { state: 'connected' as const },
    }
    vi.mocked(api.listInstances).mockResolvedValue(
      { active: true, instances: [inst], warm_set_cap: 5 } as never,
    )
    renderWithProviders(<InstancesPanel />)

    const label = await screen.findByText('connected')
    const dot = label.previousElementSibling
    expect(dot, 'status label has no preceding dot element').toBeTruthy()
    expect(dot!.className).toContain('rounded-full')
    expect(dot!.className).toContain('bg-ok')
  })
})
