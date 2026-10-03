import { describe, expect, it } from 'vitest'

import {
  buildImageOrdinalMap,
  MAX_IMAGE_DESTINATION_CHARS,
  imageDestinationAt,
  imageOrdinalCandidates,
  parseMarkdownImageDestination,
  scanImageOpeners,
} from '../lib/imageArtifactSlug'

// Mirrors `md_destination` / `IMAGE_MD_RE` in kiro_crew/messaging/outbound_files.py
// and the numbering in kiro_crew/image_artifacts.register_images.
describe('parseMarkdownImageDestination', () => {
  it('parses a plain destination and drops a title', () => {
    expect(parseMarkdownImageDestination('/tmp/a.png)')).toBe('/tmp/a.png')
    expect(parseMarkdownImageDestination('/tmp/a.png "shot")')).toBe('/tmp/a.png')
  })

  it('keeps balanced parens and honours markdown escapes only', () => {
    expect(parseMarkdownImageDestination('/tmp/shot(1).png)')).toBe('/tmp/shot(1).png')
    expect(parseMarkdownImageDestination('/tmp/a\\).png)')).toBe('/tmp/a).png')
    // A Windows path keeps its separators: `\U` is not an escapable character.
    expect(parseMarkdownImageDestination('C:\\Users\\me\\shot.png)')).toBe('C:\\Users\\me\\shot.png')
  })

  it('unwraps <...> so a spaced path survives whole', () => {
    expect(parseMarkdownImageDestination('</tmp/generated images/c.png>)')).toBe('/tmp/generated images/c.png')
    expect(parseMarkdownImageDestination('</tmp/unterminated)')).toBeNull()
  })

  it('rejects unterminated, multi-line and control-character destinations', () => {
    expect(parseMarkdownImageDestination('/tmp/never-closes.png')).toBeNull()
    expect(parseMarkdownImageDestination('/tmp/a\n.png)')).toBeNull()
    expect(parseMarkdownImageDestination('/tmp/a\u0001.png)')).toBeNull()
  })
})

