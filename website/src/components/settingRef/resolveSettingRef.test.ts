/**
 * resolveSettingRef — pure resolution logic unit tests.
 *
 * Covers: mode 'ui' (registry hit), mode 'file' (schema index hit),
 * mode 'unknown' (neither), and security-sensitive input rejection.
 */
import { describe, it, expect, vi } from 'vitest'
import { resolveSettingRef } from './resolveSettingRef'
import type { SchemaEntry } from './resolveSettingRef'

// Mock the generated registry with a known entry that has a configKey
vi.mock('../commandPalette/settingsRegistry.gen', () => ({
  SETTINGS_REGISTRY: [
    {
      id: 'chat.fallback-model',
      label: 'Fallback Model',
      tab: 'chat',
      type: 'select',
      occurrence: 1,
      configKey: 'chat.default_model',
    },
    {
      id: 'session.autocompact-pct',
      label: 'Autocompact %',
      tab: 'session',
      type: 'stepper',
      occurrence: 1,
      configKey: 'session.autocompact_pct',
    },
  ],
}))

describe('resolveSettingRef', () => {
  describe('mode: ui', () => {
    it('resolves a registry entry by configKey', () => {
      const result = resolveSettingRef('chat.default_model')
      expect(result.mode).toBe('ui')
      if (result.mode === 'ui') {
        expect(result.entry.id).toBe('chat.fallback-model')
        expect(result.entry.tab).toBe('chat')
        expect(result.entry.configKey).toBe('chat.default_model')
      }
    })

    it('resolves another registry entry', () => {
      const result = resolveSettingRef('session.autocompact_pct')
      expect(result.mode).toBe('ui')
      if (result.mode === 'ui') {
        expect(result.entry.id).toBe('session.autocompact-pct')
      }
    })
  })

  describe('mode: file', () => {
    it('resolves from schemaIndex when not in registry', () => {
      const schemaIndex = new Map<string, SchemaEntry>([
        ['telemetry.endpoint', { path: 'telemetry.endpoint', type: 'string', label: 'Endpoint' }],
      ])
      const result = resolveSettingRef('telemetry.endpoint', schemaIndex)
      expect(result.mode).toBe('file')
      if (result.mode === 'file') {
        expect(result.schemaEntry.path).toBe('telemetry.endpoint')
      }
    })
  })

  describe('mode: unknown', () => {
    it('returns unknown for a key not in registry or schema (schema loaded)', () => {
      const result = resolveSettingRef('totally.nonexistent.key', new Map())
      expect(result.mode).toBe('unknown')
    })

    it('returns unknown for empty string', () => {
      const result = resolveSettingRef('')
      expect(result.mode).toBe('unknown')
    })

    it('returns unknown for non-string input', () => {
      // @ts-expect-error — testing runtime safety
      const result = resolveSettingRef(undefined)
      expect(result.mode).toBe('unknown')
    })
  })

  describe('security: malicious inputs', () => {
    it('rejects javascript: protocol', () => {
      const result = resolveSettingRef('javascript:alert(1)')
      expect(result.mode).toBe('unknown')
    })

    it('rejects protocol-relative URL', () => {
      const result = resolveSettingRef('//evil.com')
      expect(result.mode).toBe('unknown')
    })

    it('rejects URLs with ://', () => {
      const result = resolveSettingRef('https://evil.com/xss')
      expect(result.mode).toBe('unknown')
    })

    it('rejects path traversal', () => {
      const result = resolveSettingRef('../../etc/passwd')
      expect(result.mode).toBe('unknown')
    })

    it('rejects HTML injection', () => {
      const result = resolveSettingRef('<script>alert(1)</script>')
      expect(result.mode).toBe('unknown')
    })

    it('handles excessively long input as unknown (no crash)', () => {
      const longKey = 'a'.repeat(200)
      const result = resolveSettingRef(longKey, new Map())
      expect(result.mode).toBe('unknown')
    })
  })

  describe('optimistic resolution (schemaIndex=undefined, loading state)', () => {
    it('resolves valid key to file mode with synthetic entry when schema is undefined', () => {
      const result = resolveSettingRef('some.valid.key', undefined)
      expect(result.mode).toBe('file')
      if (result.mode === 'file') {
        expect(result.schemaEntry.path).toBe('some.valid.key')
        expect(result.schemaEntry.type).toBe('unknown')
      }
    })

    it('still resolves registry hits to ui mode regardless of schema state', () => {
      const result = resolveSettingRef('chat.default_model', undefined)
      expect(result.mode).toBe('ui')
    })

    it('rejects malicious keys even when schema is undefined', () => {
      expect(resolveSettingRef('javascript:alert(1)', undefined).mode).toBe('unknown')
      expect(resolveSettingRef('//evil.com', undefined).mode).toBe('unknown')
      expect(resolveSettingRef('', undefined).mode).toBe('unknown')
    })

    it('loaded schema with key absent resolves to unknown (not file)', () => {
      const emptySchema = new Map<string, SchemaEntry>()
      const result = resolveSettingRef('totally.absent.key', emptySchema)
      expect(result.mode).toBe('unknown')
    })
  })
})
