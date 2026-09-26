/**
 * The Remote Crew surfaces used to decide a crew's badge, its hover hint, its
 * address line and its diagnostics transport with four independent chains of
 * conditionals whose last arm was `ssh`. Any method those chains did not name
 * therefore rendered as SSH — and the surfaces disagreed with each other: the
 * same fargate failure reported `transport: ssm` from the settings panel and
 * `transport: ssh` from the viewport, because one chain tested
 * `usesSsmTransport` and the other compared `=== 'ssm'` inline.
 *
 * These tests pin the single mapping that replaced them. The fargate case is the
 * regression test of record for the mislabelling defect; the unknown-method case
 * is what keeps a future transport from inheriting a sibling's identity before
 * its own copy exists.
 *
 * Dependency-free by design, following `remoteCrewConstants.test.ts`: the Python
 * declaration is read as text and extracted with an anchored regex, so a backend
 * rename fails the extraction loudly instead of passing vacuously.
 */
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

import { describe, it, expect } from 'vitest'

import { TRANSPORT_COPY_METHODS } from '../pages/settings/transportCopy'
import {
  DEFAULT_CONNECTION_METHOD,
  PRESENTED_CONNECTION_METHODS,
  normalizeConnectionMethod,
  transportPresentation,
  transportTarget,
} from '../utils/remoteCrew'

const read = (rel: string): string =>
  readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8')

describe('transport presentation mapping', () => {
  it('covers exactly the methods the gateway registers', () => {
    // src/kiro_crew/instances/registry.py:
    //   CONNECTION_METHODS: tuple[str, ...] = ("ssh", "ssm", "fargate")
    const py = read('../../../src/kiro_crew/instances/registry.py')
    const m = /^CONNECTION_METHODS:[^=]*=\s*\(([^)]*)\)/m.exec(py)
    expect(m, 'CONNECTION_METHODS declaration not found in registry.py').not.toBeNull()
    const backendMethods = (m as RegExpExecArray)[1]
      .split(',')
      .map(s => s.trim().replace(/^["']|["']$/g, ''))
      .filter(Boolean)
    expect(backendMethods.length).toBeGreaterThan(0)
    expect([...PRESENTED_CONNECTION_METHODS].sort()).toEqual([...backendMethods].sort())
  })

  it('mirrors the gateway default for a record with no method', () => {
    // registry.py: _DEFAULT_CONNECTION_METHOD = "ssh"
    const py = read('../../../src/kiro_crew/instances/registry.py')
    const m = /^_DEFAULT_CONNECTION_METHOD\s*=\s*["']([^"']+)["']/m.exec(py)
    expect(m, '_DEFAULT_CONNECTION_METHOD declaration not found').not.toBeNull()
    expect(DEFAULT_CONNECTION_METHOD).toBe((m as RegExpExecArray)[1])
  })

  it('has badge copy for every presented method', () => {
    // The copy lives at the call site (literal i18n keys, because the
    // key-reference gate's dynamic-site baseline is ratchet-down only), so this
    // is what keeps the two tables from drifting apart.
    expect([...TRANSPORT_COPY_METHODS].sort()).toEqual([...PRESENTED_CONNECTION_METHODS].sort())
  })

  it('treats an absent or blank method as the default, not as unmapped', () => {
    // An ABSENT method legitimately means ssh; an UNRECOGNISED one does not.
    // Conflating the two is the defect, so both halves are pinned.
    expect(normalizeConnectionMethod(undefined)).toBe('ssh')
    expect(normalizeConnectionMethod('')).toBe('ssh')
    expect(normalizeConnectionMethod('   ')).toBe('ssh')
    expect(transportPresentation({}).mapped).toBe(true)
    expect(transportPresentation({}).reportTransport).toBe('ssh')
  })

  it('normalises case and surrounding space the way the gateway does', () => {
    expect(normalizeConnectionMethod('  SSM ')).toBe('ssm')
    expect(transportPresentation({ connection_method: 'Fargate' }).reportTransport).toBe('fargate')
  })

  it('names fargate as its own transport in a failure report', () => {
    // Regression test of record. Before the mapping, the viewport handoff read
    // `inst?.connection_method === 'ssm' ? 'ssm' : 'ssh'`, so this was 'ssh'.
    expect(transportPresentation({ connection_method: 'fargate' }).reportTransport).toBe('fargate')
    expect(transportPresentation({ connection_method: 'ssm' }).reportTransport).toBe('ssm')
    expect(transportPresentation({ connection_method: 'ssh' }).reportTransport).toBe('ssh')
  })

  it('addresses each method by the field that actually carries its target', () => {
    expect(transportTarget({ connection_method: 'ssh', ssh_host: 'crew-1' })).toBe('crew-1')
    expect(
      transportTarget({ connection_method: 'ssm', ssm_target: 'i-0abc', ssh_host: 'ignored' }),
    ).toBe('i-0abc')
    expect(
      transportTarget({
        connection_method: 'fargate',
        ssm_target: 'ecs:cluster_aaa_bbb',
        ssh_host: 'ignored',
      }),
    ).toBe('ecs:cluster_aaa_bbb')
  })

  describe('a method this build does not know', () => {
    const unknown = { connection_method: 'outbound', ssh_host: 'leaked-host', ssm_target: 'leaked' }

    it('is marked unmapped rather than silently resolved', () => {
      expect(transportPresentation(unknown).mapped).toBe(false)
      expect(transportPresentation(unknown).method).toBe('outbound')
    })

    it('borrows no sibling transport name for its repair steps', () => {
      const { reportTransport } = transportPresentation(unknown)
      expect(reportTransport).toBe('outbound')
      expect(reportTransport).not.toBe('ssh')
      expect(reportTransport).not.toBe('ssm')
    })

    it('borrows no sibling address field', () => {
      expect(transportPresentation(unknown).addressField).toBeNull()
      // Not 'leaked-host': an empty SSH host reads as a configured-but-blank SSH
      // crew, which is the failure mode the old ternary produced.
      expect(transportTarget(unknown)).toBe('')
    })

    it('has no badge copy, so the surface falls back to the raw method name', () => {
      expect(TRANSPORT_COPY_METHODS).not.toContain('outbound')
    })
  })
})
