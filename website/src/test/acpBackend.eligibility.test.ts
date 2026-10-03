/**
 * The shared probe-row verdict (#14517). First-run setup and Settings > Agent
 * Harness both read these, so this table is the one place the meaning of a
 * `GET /api/acp-backends` row is pinned. Each surface's own test file carries the
 * same table rendered, so a surface that stopped using the shared verdict fails
 * there too.
 */
import { describe, expect, it } from 'vitest'
import {
  acpProbeBlocksUse,
  acpProbeConfirmsUse,
  acpProbeState,
  type AcpProbeState,
} from '../api/acpBackend'
import type { AcpBackendProbe } from '../api/client'

function probe(over: Partial<AcpBackendProbe> = {}): AcpBackendProbe {
  return {
    id: 'claude',
    policy_id: 'claude',
    selectable: true,
    independent_setup: true,
    installed: 'installed',
    missing_components: [],
    install_command: '',
    restart_required: false,
    ...over,
  }
}

const CASES: [string, AcpBackendProbe | undefined, AcpProbeState, boolean, boolean][] = [
  // label, row, state, blocks Use (Settings), confirms Use (first-run setup)
  ['no row', undefined, 'unprobed', false, false],
  ['installed', probe(), 'installed', false, true],
  ['missing', probe({ installed: 'missing', missing_components: ['x'] }), 'missing', true, false],
  ['check failed', probe({ installed: 'unknown' }), 'unknown', false, false],
  ['restart required', probe({ restart_required: true }), 'restart_required', true, false],
  ['unselectable', probe({ selectable: false }), 'unselectable', true, false],
]

describe('acpProbeState', () => {
  it.each(CASES)('%s', (_label, row, state, blocks, confirms) => {
    expect(acpProbeState(row)).toBe(state)
    expect(acpProbeBlocksUse(row)).toBe(blocks)
    expect(acpProbeConfirmsUse(row)).toBe(confirms)
  })

  it('never both blocks and confirms the same row', () => {
    for (const [, row] of CASES) {
      expect(acpProbeBlocksUse(row) && acpProbeConfirmsUse(row)).toBe(false)
    }
  })

  it('ranks the reasons: unselectable, then missing, then restart, then unknown', () => {
    // A build or policy refusal outranks every machine fact: installing or
    // restarting cannot make a refused switch accepted.
    expect(acpProbeState(probe({ selectable: false, installed: 'missing' }))).toBe('unselectable')
    expect(acpProbeState(probe({ selectable: false, restart_required: true }))).toBe('unselectable')
    // The gateway sets restart_required only on an installed verdict; a payload
    // that combined it with the other two still reads as a known block.
    expect(acpProbeState(probe({ installed: 'missing', restart_required: true }))).toBe('missing')
    expect(acpProbeState(probe({ installed: 'unknown', restart_required: true }))).toBe('restart_required')
  })
})
