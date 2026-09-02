import { readFileSync } from 'node:fs'
import path from 'node:path'

import { describe, expect, it } from 'vitest'

import {
  hasKeepVisibleMarker,
  stripKeepVisibleMarker,
} from '../app-sdk/protocol/keepVisibleMarker'

describe('keepVisibleMarker recognizer (#7948, round-8 grammar)', () => {
  it('strips a trailing marker line and fires the exemption', () => {
    const text = 'report body\n<!-- keep-visible -->'
    expect(stripKeepVisibleMarker(text)).toBe('report body')
    expect(hasKeepVisibleMarker(text)).toBe(true)
  })

  it('tolerates stacked sibling control tags after the marker (Design round-8)', () => {
    // A deliver/plan_task_id line after the marker must not void the
    // exemption, and the whole trailing block strips from copy/search.
    const text = 'report\n<!-- keep-visible -->\n<!-- deliver:slack -->'
    expect(hasKeepVisibleMarker(text)).toBe(true)
    expect(stripKeepVisibleMarker(text)).toBe('report')
  })

  it('rejects a tail inside an unterminated fence — visible code (GPT round-8)', () => {
    // The renderer shows every line of an open fence literally, so the
    // marker line is visible content: no exemption, no strip. Backticks
    // and tildes both.
    for (const fence of ['```', '~~~html']) {
      const text = `${fence}\n<!-- keep-visible -->`
      expect(hasKeepVisibleMarker(text)).toBe(false)
      expect(stripKeepVisibleMarker(text)).toBe(text)
    }
  })

  it('a closed fence earlier in the message does not disable the tail strip', () => {
    const text = '```\ncode\n```\ndone\n<!-- keep-visible -->'
    expect(hasKeepVisibleMarker(text)).toBe(true)
    expect(stripKeepVisibleMarker(text)).toBe('```\ncode\n```\ndone')
  })

  it('mid-body and same-line markers are rendered content', () => {
    expect(hasKeepVisibleMarker('a <!-- keep-visible --> b')).toBe(false)
    expect(hasKeepVisibleMarker('prose tail <!-- keep-visible -->')).toBe(false)
  })
})

describe('shared conformance corpus (parity pin with backend, Design round-9)', () => {
  // Same fixture asserted by test/test_display_safe_control_tags.py on the
  // Python side; a bound or grammar edited on one side goes red on the other.
  const corpusPath = path.resolve(
    __dirname,
    '../../../test/fixtures/control_tag_corpus.json',
  )
  const corpus = JSON.parse(readFileSync(corpusPath, 'utf-8')) as {
    cases: {
      name: string
      input: string
      frontend_stripped: string
      exempt: boolean
    }[]
  }

  it.each(corpus.cases)('$name', ({ input, frontend_stripped, exempt }) => {
    expect(stripKeepVisibleMarker(input)).toBe(frontend_stripped)
    expect(hasKeepVisibleMarker(input)).toBe(exempt)
  })
})
