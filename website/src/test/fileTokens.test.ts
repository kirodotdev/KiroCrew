import { describe, it, expect } from 'vitest'
import { addPendingFile, hasExactRelMention, prepareSendPayload, buildFileLabels, resolveFileSegment, mdImageDest, mdImageDestToPath } from '../utils/fileTokens'

describe('buildFileLabels uniqueness', () => {
  it('disambiguates paths that share a basename', () => {
    const m = buildFileLabels(['/q3/report.docx', '/q4/report.docx'])
    expect(m.get('/q3/report.docx')).toBe('q3/report.docx')
    expect(m.get('/q4/report.docx')).toBe('q4/report.docx')
  })

  it('widens past two segments when the last two also collide', () => {
    // Regression: both collapsed to `x/report.docx`, so two distinct
    // attachments got the same chip label and the same mentionMap key.
    const m = buildFileLabels(['/a/x/report.docx', '/b/x/report.docx'])
    expect(new Set([...m.values()]).size, 'labels must be distinct').toBe(2)
    expect(m.get('/a/x/report.docx')).not.toBe(m.get('/b/x/report.docx'))
  })

  it('leaves an already-unique basename alone', () => {
    const m = buildFileLabels(['/repo/notes.txt', '/repo/other.txt'])
    expect(m.get('/repo/notes.txt')).toBe('notes.txt')
    expect(m.get('/repo/other.txt')).toBe('other.txt')
  })

  it('keeps a colliding mention resolvable to its own path', () => {
    // The label is also the mentionMap key, so a collision made one path
    // unreachable from its own chip.
    const content = '[attached_file 1] /a/x/report.docx\n[attached_file 2] /b/x/report.docx'
    const r = resolveFileSegment(content, ['/a/x/report.docx', '/b/x/report.docx'])
    const targets = new Set([...r.mentionMap.values(), ...r.cardPaths])
    expect(targets.has('/a/x/report.docx')).toBe(true)
    expect(targets.has('/b/x/report.docx')).toBe(true)
  })
})

