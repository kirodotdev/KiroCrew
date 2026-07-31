/**
 * The `highlight.js` BARREL must not be imported from application code.
 *
 * The barrel registers all ~190 bundled grammars (~200-240 KB gzip) and lands in
 * the eagerly-preloaded `vendor-markdown` chunk. `src/utils/hljs.ts` already
 * wraps `highlight.js/lib/core` with only the grammars the dashboard renders, so
 * every main-thread caller must go through it.
 *
 * Two halves, because a repoint alone regresses on the next import:
 *  1. no source file imports the barrel at runtime;
 *  2. the eslint rule that enforces (1) is actually configured and firing.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync } from 'fs'
import { join } from 'path'
import { ESLint } from 'eslint'

const WEBSITE_ROOT = join(__dirname, '..', '..')
const SRC = join(WEBSITE_ROOT, 'src')

/** Every .ts/.tsx file under src/, tests included. */
function sourceFiles(dir: string): string[] {
  const out: string[] = []
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name)
    if (entry.isDirectory()) out.push(...sourceFiles(path))
    else if (/\.tsx?$/.test(entry.name)) out.push(path)
  }
  return out
}

/** A runtime (value) import of the exact `highlight.js` specifier. */
const BARREL_VALUE_IMPORT = /^\s*import\s+(?!type\b)[^;]*?from\s+['"]highlight\.js['"]/m

describe('highlight.js barrel is not in the eager bundle', () => {
  it('is not value-imported anywhere under src/', () => {
    const offenders = sourceFiles(SRC).filter(path =>
      BARREL_VALUE_IMPORT.test(readFileSync(path, 'utf-8')),
    )
    expect(offenders.map(p => p.slice(WEBSITE_ROOT.length + 1))).toEqual([])
  })

  it('leaves the two former callers pointed at the core build', () => {
    // Named explicitly: these are the files the fix repointed, and a revert
    // reads as "back to the barrel" rather than as a vague global count change.
    for (const rel of ['src/components/ArtifactBody.tsx', 'src/components/MarkdownPanel.tsx']) {
      const src = readFileSync(join(WEBSITE_ROOT, rel), 'utf-8')
      expect(src, rel).toMatch(/import hljs from '\.\.\/utils\/hljs'/)
    }
  })

  it('is rejected by eslint so it cannot come back', async () => {
    const eslint = new ESLint({ cwd: WEBSITE_ROOT })
    const [result] = await eslint.lintText("import hljs from 'highlight.js'\nexport default hljs\n", {
      filePath: join(SRC, 'restrictedImportProbe.ts'),
    })
    const messages = result.messages.filter(m => m.ruleId?.endsWith('no-restricted-imports'))
    expect(messages).toHaveLength(1)
    expect(messages[0].severity).toBe(2)
  }, 30000)

  it('still allows the type-only import the core wrapper needs', async () => {
    // `utils/hljsLanguages.ts` imports `HLJSApi` as a type; that erases at
    // compile time and must not be caught by the rule.
    const eslint = new ESLint({ cwd: WEBSITE_ROOT })
    const [result] = await eslint.lintText("import type { HLJSApi } from 'highlight.js'\nexport type A = HLJSApi\n", {
      filePath: join(SRC, 'restrictedImportProbe.ts'),
    })
    expect(result.messages.filter(m => m.ruleId?.endsWith('no-restricted-imports'))).toEqual([])
  }, 30000)
})
