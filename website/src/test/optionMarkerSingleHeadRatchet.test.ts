/**
 * The FRONTEND half of the single-head ratchet.
 *
 * The backend already pins that only one file may define an OPTIONS-family pattern.
 * The frontend had no equivalent, and the gap was not theoretical: three consumers
 * stripped `OPTION_MARKER_RE` and passed `[OPTION-ACTIONS: …]` straight through,
 * because `"[OPTION-ACTIONS:"` does not start with `"[OPTIONS:"` — the two heads
 * diverge at `S` vs `-`, so a `startsWith`-shaped assumption silently misses one.
 *
 * These tests pin the fix at the level of the DEFECT CLASS, not the three instances,
 * so a fourth consumer written the same way fails here instead of shipping.
 */
import { describe, it, expect } from 'vitest'
import fs from 'node:fs'
import path from 'node:path'

import { stripOptionMarkers } from '../app-sdk/protocol/optionMarker'
import { searchableText } from '../utils/searchableText'
import type { ChatMessage } from '../types'

const SRC = path.resolve(__dirname, '..')
const ACTION = '[OPTION-ACTIONS: close=Nothing else, close this tab]'
const CONTENT = '[OPTIONS: Alpha | Beta]'

/** Every `.ts`/`.tsx` under src/, excluding tests and the protocol module itself. */
function consumerFiles(): string[] {
  const out: string[] = []
  const walk = (dir: string) => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name)
      if (entry.isDirectory()) {
        if (entry.name === 'node_modules' || entry.name === 'test') continue
        walk(full)
        continue
      }
      if (!/\.tsx?$/.test(entry.name)) continue
      if (/\.test\.tsx?$/.test(entry.name)) continue
      if (full.includes(path.join('app-sdk', 'protocol'))) continue
      out.push(full)
    }
  }
  walk(SRC)
  return out
}

/**
 * Source with comments removed, so the scan judges CODE and not prose.
 *
 * The backend ratchet strips comments for the same reason, and the reason is
 * measured rather than anticipated: `AssistantMessage.tsx` names one pattern in a
 * comment explaining why the partial-marker strip exists, while its code goes
 * through `parseOptions` and already covers both heads. Matching raw text flagged it
 * as a single-head consumer — a finding about the scan, not about the file.
 */
function codeOnly(body: string): string {
  return body.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^[ \t]*\/\/.*$/gm, '')
}

describe('stripOptionMarkers removes both heads', () => {
  it('strips an action marker, a content marker, and both together', () => {
    expect(stripOptionMarkers(`Body\n${ACTION}`).trim()).toBe('Body')
    expect(stripOptionMarkers(`Body\n${CONTENT}`).trim()).toBe('Body')
    expect(stripOptionMarkers(`Body\n${CONTENT}\n${ACTION}`).trim()).toBe('Body')
  })

  it('leaves ordinary prose untouched', () => {
    // Negative control: proves the strip is not simply eating text.
    const prose = 'The [OPTIONS:] syntax is documented here and stays visible.'
    expect(stripOptionMarkers(prose)).toBe(prose)
  })
})

describe('the search index cannot report a phantom match', () => {
  const msg = (content: string): ChatMessage =>
    ({ role: 'assistant', content }) as unknown as ChatMessage

  it('excludes action-marker text from searchable content', () => {
    // Fails before the fix: the label word was searchable while never rendering as
    // body text, so a hit was counted that the highlighter could not mark.
    expect(searchableText(msg(`Body\n${ACTION}`))).not.toContain('close this tab')
  })

  it('still excludes content-marker text', () => {
    expect(searchableText(msg(`Body\n${CONTENT}`))).not.toContain('Alpha')
  })

  it('keeps prose that really is rendered', () => {
    // Positive control: without this, a strip that removed everything would pass.
    expect(searchableText(msg(`Findable body\n${ACTION}`))).toContain('Findable body')
  })
})

describe('no consumer keys on a single marker head', () => {
  it('every file touching one raw pattern touches the other, or uses the helper', () => {
    const offenders = consumerFiles().filter(file => {
      const body = codeOnly(fs.readFileSync(file, 'utf8'))
      const content = body.includes('OPTION_MARKER_RE')
      const action = body.includes('OPTION_ACTION_MARKER_RE')
      // XOR: naming exactly one raw pattern is the defect shape. Using
      // `stripOptionMarkers` names neither and is the intended path.
      return content !== action
    })
    expect(offenders).toEqual([])
  })

  it('the scan can actually find files, and the predicate flags a single-head one', () => {
    // Positive control on the scan itself. A glob that silently matched nothing
    // would report a clean sweep — the same false all-clear this ratchet exists to
    // catch.
    const files = consumerFiles()
    expect(files.length).toBeGreaterThan(50)
    expect(files.some(f => f.endsWith(path.join('utils', 'searchableText.ts')))).toBe(true)

    // The predicate must FAIL on a synthetic single-head consumer, or the green
    // above would prove nothing.
    const singleHead = codeOnly('const x = OPTION_MARKER_RE\n')
    expect(singleHead.includes('OPTION_MARKER_RE') !== singleHead.includes('OPTION_ACTION_MARKER_RE')).toBe(true)

    // ...and must PASS on a both-heads consumer, so it is not simply always true.
    const bothHeads = codeOnly('const a = OPTION_MARKER_RE, b = OPTION_ACTION_MARKER_RE\n')
    expect(bothHeads.includes('OPTION_MARKER_RE') !== bothHeads.includes('OPTION_ACTION_MARKER_RE')).toBe(false)
  })

  it('comment stripping does not blind the scan to real code', () => {
    // The comment strip is what un-flagged AssistantMessage.tsx, so pin that it
    // removes ONLY comments: a commented mention is ignored, a real reference is not.
    expect(codeOnly('// mentions OPTION_MARKER_RE in prose\n')).not.toContain('OPTION_MARKER_RE')
    expect(codeOnly('const x = OPTION_MARKER_RE // trailing note\n')).toContain('OPTION_MARKER_RE')
  })
})