describe('prepareSendPayload', () => {
  it('does not corrupt an incidental mid-word substring that happens to look like a mention', () => {
    // Regression: the GPT-review-reported corruption path. If `foo@README.md`
    // is unrelated typed text and README.md is ALSO a staged attachment (e.g.
    // added via the tree menu's "Add to chat"), a boundary-less token match
    // would splice `[attached_file N] ...` into the MIDDLE of that word. The
    // token-replacement pass (buildRelMap/replaceTokens, via tokenRegex's left
    // boundary) must leave `foo@README.md` untouched and only touch a REAL,
    // boundary-checked mention elsewhere in the text.
    const result = prepareSendPayload('foo@README.md see @README.md', ['/proj/README.md'])
    expect(result.txt).toContain('foo@README.md')
    expect(result.txt).not.toMatch(/foo\[attached_file/)
    expect(result.txt).toContain('[attached_file 1] /proj/README.md')
  })

  it('includes non-image files without @-mention', () => {
    const result = prepareSendPayload('hello', ['/tmp/data.csv'])
    expect(result.txt).toContain('[attached_file 1]')
    expect(result.txt).toContain('/tmp/data.csv')
    expect(result.filePaths).toEqual(['/tmp/data.csv'])
  })

  it('includes image files as markdown', () => {
    const result = prepareSendPayload('check this', ['/tmp/photo.png'])
    expect(result.txt).toContain('![image](/tmp/photo.png)')
    expect(result.imgPaths).toEqual(['/tmp/photo.png'])
  })

  it('separates the image from the message text with a blank line in displayTxt', () => {
    const result = prepareSendPayload('my caption', ['/tmp/photo.png'])
    // Blank line (Markdown paragraph break) so the image renders in its own
    // block and the text drops to the next line, not inline after the image.
    expect(result.displayTxt).toBe('![image](/tmp/photo.png)\n\nmy caption')
    expect(result.displayTxt).toContain('![image](/tmp/photo.png)\n\nmy caption')
  })

  it('emits image-only displayTxt with no trailing separator when text is empty', () => {
    const result = prepareSendPayload('', ['/tmp/photo.png'])
    expect(result.displayTxt).toBe('![image](/tmp/photo.png)')
  })

  it('separates the image from the message text with a blank line in txt (LLM-facing)', () => {
    const result = prepareSendPayload('my caption', ['/tmp/photo.png'])
    // The blank-line separation is persisted in the LLM-facing `txt`, not just
    // the optimistic displayTxt, so the image renders in its own block on every
    // surface that replays stored content (dashboard re-render after a turn,
    // gateway restart, Slack replay, exports) — the original bug was the
    // single-'\n' persisted content collapsing image + caption onto one line.
    expect(result.txt).toBe('![image](/tmp/photo.png)\n\nmy caption')
  })

  it('emits image-only txt with no trailing separator when text is empty', () => {
    const result = prepareSendPayload('', ['/tmp/photo.png'])
    expect(result.txt).toBe('![image](/tmp/photo.png)')
  })

  it('separates image from caption with a blank line but keeps single newline to appended file tokens', () => {
    const result = prepareSendPayload('my caption', ['/tmp/photo.png', '/tmp/data.csv'])
    // image block -> blank line -> caption -> single newline -> [attached_file].
    expect(result.txt).toBe(
      '![image](/tmp/photo.png)\n\nmy caption\n[attached_file 1] /tmp/data.csv',
    )
  })

  it('includes mixed image and non-image files', () => {
    const result = prepareSendPayload('here', ['/tmp/a.png', '/tmp/b.zip'])
    expect(result.imgPaths).toEqual(['/tmp/a.png'])
    expect(result.filePaths).toEqual(['/tmp/b.zip'])
    expect(result.txt).toContain('![image]')
    expect(result.txt).toContain('[attached_file')
    expect(result.displayTxt).not.toContain('[attached_file')
    expect(result.displayTxt).toContain('![image]')
  })

  // Issue #3497: on Windows the upload endpoint returns backslash paths
  // (`C:\Users\me\.kiro\crew\uploads\x.png`). In a markdown destination
  // CommonMark eats `\` before punctuation (`\.` -> `.`), mangling the path,
  // and the drive letter parses as an unknown `c:` scheme that the URL
  // sanitizer empties — so the sender's bubble rendered no image at all.
  describe('windows image paths (issue #3497)', () => {
    it('emits a drive path in forward-slash form in both txt and displayTxt', () => {
      const result = prepareSendPayload('caption', ['C:\\Users\\me\\.kiro\\crew\\uploads\\shot.png'])
      expect(result.txt).toContain('![image](C:/Users/me/.kiro/crew/uploads/shot.png)')
      expect(result.displayTxt).toContain('![image](C:/Users/me/.kiro/crew/uploads/shot.png)')
      // imgPaths keeps the original path — it is the server-side identity.
      expect(result.imgPaths).toEqual(['C:\\Users\\me\\.kiro\\crew\\uploads\\shot.png'])
    })

    it('wraps a destination containing spaces in angle brackets', () => {
      const result = prepareSendPayload('', ['C:\\Users\\John Doe\\uploads\\shot.png'])
      expect(result.displayTxt).toBe('![image](<C:/Users/John Doe/uploads/shot.png>)')
    })

    it('wraps a POSIX destination containing spaces without touching separators', () => {
      const result = prepareSendPayload('', ['/tmp/my shots/pic.png'])
      expect(result.displayTxt).toBe('![image](</tmp/my shots/pic.png>)')
    })

    it('normalizes a UNC share path to forward slashes (roaming profiles)', () => {
      const result = prepareSendPayload('', ['\\\\fileserver\\home\\me\\.kiro\\crew\\uploads\\shot.png'])
      expect(result.displayTxt).toBe('![image](//fileserver/home/me/.kiro/crew/uploads/shot.png)')
    })

    it('wraps and escapes a literal % so consumers decode only marked forms', () => {
      // The wrap is the provenance marker: without it, a legacy destination
      // containing `%20` would be indistinguishable from producer-encoded
      // output and a file literally named `photo%20copy.png` would decode to
      // `photo copy.png` and fetch the wrong file.
      const result = prepareSendPayload('', ['/tmp/photo%20copy.png'])
      expect(result.displayTxt).toBe('![image](</tmp/photo%2520copy.png>)')
    })

    it('escapes backslash-before-punctuation via the bracketed form (POSIX)', () => {
      // `\.` in a plain destination is a CommonMark escape that collapses to
      // `.` — the wrap + escape keeps the on-disk name intact.
      const result = prepareSendPayload('', ['/tmp/my dir\\.hidden.png'])
      expect(result.displayTxt).toBe('![image](</tmp/my dir\\\\.hidden.png>)')
    })

    it('escapes angle brackets inside the bracketed form', () => {
      const result = prepareSendPayload('', ['/tmp/a <b>.png'])
      expect(result.displayTxt).toBe('![image](</tmp/a \\<b\\>.png>)')
    })

    it('wraps a POSIX path containing a backslash (letter-follow case included)', () => {
      // Any backslash routes into the escaped bracketed form — `\n` after `\`
      // is not a CommonMark escape, but wrapping uniformly keeps one rule.
      const result = prepareSendPayload('', ['/tmp/weird\\name.png'])
      expect(result.displayTxt).toBe('![image](</tmp/weird\\\\name.png>)')
    })

    it('wraps a parenthesized duplicate-name path without escaping the parens', () => {
      // Parens are legal inside CommonMark's <…> destination.
      const result = prepareSendPayload('', ['/tmp/screenshot (1).png'])
      expect(result.displayTxt).toBe('![image](</tmp/screenshot (1).png>)')
    })

    it('mdImageDestToPath is the full inverse of mdImageDest', () => {
      // Already-forward-slashed inputs (slash normalization is one-way).
      for (const p of [
        '/tmp/photo.png',
        '/tmp/screenshot (1).png',
        'C:/Users/John Doe/uploads/shot.png',
        '/tmp/my dir\\.hidden.png',
        '/tmp/a <b>.png',
        '//fileserver/home/me/shot.png',
        '/tmp/photo%20copy.png',
        '/tmp/100%.png',
      ]) {
        expect(mdImageDestToPath(mdImageDest(p))).toBe(p)
      }
    })

    it('mdImageDestToPath preserves an unwrapped legacy destination verbatim', () => {
      // Pre-existing history was written raw: `%20` there is part of the
      // on-disk name, not an encoding.
      expect(mdImageDestToPath('/tmp/photo%20copy.png')).toBe('/tmp/photo%20copy.png')
    })
  })

  it('includes @-referenced files inline and unreferenced as appended tokens', () => {
    const result = prepareSendPayload(
      'see @data.csv for details',
      ['/tmp/data.csv', '/tmp/extra.log'],
    )
    expect(result.filePaths).toContain('/tmp/data.csv')
    expect(result.filePaths).toContain('/tmp/extra.log')
    expect(result.txt).toContain('/tmp/extra.log')
    expect(result.displayTxt).not.toContain('[attached_file')
    expect(result.displayTxt).not.toContain('/tmp/extra.log')
  })

  it('returns empty filePaths when no files pending', () => {
    const result = prepareSendPayload('just text', [])
    expect(result.filePaths).toEqual([])
    expect(result.imgPaths).toEqual([])
  })

  it('replaces @-referenced token inline in txt', () => {
    const result = prepareSendPayload('see @data.csv', ['/tmp/data.csv'])
    expect(result.txt).toContain('[attached_file 1] /tmp/data.csv')
    expect(result.txt).not.toContain('@data.csv')
  })

  it('deduplicates when same file appears twice', () => {
    const result = prepareSendPayload('hello', ['/tmp/a.csv', '/tmp/a.csv'])
    expect(result.filePaths).toEqual(['/tmp/a.csv'])
    expect(result.txt).toContain('[attached_file 1] /tmp/a.csv')
  })

  it('assigns unique token numbers when @-ref is not the first file', () => {
    const result = prepareSendPayload(
      'see @data.csv',
      ['/tmp/extra.log', '/tmp/data.csv'],
    )
    const indices = [...result.txt.matchAll(/\[attached_file (\d+)\]/g)].map(m => m[1])
    expect(indices.length).toBe(2)
    expect(new Set(indices).size).toBe(indices.length)
    expect(result.txt).toContain('[attached_file 1] /tmp/data.csv')
    expect(result.txt).toContain('[attached_file 2] /tmp/extra.log')
    expect(result.filePaths).toEqual(['/tmp/data.csv', '/tmp/extra.log'])
  })
})

describe('addPendingFile canonical dedupe', () => {
  it('upgrades a matching legacy native entry to the canonical form', () => {
    // A restored draft can hold native `C:\…`; keeping it would strand the
    // remove-chip lookup, which keys on the canonical staged string.
    expect(addPendingFile(['C:\\repo\\a.ts'], 'C:/repo/a.ts')).toEqual(['C:/repo/a.ts'])
    expect(addPendingFile(['C:/repo/a.ts'], 'C:\\repo\\a.ts')).toEqual(['C:/repo/a.ts'])
  })

  it('returns the same array when the canonical entry is already staged', () => {
    const prev = ['C:/repo/a.ts']
    expect(addPendingFile(prev, 'C:/repo/a.ts')).toBe(prev)
  })

  it('replaces in place, preserving order', () => {
    expect(addPendingFile(['/tmp/x.ts', 'C:\\repo\\a.ts', '/tmp/y.ts'], 'C:/repo/a.ts'))
      .toEqual(['/tmp/x.ts', 'C:/repo/a.ts', '/tmp/y.ts'])
  })

  it('stores new entries in canonical forward-slash form', () => {
    expect(addPendingFile([], 'C:\\repo\\a.ts')).toEqual(['C:/repo/a.ts'])
  })

  it('appends distinct files and leaves POSIX paths untouched', () => {
    expect(addPendingFile(['/tmp/a.ts'], '/tmp/b.ts')).toEqual(['/tmp/a.ts', '/tmp/b.ts'])
    // `\` is a legal POSIX filename character, not a separator.
    expect(addPendingFile(['/tmp/weird\\name.txt'], '/tmp/weird\\name.txt')).toEqual(['/tmp/weird\\name.txt'])
  })
})

describe('hasExactRelMention exact-token mention detection', () => {
  it('sees the slash-form token (the tree-menu rendition)', () => {
    expect(hasExactRelMention('look at @src/a/b.ts here', 'src/a/b.ts')).toBe(true)
  })

  it('sees the backslash-form token (the native-Windows picker rendition)', () => {
    expect(hasExactRelMention('look at @src\\a\\b.ts here', 'src/a/b.ts')).toBe(true)
  })

  it('does NOT suffix-match: a shorter basename mention of a DIFFERENT file is not a hit', () => {
    // Regression: a suffix walk would let `@util.ts` (staged for src/a/util.ts)
    // report src/b/util.ts as "already mentioned", and the fallback chip-remove
    // derivation (buildRelMap, also a suffix walk) would then strip that same
    // `@util.ts` token when removing src/b/util.ts's chip -- deleting
    // src/a/util.ts's mention instead.
    expect(hasExactRelMention('look at @util.ts here', 'src/b/util.ts')).toBe(false)
  })

  it('does not match a different file or a mid-word fragment', () => {
    expect(hasExactRelMention('look at @src/a/c.ts here', 'src/a/b.ts')).toBe(false)
    // boundary check: @src/a/b.tsx is not @src/a/b.ts
    expect(hasExactRelMention('look at @src/a/b.tsx here', 'src/a/b.ts')).toBe(false)
    expect(hasExactRelMention('', 'src/a/b.ts')).toBe(false)
  })

  it('a POSIX rel containing a backslash matches only itself, not a slash rewrite', () => {
    // `\` is a legal POSIX filename character; the backslash-rendition check
    // must not be invented for a rel that already contains one.
    expect(hasExactRelMention('see @weird\\name.txt', 'weird\\name.txt')).toBe(true)
    expect(hasExactRelMention('see @weird/name.txt', 'weird\\name.txt')).toBe(false)
  })

  it('does not match a mention embedded mid-word', () => {
    // `foo@README.md` is incidental text (an email-like string, a filename
    // typo), not a mention -- without a left boundary this would false-
    // positive, causing the caller to skip inserting its own clean token
    // while the file still gets staged with no valid distinguishing @token.
    expect(hasExactRelMention('foo@README.md', 'README.md')).toBe(false)
    expect(hasExactRelMention('see @README.md now', 'README.md')).toBe(true)
  })
})
