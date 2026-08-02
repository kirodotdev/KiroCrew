/**
 * The gate that guards the gate.
 *
 * `eslint-plugin-i18next` exempts every literal under an ALL-CAPS variable
 * declarator, and this dashboard keeps its module-level UI-copy tables under
 * exactly that naming convention — `SESSION_FILTERS`, `SORT_OPTIONS`,
 * `EFFORT_DISPLAY`. The result was silent: shipped English (`Unread`,
 * `In progress`, `Newest`, `High`) rendered in every locale while
 * `untranslated-baseline.json` reported those files as clean, so the copy was
 * never counted, scheduled, or translated.
 *
 * `eslint-rules/i18n-strict.js` removes that one behaviour, and
 * `eslint.i18n.strict.config.js` is what the DIFF-SCOPED gates in
 * `scripts/check-i18n-strings.mjs` run. These tests assert the removal from the
 * OUTSIDE — by linting source text with the real config — so they fail if the
 * wrapper stops working, if the gate stops using it, or if a dependency bump
 * changes the upstream visitor.
 *
 * The split itself is asserted too: the per-file CEILINGS deliberately keep the
 * upstream rule, because closing the hole there would force a bulk `--update` of a
 * ledger four open branches share — the one thing `website/AGENTS.md` forbids.
 */

import { describe, it, expect } from 'vitest'
import { ESLint } from 'eslint'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { ALL_CAPS_MARKER } from '../../eslint-rules/i18n-strict.js'

const WEBSITE_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..')

/** Lint one snippet exactly as the DIFF-SCOPED gates do. */
async function lint(code: string, config = 'eslint.i18n.strict.config.js'): Promise<string[]> {
  const engine = new ESLint({
    cwd: WEBSITE_ROOT,
    overrideConfigFile: config,
    // The gate runs with `--no-inline-config`; matching it here means a snippet
    // cannot quietly disable the rule.
    allowInlineConfig: false,
  })
  const [result] = await engine.lintText(code, { filePath: path.join(WEBSITE_ROOT, 'src/probe.tsx') })
  return result.messages.map(m => m.message)
}

describe('i18n-strict/no-literal-string', () => {
  it('reports display copy inside an ALL-CAPS table — the case upstream exempts', async () => {
    const messages = await lint(`
      export const SORT_OPTIONS = [
        { value: 'date-desc', label: 'Newest' },
        { value: 'created-desc', label: 'Created (Newest)' },
      ]
    `)
    expect(messages.join('\n')).toContain('Newest')
    expect(messages).toHaveLength(2)
  })

  it('reports the same copy under a camelCase name — behaviour is unchanged there', async () => {
    const messages = await lint(`export const sortOptions = [{ label: 'Newest' }]`)
    expect(messages).toHaveLength(1)
  })

  it('reports a bare ALL-CAPS string constant holding prose', async () => {
    const messages = await lint(`export const EFFORT_HELP = 'Reasoning effort sets how long the model thinks.'`)
    expect(messages).toHaveLength(1)
  })

  it('stays quiet on machine values in an ALL-CAPS table', async () => {
    // Storage keys, lowercase enum values, class strings and endpoints: the
    // content filters in `eslint.i18n.config.js` still apply, so closing the
    // declarator hole does not turn every constant into a finding.
    const messages = await lint(`
      export const SORT_LS_KEY = 'mc-session-sort'
      export const EFFORT_LEVELS = ['', 'low', 'medium', 'high', 'max']
      export const ENDPOINT = 'https://example.invalid/api'
      export const ROW_CLASS = 'flex items-center gap-2'
    `)
    expect(messages).toEqual([])
  })

  it('stays quiet on a table of CATALOG KEYS — the shape the gate asks for', async () => {
    // `check-i18n-keys.mjs` tells you to replace a dynamic key with a table of
    // literal keys. Those tables are ALL-CAPS module constants, so without the
    // dotted-path exemption this gate would report its own recommended fix.
    const messages = await lint(`
      export const FILTER_LABEL_KEY = {
        unread: 'pages.chatSidebar.filter_unread',
        running: 'pages.chatSidebar.filter_running_description',
      }
      export const EFFORT_LABEL_KEY = { max: 'lib.effort.max' }
    `)
    expect(messages).toEqual([])
  })

  it('is the rule the strict config actually runs', async () => {
    const engine = new ESLint({ cwd: WEBSITE_ROOT, overrideConfigFile: 'eslint.i18n.strict.config.js' })
    const [result] = await engine.lintText(`export const A_B = { label: 'Newest' }`, {
      filePath: path.join(WEBSITE_ROOT, 'src/probe.tsx'),
    })
    // Fails loudly if the strict config is reverted to `i18next/no-literal-string`,
    // which would silently re-open the hole while still reporting other findings.
    expect(result.messages.map(m => m.ruleId)).toEqual(['i18n-strict/no-literal-string'])
  })
})

