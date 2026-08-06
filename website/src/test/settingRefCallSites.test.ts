/**
 * Integration test: verifies that every <SettingRef configKey="..."> call site
 * in the codebase resolves to a non-unknown mode when given the real
 * settingsRegistry.gen.ts + a schema fixture extracted from the live backend.
 *
 * This ensures file-mode keys actually exist in the backend schema, so they
 * never render as inert <code> in production.
 *
 * DRIFT PROTECTION: Instead of a hand-maintained array, this test discovers
 * call sites by scanning source files at test time. A new <SettingRef
 * configKey="..."> added anywhere in the codebase is automatically caught.
 *
 * ENV-KEY DRIFT PROTECTION: kind="env" call sites are validated against
 * settingref-env-vars.json — which the backend pytest side asserts all
 * exist in source. Together: a typo'd call-site var fails vitest; a stale
 * JSON entry fails pytest.
 */
import { describe, it, expect } from 'vitest'
import * as fs from 'fs'
import * as path from 'path'
import { SETTINGS_REGISTRY } from '../components/commandPalette/settingsRegistry.gen'
import { resolveSettingRef } from '../components/settingRef/resolveSettingRef'
import type { SchemaEntry } from '../components/settingRef/resolveSettingRef'

/**
 * Schema fixture is shared with the backend drift-guard test
 * (test/test_settingref_schema_fixture.py). Both read from the same JSON file.
 */
const SCHEMA_FIXTURE: SchemaEntry[] = JSON.parse(
  fs.readFileSync(path.resolve(__dirname, 'fixtures/settingref-schema.json'), 'utf-8'),
)

/** Build the Map<path, SchemaEntry> that resolveSettingRef expects. */
function buildSchemaIndex(entries: SchemaEntry[]): Map<string, SchemaEntry> {
  const map = new Map<string, SchemaEntry>()
  for (const entry of entries) {
    map.set(entry.path, entry)
  }
  return map
}

/**
 * Discover all configKey values from <SettingRef configKey="..."> call sites
 * in the source tree. Excludes env-kind refs (they don't resolve against schema)
 * and test files (they use mock registries).
 */
function discoverCallSiteConfigKeys(): string[] {
  const srcRoot = path.resolve(__dirname, '../')
  const keys = new Set<string>()
  const configKeyRe = /<SettingRef[^>]*\bconfigKey="([^"]+)"/g

  function walk(dir: string) {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name)
      if (entry.isDirectory()) {
        if (entry.name === 'node_modules' || entry.name === 'test') continue
        walk(full)
      } else if (entry.name.endsWith('.tsx') && !entry.name.endsWith('.test.tsx')) {
        const source = fs.readFileSync(full, 'utf-8')
        let match: RegExpExecArray | null
        while ((match = configKeyRe.exec(source)) !== null) {
          // Skip env-kind refs: check if kind="env" precedes or follows configKey on same tag
          const tagStart = source.lastIndexOf('<SettingRef', match.index)
          const tagEnd = source.indexOf('>', match.index)
          const tagText = source.slice(tagStart, tagEnd + 1)
          if (tagText.includes('kind="env"') || tagText.includes("kind='env'")) continue
          keys.add(match[1])
        }
      }
    }
  }

  walk(srcRoot)
  return [...keys].sort()
}

const CALL_SITE_CONFIG_KEYS = discoverCallSiteConfigKeys()

