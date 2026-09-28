/**
 * `truncate` is never correct on an inline `ErrorNotice`'s ROOT, and the reason is
 * inheritance rather than the ellipsis: that root is `inline-flex`, where `text-overflow` is
 * inert, while `white-space: nowrap` DOES inherit into the message span and cancels the
 * `overflow-wrap: anywhere` that span declares for itself. The message then shrinks below
 * min-content and paints over its own sibling controls instead of being clipped.
 *
 * On `messageClassName` the same class is fine: it lands on the message span itself, which
 * is a block container for its own inline content, so the ellipsis applies and `truncate`'s
 * `overflow: hidden` clips instead of overflowing. Only the root spelling is rejected.
 *
 * Scanned at the source rather than rendered: the defect is a class combination across
 * many call sites, and jsdom lays nothing out, so a render proves less than the count.
 */
import { describe, it, expect } from 'vitest'
import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'

const SRC = join(__dirname, '..')
const BLOCK = /<ErrorNotice\b[\s\S]*?\/>/g

function sourceFiles(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name)
    if (entry.isDirectory()) return sourceFiles(path)
    return /\.tsx?$/.test(entry.name) ? [path] : []
  })
}

function inlineNoticeBlocks(): { file: string; block: string }[] {
  return sourceFiles(SRC).flatMap((file) => {
    const text = readFileSync(file, 'utf-8')
    if (!text.includes('ErrorNotice')) return []
    return [...text.matchAll(BLOCK)]
      .map((m) => m[0])
      .filter((block) => block.includes('variant="inline"'))
      .map((block) => ({ file: file.slice(SRC.length + 1), block }))
  })
}

function withoutMessageClass(block: string): string {
  return block.replace(/messageClassName=(?:"[^"]*"|\{[^}]*\})/g, '')
}

describe('inline ErrorNotice call sites', () => {
  it('scans enough call sites for the assertion below to mean anything', () => {
    // Guards against a silent pass: a broken matcher would find nothing and "succeed".
    expect(inlineNoticeBlocks().length).toBeGreaterThan(200)
  })

  it('never passes `truncate` on the ROOT, whose inherited nowrap defeats the message wrap', () => {
    const offenders = inlineNoticeBlocks()
      // `messageClassName` is dropped first: `truncate` there is the supported
      // one-line-ellipsis pattern, and only the root spelling causes the overlap.
      .filter(({ block }) => /\btruncate\b/.test(withoutMessageClass(block)))
      .map(({ file }) => file)
    expect(offenders).toEqual([])
  })

  it('never passes a bare `whitespace-nowrap` anywhere, which is the half that does the damage', () => {
    // Spelling the harmful half directly reproduces the defect, with no inert ellipsis
    // to suggest anything was intended. Harmful on the message span too, where it
    // removes the wrap without bringing the `overflow: hidden` that would clip it.
    const offenders = inlineNoticeBlocks()
      .filter(({ block }) => /\bwhitespace-nowrap\b/.test(block))
      .map(({ file }) => file)
    expect(offenders).toEqual([])
  })
})