describe('the ceiling config keeps the upstream rule', () => {
  it('still exempts ALL-CAPS tables, so no ledger re-snapshot is forced', async () => {
    const messages = await lint(
      `export const SORT_OPTIONS = [{ label: 'Newest' }]`,
      'eslint.i18n.config.js',
    )
    expect(messages).toEqual([])
  })

  it('reports the same snippet under a camelCase name, so it is not simply off', async () => {
    const messages = await lint(
      `export const sortOptions = [{ label: 'Newest' }]`,
      'eslint.i18n.config.js',
    )
    expect(messages).toHaveLength(1)
  })

  it('shares every option with the strict config — only the rule differs', async () => {
    const [base, strict] = await Promise.all([
      import('../../eslint.i18n.config.js'),
      import('../../eslint.i18n.strict.config.js'),
    ])
    type Block = { rules?: Record<string, unknown> }
    const opts = (blocks: Block[], id: string) =>
      blocks.map(b => b.rules?.[id]).find(Boolean)
    // Same options object, so an exemption added to one can never be missing
    // from the other.
    expect(opts(strict.default as Block[], 'i18n-strict/no-literal-string'))
      .toEqual(opts(base.default as Block[], 'i18next/no-literal-string'))
  })

  it('carries EVERY block across, including per-file `off` exemptions', async () => {
    const [base, strict] = await Promise.all([
      import('../../eslint.i18n.config.js'),
      import('../../eslint.i18n.strict.config.js'),
    ])
    type Block = { files?: string[], rules?: Record<string, unknown> }
    const carrying = (blocks: Block[], id: string) =>
      blocks
        .map((b, i) => ({ i, files: b.files, options: b.rules?.[id] }))
        .filter(b => b.options !== undefined)

    const inBase = carrying(base.default as Block[], 'i18next/no-literal-string')
    const inStrict = carrying(strict.default as Block[], 'i18n-strict/no-literal-string')

    // The base config accrues per-file `off` overrides over time from PRs that
    // have nothing to do with i18n (`src/lib/commitProfiler.tsx` was the first).
    // Each must land in the strict namespace at the same block: the base rule is
    // not registered for the strict run, so a dropped `off` does not disable
    // anything — it silently re-arms the strict rule on a path the base config
    // released, and the diff gates would then report a file main considers exempt.
    expect(inStrict).toEqual(inBase)
    expect(inBase.length).toBeGreaterThan(1)
    expect(inBase.filter(b => b.options === 'off').length).toBeGreaterThan(0)

    // And the upstream rule must not survive anywhere in the strict config, or
    // ESLint would fail on an unregistered plugin rule.
    expect(carrying(strict.default as Block[], 'i18next/no-literal-string')).toEqual([])
  })
})

describe('the ALL-CAPS marker', () => {
  it('tags only the findings the upstream rule would have hidden', async () => {
    const messages = await lint(`
      export const SORT_OPTIONS = [{ label: 'Newest' }]
      export const sortOptions = [{ label: 'Oldest' }]
    `)
    const marked = messages.filter(m => m.endsWith(ALL_CAPS_MARKER))
    const plain = messages.filter(m => !m.endsWith(ALL_CAPS_MARKER))
    expect(marked).toHaveLength(1)
    expect(marked[0]).toContain('Newest')
    expect(plain).toHaveLength(1)
    expect(plain[0]).toContain('Oldest')
  })

  it('makes the untagged set equal what the ceiling config reports', async () => {
    // This equivalence is the whole basis of the single-pass design: the per-file
    // ceilings are computed from the untagged findings of the STRICT run, so if the
    // two ever diverged the committed ledger would silently start measuring a
    // different population than it was snapshotted from.
    const probe = `
      export const SESSION_FILTERS = [{ label: 'Unread' }]
      export const EFFORT_HELP = 'Reasoning effort sets how long the model thinks.'
      export const helper = { label: 'Created (Newest)' }
      const shown = true ? 'Show details' : 'Hide details'
      export const STORAGE_KEY = 'mc-session-sort'
      export default [shown]
    `
    const strict = await lint(probe)
    const loose = await lint(probe, 'eslint.i18n.config.js')
    const untagged = strict.filter(m => !m.endsWith(ALL_CAPS_MARKER))
    expect(untagged.sort()).toEqual(loose.sort())
    // And the marked ones are exactly the difference, not an empty set.
    expect(strict.length - untagged.length).toBeGreaterThan(0)
  })

  it('nests: a plain declarator inside an ALL-CAPS one is still tagged', async () => {
    // Upstream suppresses on ANY ALL-CAPS ancestor (`indicatorStack.some`), so the
    // marker has to track depth rather than the immediate parent.
    const messages = await lint(`
      export const OUTER = (() => {
        const inner = { label: 'Newest' }
        return inner
      })()
    `)
    expect(messages).toHaveLength(1)
    expect(messages[0].endsWith(ALL_CAPS_MARKER)).toBe(true)
  })
})