describe('buildImageOrdinalMap', () => {
  it('numbers every direct opener in raw order, including ones the renderer never shows', () => {
    const raw = [
      '```md',
      '![fenced](/tmp/fenced.png)',            // 0
      '```',
      '<mcwidget title="![ghost](/tmp/ghost.png)"></mcwidget>',  // 1
      '<tool_use>![stray](/tmp/stray.png)</tool_use>',           // 2
      '![ref][r]',                              // reference-style: not an opener
      '![real](/tmp/real.png)',                 // 3
      '![real](/tmp/real.png)',                 // 4 -> same file, first ordinal kept
      '![remote](https://example.com/x.png)',   // 5
      '',
      '[r]: /tmp/ref.png',
    ].join('\n')
    const map = buildImageOrdinalMap(raw)
    expect(map.get('/tmp/fenced.png')).toEqual([0])
    expect(map.get('/tmp/ghost.png')).toEqual([1])
    expect(map.get('/tmp/stray.png')).toEqual([2])
    expect(map.get('/tmp/real.png')).toEqual([3, 4])
    expect(map.get('https://example.com/x.png')).toEqual([5])
    expect(map.has('/tmp/ref.png')).toBe(false)
  })

  it('carries the scan: ordinal -> opener start and destination, unparsed openers included', () => {
    const raw = '![a](/tmp/a.png) ![never](/tmp/open ![b](/tmp/b.png)'
    const map = buildImageOrdinalMap(raw)
    expect(map.openers.map(o => o.start)).toEqual([0, raw.indexOf('![never]'), raw.indexOf('![b]')])
    // The unterminated opener is numbered (the backend numbers every match) but
    // has no destination; the opener inside its would-be destination closes.
    expect(map.openers.map(o => o.dest)).toEqual(['/tmp/a.png', null, '/tmp/b.png'])
    expect(map.get('/tmp/b.png')).toEqual([2])
  })

  it('gives up on a destination past the bound instead of walking the message', () => {
    const long = '/tmp/' + 'x'.repeat(MAX_IMAGE_DESTINATION_CHARS) + '.png'
    expect(parseMarkdownImageDestination(`${long})`)).toBeNull()
    const justUnder = '/tmp/' + 'x'.repeat(MAX_IMAGE_DESTINATION_CHARS - 10) + '.png'
    expect(parseMarkdownImageDestination(`${justUnder})`)).toBe(justUnder)
  })

  it('reads the destination in place from an offset, escapes included', () => {
    const text = 'lead ![a](/tmp/sh\\)ot(1).png "t") tail'
    const start = text.indexOf('](') + 2
    expect(parseMarkdownImageDestination(text, start)).toBe('/tmp/sh)ot(1).png')
    // The bound is measured from the offset, not from the start of the text.
    const padded = 'p'.repeat(100) + '/tmp/' + 'x'.repeat(MAX_IMAGE_DESTINATION_CHARS - 10) + '.png)'
    expect(parseMarkdownImageDestination(padded, 100)).toBe(padded.slice(100, -1))
    expect(parseMarkdownImageDestination(padded, 90)).toBeNull()
  })

  it('scans a message dense with openers in time linear in the destinations found', () => {
    // 20,000 short openers back to back: a per-opener slice of the 32 KiB bound
    // would copy ~650 MB here; the in-place walk copies only the destinations.
    const count = 20_000
    const raw = Array.from({ length: count }, (_, i) => `![i](/tmp/${i}.png)`).join('')
    const started = performance.now()
    const openers = scanImageOpeners(raw)
    const elapsed = performance.now() - started
    expect(openers).toHaveLength(count)
    expect(openers[0].dest).toBe('/tmp/0.png')
    expect(openers[count - 1].dest).toBe(`/tmp/${count - 1}.png`)
    expect(elapsed).toBeLessThan(2_000)
  })

  it('accepts a Windows extended-length path the backend can register', () => {
    // 32,767 characters is the longest path Windows opens (extended-length,
    // `\\\\?\\` semantics); the backend has no destination bound of its own, so
    // an image it stored behind such a path must still resolve here or the
    // fallback silently never fires for it.
    const drive = 'C:/'
    const win = drive + 'y'.repeat(32_767 - drive.length - 4) + '.png'
    expect(win.length).toBe(32_767)
    expect(parseMarkdownImageDestination(`${win})`)).toBe(win)
  })

  it('finds exactly the openers the reference regex finds, including escapes', () => {
    // The hand walker replaces `IMAGE_MD_RE`; the regex stays the oracle.
    const oracle = /!\[(?:\\.|[^\]\\])*\]\(/g
    const cases = [
      '![a](x) ![b](y)',
      '![a\\](x](y)',
      '![\\]](z)',
      '![no close (x)',
      '![a]no paren ![b](y)',
      '![![nested](y)',
      '![a\\\n](x) ![b](y)',
      '![trailing\\',
      '\\![escaped](x) ![real](y)',
      'text ![a](x)![b](y)![c](z)',
      '![]() ![ ](x)',
    ]
    for (const text of cases) {
      const expected: number[] = []
      oracle.lastIndex = 0
      let m: RegExpExecArray | null
      while ((m = oracle.exec(text)) !== null) expected.push(m.index)
      expect(scanImageOpeners(text).map(o => o.start), JSON.stringify(text)).toEqual(expected)
    }
  })

  it('stays linear on a message full of unterminated alt text', () => {
    // `![` repeated with no closing `]`: a regex retries the alt walk at every
    // opener, so N openers before an L-char tail cost N × L. The walker resumes
    // where the failed attempt stopped, so the whole message is walked once.
    const raw = '!['.repeat(20_000) + 'x'.repeat(1_000_000)
    const started = performance.now()
    const map = buildImageOrdinalMap(raw)
    const elapsed = performance.now() - started
    expect(map.openers).toHaveLength(0)
    expect(elapsed).toBeLessThan(10_000)
  })

  it('stays linear on a message full of unterminated openers', () => {
    // Before the bound, each unterminated opener rescanned the REST of the
    // message, so openers followed by a long tail cost openers × tail: here
    // 2,000 × 1 MB, tens of seconds. Bounded, each costs at most 32,768 chars
    // (~65M in total, well under the 10 s bound even on a slow runner).
    const n = 2_000
    const raw = Array.from({ length: n }, (_, i) => `![u${i}](/tmp/never`).join(' ')
      + ' ' + 'x'.repeat(1_000_000)
    const started = performance.now()
    const map = buildImageOrdinalMap(raw)
    const elapsed = performance.now() - started
    expect(map.openers).toHaveLength(n)
    expect(map.size).toBe(0)
    expect(elapsed).toBeLessThan(10_000)
  })
})

