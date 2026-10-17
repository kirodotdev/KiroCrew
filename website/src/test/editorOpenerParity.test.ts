/**
 * `EDITOR_OPENABLE_EXTS` mirrors the desktop shell's open-file allowlist so a
 * surface can skip offering a launch the shell is certain to refuse. The shell's
 * own set stays the security bar; this pins the mirror to it.
 */
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import { EDITOR_OPENABLE_EXTS, editorAcceptsPath } from '../lib/electron'

const REGISTRAR = readFileSync(resolve(process.cwd(), 'electron/ipc-registrar.js'), 'utf8')

function shellAllowlist(): string[] {
  const m = REGISTRAR.match(/const OPENABLE_EXTS = new Set\(\[([\s\S]*?)\]\)/)
  if (!m) throw new Error('OPENABLE_EXTS not found in electron/ipc-registrar.js')
  return [...m[1].matchAll(/"([^"]+)"/g)].map((x) => x[1]).sort()
}

describe('EDITOR_OPENABLE_EXTS', () => {
  it('equals the shell handler allowlist', () => {
    expect([...EDITOR_OPENABLE_EXTS].sort()).toEqual(shellAllowlist())
  })
})

describe('editorAcceptsPath', () => {
  it('accepts an absolute path of an allowlisted type, case-insensitively', () => {
    expect(editorAcceptsPath('/ws/research/FINDINGS.md')).toBe(true)
    expect(editorAcceptsPath('/ws/notes/PLAN.MD')).toBe(true)
    expect(editorAcceptsPath('C:\\Users\\me\\notes.txt')).toBe(true)
  })

  it('refuses a type the shell will not open', () => {
    expect(editorAcceptsPath('/ws/reports/q3-review.pdf')).toBe(false)
    expect(editorAcceptsPath('/ws/data/rows.csv')).toBe(false)
    expect(editorAcceptsPath('/ws/site/index.html')).toBe(false)
    expect(editorAcceptsPath('/ws/README')).toBe(false)
  })

  it('refuses a dotfile with no extension, as the shell handler does', () => {
    expect(editorAcceptsPath('/ws/.env')).toBe(false)
    expect(editorAcceptsPath('/ws/prod.env')).toBe(true)
  })

  it('refuses a relative or home-relative path the shell cannot resolve', () => {
    expect(editorAcceptsPath('notes/plan.md')).toBe(false)
    expect(editorAcceptsPath('./plan.md')).toBe(false)
    expect(editorAcceptsPath('~/notes/plan.md')).toBe(false)
  })
})
