import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

import { countImageOpeners, parseMarkdownImageDestination } from '../lib/imageArtifactSlug'

// The backend's grammar (kiro_crew/messaging/outbound_files.py: IMAGE_MD_RE,
// md_destination) and this port must agree byte for byte. One corpus drives
// both test/test_image_grammar_parity.py and this file.
interface Corpus {
  destinations: { rest: string; expect: string | null }[]
  openers: { text: string; count: number }[]
}

const corpus: Corpus = JSON.parse(
  readFileSync(resolve(__dirname, '../../../test/fixtures/image_grammar_parity.json'), 'utf-8'),
)

describe('image grammar parity with the backend', () => {
  it.each(corpus.destinations)('destination %j', ({ rest, expect: want }) => {
    expect(parseMarkdownImageDestination(rest)).toBe(want)
  })

  it.each(corpus.openers)('opener count %j', ({ text, count }) => {
    expect(countImageOpeners(text)).toBe(count)
  })
})