describe('imageOrdinalCandidates', () => {
  const raw = '![a](/tmp/a.png)\n![b](/tmp/b.png)\n![a](/tmp/a.png)\n![a](/tmp/a.png)'
  const map = buildImageOrdinalMap(raw)
  const whole = { message: raw, blockStart: 0, blockEnd: raw.length }

  it('resolves a unique destination without needing the raw span', () => {
    expect(imageOrdinalCandidates(raw, raw.indexOf('![b]'), map)).toEqual([1])
  })

  it('resolves a repeated destination to exactly its own ordinal when the raw span is known', () => {
    // The other copies of the same destination are never offered: a source
    // file rewritten between two captures leaves them holding different bytes.
    expect(imageOrdinalCandidates(raw, raw.lastIndexOf('![a]'), map, whole)).toEqual([3])
    expect(imageOrdinalCandidates(raw, 0, map, whole)).toEqual([0])
  })

  it('never guesses: a repeated destination without a raw span gets no fallback', () => {
    expect(imageOrdinalCandidates(raw, 0, map)).toEqual([])
  })

  it('returns nothing for a reference-style image, an unknown destination, or no map', () => {
    const text = '![r][ref]\n![z](/tmp/z.png)'
    expect(imageOrdinalCandidates(text, 0, map)).toEqual([])
    expect(imageOrdinalCandidates(text, text.indexOf('![z]'), map)).toEqual([])
    expect(imageOrdinalCandidates(raw, 0, null)).toEqual([])
  })

  it('maps a block-local occurrence onto the raw occurrences before the block', () => {
    // Raw: a fenced copy (0), then a markdown block whose rendered text equals
    // its raw slice: the block's only image is raw ordinal 1.
    const message = '```\n![a](/tmp/a.png)\n```\n![a](/tmp/a.png)'
    const block = '![a](/tmp/a.png)'
    const blockStart = message.lastIndexOf(block)
    const m = buildImageOrdinalMap(message)
    expect(imageOrdinalCandidates(block, 0, m, {
      message, blockStart, blockEnd: blockStart + block.length,
    })).toEqual([1])
  })

  it('gives no fallback when preprocessing removed a copy from the block (counts disagree)', () => {
    // The raw block holds two openers (one inside a stray tag the renderer
    // strips) but the rendered text holds one: the occurrence is ambiguous.
    const rawBlock = '<tool_use>![a](/tmp/a.png)</tool_use>\n![a](/tmp/a.png)'
    const rendered = '\n![a](/tmp/a.png)'
    const m = buildImageOrdinalMap(rawBlock)
    expect(imageOrdinalCandidates(rendered, 1, m, {
      message: rawBlock, blockStart: 0, blockEnd: rawBlock.length,
    })).toEqual([])
  })
})

describe('imageDestinationAt', () => {
  it('reads the destination only when a direct opener starts exactly at the offset', () => {
    const text = 'see ![a](/tmp/a.png) and ![b][ref]'
    expect(imageDestinationAt(text, text.indexOf('![a]'))).toBe('/tmp/a.png')
    expect(imageDestinationAt(text, text.indexOf('![b]'))).toBeNull()
    expect(imageDestinationAt(text, 0)).toBeNull()
  })
})
