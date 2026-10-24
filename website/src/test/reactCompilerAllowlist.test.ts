/**
 * The React Compiler opt-in list (react-compiler.config.mjs) holds only files
 * the compiler compiles completely. The build runs the compiler with its
 * default panicThreshold, which emits a bailed function as written, so without
 * this test a listed component could stop being compiled and nothing would say
 * so. Each listed file is compiled here with the build's options and fails on
 * any function the compiler reports as bailed or skipped.
 *
 * The control cases pin the detector itself. The clean control carries a
 * destructuring default, which Babel 8 with compiler 1.0 reports as a false
 * error, so a Babel major bump that breaks the scan fails here first.
 */
import { describe, expect, it } from 'vitest'
import { existsSync, readFileSync } from 'node:fs'
import path from 'node:path'
import { transformSync } from '@babel/core'
import reactCompiler from 'babel-plugin-react-compiler'
import {
  REACT_COMPILER_FILES,
  REACT_COMPILER_OPTIONS,
  reactCompilerIdFilter,
} from '../../react-compiler.config.mjs'

const WEBSITE = path.resolve(__dirname, '../..')

interface CompileReport {
  compiled: string[]
  problems: string[]
}

function compile(code: string, filename: string): CompileReport {
  const report: CompileReport = { compiled: [], problems: [] }
  const logger = {
    logEvent(_file: string | null, e: {
      kind: string
      fnName?: string | null
      fnLoc?: { start: { line: number } } | null
      reason?: string
      detail?: { reason?: string; options?: { reason?: string } }
      data?: unknown
    }) {
      const line = e.fnLoc ? `line ${e.fnLoc.start.line}` : 'file'
      if (e.kind === 'CompileSuccess') report.compiled.push(e.fnName ?? line)
      else if (e.kind === 'CompileError') report.problems.push(`${line}: ${e.detail?.options?.reason ?? e.detail?.reason ?? 'CompileError'}`)
      else if (e.kind === 'CompileSkip') report.problems.push(`${line}: skipped (${e.reason ?? 'no reason'})`)
      else if (e.kind === 'PipelineError') report.problems.push(`${line}: pipeline error ${String(e.data).slice(0, 200)}`)
    },
  }
  transformSync(code, {
    filename,
    babelrc: false,
    configFile: false,
    code: false,
    parserOpts: { plugins: ['typescript', ...(filename.endsWith('x') ? ['jsx' as const] : [])] },
    plugins: [[reactCompiler, { ...REACT_COMPILER_OPTIONS, panicThreshold: 'none', logger }]],
  })
  return report
}

describe('React Compiler allowlist', () => {
  it('lists unique, sorted, existing non-test source files', () => {
    expect([...new Set(REACT_COMPILER_FILES)]).toEqual(REACT_COMPILER_FILES)
    expect([...REACT_COMPILER_FILES].sort()).toEqual(REACT_COMPILER_FILES)
    for (const f of REACT_COMPILER_FILES) {
      expect(f).toMatch(/^src\/[^\\]+\.tsx?$/)
      expect(f).not.toMatch(/\.(test|spec|stories)\.tsx?$/)
      expect(existsSync(path.join(WEBSITE, f)), f).toBe(true)
    }
  })

  it.each(REACT_COMPILER_FILES)('%s compiles with no bailout', (f) => {
    const report = compile(readFileSync(path.join(WEBSITE, f), 'utf8'), path.join(WEBSITE, f))
    expect(report.problems, `${f} bails; fix it or drop it from react-compiler.config.mjs`).toEqual([])
    expect(report.compiled.length, `${f} has no component or hook to compile`).toBeGreaterThan(0)
  })
})

describe('React Compiler bailout detector', () => {
  it('compiles a clean component, destructuring default included', () => {
    const report = compile(
      'export function Badge({ label = "x" }: { label?: string }) { return <span>{label}</span> }\n',
      path.join(WEBSITE, 'src/Badge.tsx'),
    )
    expect(report).toEqual({ compiled: ['Badge'], problems: [] })
  })

  it('reports a ref read during render', () => {
    const report = compile(
      'import { useRef } from "react"\n' +
        'export function Latest({ v }: { v: number }) { const r = useRef(v); return <i>{r.current}</i> }\n',
      path.join(WEBSITE, 'src/Latest.tsx'),
    )
    expect(report.compiled).toEqual([])
    expect(report.problems).toHaveLength(1)
  })

  it('reports a "use no memo" opt-out', () => {
    const report = compile(
      'export function Plain() { "use no memo"; return <b /> }\n',
      path.join(WEBSITE, 'src/Plain.tsx'),
    )
    expect(report.compiled).toEqual([])
    expect(report.problems).toHaveLength(1)
  })
})

describe('reactCompilerIdFilter', () => {
  const files = ['src/components/ChatInput.tsx', 'src/pages/ChatPage.tsx']

  it('is null for an empty list', () => {
    expect(reactCompilerIdFilter('/w/website', [])).toBeNull()
  })

  it('matches listed ids with or without a query, and nothing else', () => {
    const re = reactCompilerIdFilter('/w/website/', files)!
    expect(re.test('/w/website/src/components/ChatInput.tsx')).toBe(true)
    expect(re.test('/w/website/src/pages/ChatPage.tsx?v=1')).toBe(true)
    expect(re.test('/w/website/src/components/ChatInputBar.tsx')).toBe(false)
    expect(re.test('/w/website/src/components/ChatInput.tsx.bak')).toBe(false)
    expect(re.test('/x/w/website/src/pages/ChatPage.tsx')).toBe(false)
    expect(re.test('/w/websiteXsrc/pages/ChatPage.tsx')).toBe(false)
  })

  it('normalizes a Windows root to the forward-slash ids Vite uses', () => {
    const re = reactCompilerIdFilter('C:\\w\\website', files)!
    expect(re.test('C:/w/website/src/components/ChatInput.tsx')).toBe(true)
  })
})