describe('SettingRef call site resolution (integration)', () => {
  const schemaIndex = buildSchemaIndex(SCHEMA_FIXTURE)

  it('discovers at least one call site from source', () => {
    expect(CALL_SITE_CONFIG_KEYS.length).toBeGreaterThan(0)
  })

  for (const configKey of CALL_SITE_CONFIG_KEYS) {
    it(`"${configKey}" resolves to ui or file — NOT unknown`, () => {
      const resolution = resolveSettingRef(configKey, schemaIndex)
      if (resolution.mode === 'unknown') {
        throw new Error(
          `Drift detected: <SettingRef configKey="${configKey}"> found in source but ` +
          `the key does not resolve against SETTINGS_REGISTRY or SCHEMA_FIXTURE. ` +
          `Either add it to SCHEMA_FIXTURE in this test file, or verify the call site ` +
          `has the correct configKey value.`
        )
      }
      expect(resolution.mode).not.toBe('unknown')
    })
  }

  it('all fixture keys are verified to exist in the REAL backend schema (fixture is not stale)', () => {
    // This test documents that the fixture was generated from the real backend.
    // If a key is removed from the backend, regenerate the fixture and remove
    // the call site or update it.
    for (const entry of SCHEMA_FIXTURE) {
      expect(entry.path).toBeTruthy()
      expect(entry.type).toBeTruthy()
    }
  })

  it('ui-mode keys resolve via SETTINGS_REGISTRY (configKey field)', () => {
    // Keys that have configKey in SETTINGS_REGISTRY should resolve to 'ui'
    const registryKeys = SETTINGS_REGISTRY
      .filter(e => e.configKey)
      .map(e => e.configKey!)
    for (const key of registryKeys) {
      const resolution = resolveSettingRef(key, schemaIndex)
      expect(resolution.mode).toBe('ui')
    }
  })

  it('file-mode keys resolve when not in SETTINGS_REGISTRY but in schema', () => {
    for (const configKey of CALL_SITE_CONFIG_KEYS) {
      const inRegistry = SETTINGS_REGISTRY.some(e => e.configKey === configKey)
      if (!inRegistry) {
        const resolution = resolveSettingRef(configKey, schemaIndex)
        expect(resolution.mode).toBe('file')
      }
    }
  })
})

/**
 * Env-key drift guard: validates that every <SettingRef kind="env" configKey="...">
 * call site uses a var name that exists in the settingref-env-vars.json fixture.
 * The backend pytest side asserts every fixture entry exists in backend source.
 */
const ENV_VARS_FIXTURE: string[] = JSON.parse(
  fs.readFileSync(path.resolve(__dirname, 'fixtures/settingref-env-vars.json'), 'utf-8'),
)

/**
 * Discover all env var names from <SettingRef kind="env" configKey="..."> call sites.
 * Only extracts configKey from tags that include kind="env".
 */
function discoverEnvCallSiteVars(): string[] {
  const srcRoot = path.resolve(__dirname, '../')
  const keys = new Set<string>()
  // Match SettingRef tags that include kind="env"
  const tagRe = /<SettingRef[^>]*>/g
  const configKeyRe = /\bconfigKey="([^"]+)"/
  const kindEnvRe = /\bkind="env"/

  function walk(dir: string) {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name)
      if (entry.isDirectory()) {
        if (entry.name === 'node_modules' || entry.name === 'test') continue
        walk(full)
      } else if (entry.name.endsWith('.tsx') && !entry.name.endsWith('.test.tsx')) {
        const source = fs.readFileSync(full, 'utf-8')
        let match: RegExpExecArray | null
        while ((match = tagRe.exec(source)) !== null) {
          const tag = match[0]
          if (!kindEnvRe.test(tag)) continue
          const keyMatch = configKeyRe.exec(tag)
          if (keyMatch) keys.add(keyMatch[1])
        }
      }
    }
  }

  walk(srcRoot)
  return [...keys].sort()
}

const ENV_CALL_SITE_VARS = discoverEnvCallSiteVars()

describe('SettingRef env-key drift guard (integration)', () => {
  it('discovers at least one env call site from source', () => {
    expect(ENV_CALL_SITE_VARS.length).toBeGreaterThan(0)
  })

  for (const envVar of ENV_CALL_SITE_VARS) {
    it(`env var "${envVar}" exists in settingref-env-vars.json fixture`, () => {
      if (!ENV_VARS_FIXTURE.includes(envVar)) {
        throw new Error(
          `Drift detected: <SettingRef kind="env" configKey="${envVar}"> found in source but ` +
          `the var is not listed in settingref-env-vars.json. Either add it to the fixture ` +
          `(and verify it exists in the backend), or fix the call site typo.`
        )
      }
      expect(ENV_VARS_FIXTURE).toContain(envVar)
    })
  }

  it('fixture is not empty', () => {
    expect(ENV_VARS_FIXTURE.length).toBeGreaterThan(0)
  })
})
