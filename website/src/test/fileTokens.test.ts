import { describe, it, expect } from 'vitest'
import { addPendingFile, extendsConsumably, findUnreferencedAttachments, foldWinSep, isWindowsShapedPath, mentionBoundary, mentionBoundaryFor, mentionTokenRegex, normalizeWindowsPath, parseFiles, prepareSendPayload, buildFileLabels, resolveFileSegment, mdImageDest, mdImageDestToPath, restoreQueuedContent, restoreUnreferencedImages, serializeDirTokens } from '../utils/fileTokens'

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
  it('replaces a punctuated mention inline instead of appending a duplicate standalone marker (fork GPT review)', () => {
    // The reconciliation boundary keeps `@src/main.ts.` staged (ordinary
    // sentence-ending punctuation), so the send path must FIND that same
    // mention: with the old whitespace-only tokenRegex it classified the
    // file unreferenced and appended `[attached_file 1]` standalone while
    // the mention text sat unreplaced beside it.
    const result = prepareSendPayload('check @src/main.ts.', ['/repo/src/main.ts'])
    expect(result.txt).toBe('check [attached_file 1] /repo/src/main.ts\u200a.')
  })

  it('keeps every inline marker whitespace-terminated, so readers never glue the trailer to the path (fork Opus review)', () => {
    // The queue-edit pruner's span match, the text-only parseFiles fallback
    // (steer rows) and the renderer's no-index fallback all end a marker path
    // at whitespace. A glued `, please` made them read `/repo/a.txt,`.
    const { txt } = prepareSendPayload('check @a.txt, please', ['/repo/a.txt'])
    expect(txt).toBe('check [attached_file 1] /repo/a.txt\u200a, please')
    expect(parseFiles(txt)).toEqual(['/repo/a.txt'])
    // Rendered, the bubble reads exactly as typed.
    expect(resolveFileSegment(txt, ['/repo/a.txt']).display).toBe('check @a.txt, please')
    expect(resolveFileSegment(txt, []).display).toBe('check @a.txt, please')
    // A mention already followed by whitespace gets no separator.
    expect(prepareSendPayload('check @a.txt now', ['/repo/a.txt']).txt).toBe('check [attached_file 1] /repo/a.txt now')
  })

  it('keeps a hair space the user pasted beside a mention: the renderer removes only the generated separator (fork GPT review)', () => {
    const HS = '\u200a'
    for (const typed of [`check${HS}@a.txt now`, `check @a.txt${HS}, please`, `(${HS}@a.txt${HS})`]) {
      const { txt } = prepareSendPayload(typed, ['/repo/a.txt'])
      expect(parseFiles(txt)).toEqual(['/repo/a.txt'])
      expect(resolveFileSegment(txt, ['/repo/a.txt']).display).toBe(typed)
    }
  })

  it('never removes a space the user typed before punctuation (fork GPT review)', () => {
    // `@a.txt , please` (French typography, or just a typed space): the
    // serializer adds nothing, and the renderer must not eat the user's space.
    const { txt } = prepareSendPayload('check @a.txt , please', ['/repo/a.txt'])
    expect(txt).toBe('check [attached_file 1] /repo/a.txt , please')
    expect(resolveFileSegment(txt, ['/repo/a.txt']).display).toBe('check @a.txt , please')
    expect(resolveFileSegment(txt, []).display).toBe('check @a.txt , please')
    // Same with a quoted word after the mention (fork Opus review): eating
    // the space also broke the inline chip's boundary.
    const quoted = prepareSendPayload('check @a.ts "x"', ['/repo/a.ts']).txt
    expect(quoted).toBe('check [attached_file 1] /repo/a.ts "x"')
    expect(resolveFileSegment(quoted, ['/repo/a.ts']).display).toBe('check @a.ts "x"')
  })

  it('replaces a bracket-wrapped mention under the same shared boundary contract', () => {
    const result = prepareSendPayload('see (@src/main.ts) here', ['/repo/src/main.ts'])
    expect(result.txt).toBe('see (\u200a[attached_file 1] /repo/src/main.ts\u200a) here')
    // The separators exist for the wire readers; the bubble reads as typed.
    expect(resolveFileSegment(result.txt, ['/repo/src/main.ts']).display).toBe('see (@main.ts) here')
  })

  it('binds each mention to the file whose OWN alias it is when a sibling literally extends it (fork GPT review)', () => {
    // `report` and `report,` are both legal filenames. Without the shared
    // prefix-sibling rule, the g-flag replace of `report` also rewrote the
    // HEAD of `@report,`'s own mention, binding that text -- and its
    // attachment -- to the wrong file.
    const result = prepareSendPayload('see @report and @report, thanks', ['/r/report', '/r/report,'])
    expect(result.txt).toBe('see [attached_file 1] /r/report and [attached_file 2] /r/report, thanks')
  })

  it('a lone punctuated mention that is a sibling\'s OWN alias binds to that sibling only', () => {
    // `@report,` present, both files staged: the text belongs to `report,`;
    // `report` is unreferenced and gets its own standalone marker line.
    const result = prepareSendPayload('@report, ', ['/r/report', '/r/report,'])
    expect(result.txt.startsWith('[attached_file 1] /r/report, ')).toBe(true)
    expect(result.filePaths).toEqual(['/r/report,', '/r/report'])
    expect(result.txt.split('\n').some(l => l === '[attached_file 2] /r/report')).toBe(true)
  })

  it('a wrapped file-line mention keeps its boundary: `(@src/main.ts:42)` is referenced and replaced inline (fork GPT review)', () => {
    // The `:line` alternative used to require whitespace/end directly after
    // the digits, so a closing wrapper broke the boundary: the chip
    // unstaged and the send omitted the intended file.
    const result = prepareSendPayload('see (@src/main.ts:42) here', ['/repo/src/main.ts'])
    expect(result.txt).toBe('see (\u200a[attached_file 1] /repo/src/main.ts\u200a:42) here')
  })

  it('rendering applies the prefix-sibling rule too: an unmentioned prefix sibling keeps its attachment card (fork GPT review)', () => {
    // findUnreferencedAttachments used to probe each path ALONE, so `report`
    // had no sibling to force the strict boundary, matched `@report,` via
    // the punctuation boundary, was counted referenced, and its attachment
    // card was hidden. One rel map over ALL files gives the rule its
    // candidate set: only `report,` is referenced here.
    expect(findUnreferencedAttachments('see @report, thanks', ['/r/report', '/r/report,']))
      .toEqual(['/r/report'])
    // The genuinely-referenced sibling stays referenced.
    expect(findUnreferencedAttachments('see @report and @report, thanks', ['/r/report', '/r/report,']))
      .toEqual([])
  })

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

describe('isWindowsShapedPath: drive-letter or UNC prefix, either spelling', () => {
  it('recognizes a forward-slash UNC project (fork GPT review)', () => {
    expect(isWindowsShapedPath('//server/share/repo')).toBe(true)
    expect(isWindowsShapedPath('//server/share')).toBe(true)
    expect(isWindowsShapedPath('\\\\server\\share\\repo')).toBe(true)
    expect(isWindowsShapedPath('C:/repo')).toBe(true)
  })

  it('needs a host plus a separator, the same shape as the backslash UNC form', () => {
    expect(isWindowsShapedPath('//server')).toBe(false)
    expect(isWindowsShapedPath('///x/y')).toBe(false)
    expect(isWindowsShapedPath('/home/u/p')).toBe(false)
  })

  it('does not change the producer normalizer: a //-spelled path keeps its backslashes', () => {
    // Fails if the producer regex is ever widened instead: that would rewrite
    // a legal POSIX filename character inside the path.
    expect(normalizeWindowsPath('//server/share/a\\b.txt')).toBe('//server/share/a\\b.txt')
  })

  it('foldWinSep reads every separator spelling the same on Windows, and nothing on POSIX', () => {
    const win = foldWinSep(true)
    expect(win('@other\\src/main.ts')).toBe('@other/src/main.ts')
    expect(win('@other\\src\\main.ts')).toBe(win('@other/src/main.ts'))
    expect(win('a\\b').length).toBe(3) // 1:1, so indices carry over
    expect(foldWinSep(false)('weird\\name.txt')).toBe('weird\\name.txt')
  })
})

describe('replaceTokens: an EMPTY replacement drops the mention like the remove-chip strip (fork Opus review)', () => {
  it('a wrapped image mention leaves no stray pair behind', () => {
    const result = prepareSendPayload('see (@shot.png) here', ['/r/shot.png'])
    expect(result.txt).toBe('![image](/r/shot.png)\n\nsee  here')
    expect(result.txt).not.toContain('()')
    expect(result.displayTxt).not.toContain('()')
  })

  it('an image mention with a :line suffix takes the suffix with it', () => {
    const result = prepareSendPayload('see @shot.png:3 here', ['/r/shot.png'])
    expect(result.txt).toBe('![image](/r/shot.png)\n\nsee  here')
  })

  it('an UNPAIRED wrapper is left in place like ordinary punctuation', () => {
    const result = prepareSendPayload('(@shot.png here', ['/r/shot.png'])
    expect(result.txt).toBe('![image](/r/shot.png)\n\n( here')
  })
})

describe('mentionBoundaryFor: the one prefix-sibling rule', () => {
  it('extendsConsumably is the rule behind mentionBoundaryFor, both directions (fork GPT review)', () => {
    // Truth table: consumable extensions are hazards; inert ones are not.
    expect(extendsConsumably('@report', '@report,')).toBe(true)
    expect(extendsConsumably('@report', '@report:42')).toBe(true)
    expect(extendsConsumably('@report', '@report:42,')).toBe(true)
    expect(extendsConsumably('@report', '@report:42.md')).toBe(false)
    expect(extendsConsumably('@report', '@report.bak')).toBe(false)
    expect(extendsConsumably('@report', '@repo')).toBe(false)  // not an extension
    expect(extendsConsumably('@report', '@report')).toBe(false) // equal length
    // Parity: mentionBoundaryFor forces strict exactly when the predicate fires.
    for (const ext of [',', ':42', ':42,', ':42.md', '.bak', 'x']) {
      const strict = mentionBoundaryFor('@report', new Set([`@report${ext}`])) !== mentionBoundary
      expect(strict, `ext=${ext}`).toBe(extendsConsumably('@report', `@report${ext}`))
    }
  })

  it('a candidate strictly extended by a sibling gets the strict boundary; the sibling itself stays permissive', () => {
    // Behavioral pins (the strict source itself is module-private): the
    // extended candidate's boundary refuses trailing punctuation; the
    // sibling and the sibling-free case keep the permissive boundary.
    expect(mentionBoundaryFor('@report', new Set(['@report,']))).not.toBe(mentionBoundary)
    expect(new RegExp(`^${mentionBoundaryFor('@report', new Set(['@report,']))}`).test(', ')).toBe(false)
    expect(mentionBoundaryFor('@report,', new Set(['@report']))).toBe(mentionBoundary)
    expect(mentionBoundaryFor('@report')).toBe(mentionBoundary)
    // A `:line`-shaped extension is also hazardous.
    expect(mentionBoundaryFor('@report', new Set(['@report:1']))).not.toBe(mentionBoundary)
  })

  it('a sibling extending with boundary-inert characters does NOT force strict: `.env` vs `.env.local` (fork Opus review)', () => {
    // `.local`, `x` of `.tsx`, `.dev` -- extensions the permissive boundary
    // could never consume protect nothing; forcing strict there made an
    // ordinary `@.env,` read as unmentioned, silently unstaging the file.
    expect(mentionBoundaryFor('.env', new Set(['.env.local']))).toBe(mentionBoundary)
    expect(mentionBoundaryFor('main.ts', new Set(['main.tsx']))).toBe(mentionBoundary)
  })

  it('a `:digits` extension the boundary cannot finish consuming does NOT force strict: `report` vs `report:42.md` (fork GPT review)', () => {
    // The permissive boundary consumes `:digits` only when whitespace, end,
    // or a punctuation run ends it -- `@report:42.md` can never be read as a
    // `report` mention, so forcing strict protected nothing and a plain
    // `@report,` silently unstaged the file. A consumable `:digits` tail
    // (`report:42`, `report:42,`) keeps the protection.
    expect(mentionBoundaryFor('@report', new Set(['@report:42.md']))).toBe(mentionBoundary)
    expect(mentionBoundaryFor('@report', new Set(['@report:4x']))).toBe(mentionBoundary)
    expect(mentionBoundaryFor('@report', new Set(['@report:42']))).not.toBe(mentionBoundary)
    expect(mentionBoundaryFor('@report', new Set(['@report:42,']))).not.toBe(mentionBoundary)
  })

  it('both colon-sibling files keep inline markers through a punctuated sentence at send time (fork GPT review)', () => {
    const result = prepareSendPayload('see @report, and @report:42.md thanks', ['/r/report', '/r/report:42.md'])
    expect(result.txt).toBe('see [attached_file 1] /r/report\u200a, and [attached_file 2] /r/report:42.md thanks')
  })

  it('both dotfile siblings stay staged through a punctuated sentence at send time (fork Opus review)', () => {
    const result = prepareSendPayload('Compare @.env, @.env.local and tell me', ['/r/.env', '/r/.env.local'])
    expect(result.txt).toBe('Compare [attached_file 1] /r/.env\u200a, [attached_file 2] /r/.env.local and tell me')
  })

  it('mentionTokenRegex applies the rule: the shorter alias never claims the sibling\'s own mention', () => {
    expect(mentionTokenRegex('report', '', new Set(['report,'])).test('see @report, here')).toBe(false)
    expect(mentionTokenRegex('report').test('see @report, here')).toBe(true)
  })
})

describe('restoreQueuedContent (cancel-queued parser fallback)', () => {
  // The primary cancel restore is ChatPage's send-side stash (lossless for
  // every shape). This parser covers reload/other-tab/edited entries, and its
  // contract is strict: claim ONLY provably-lossless shapes, everything else
  // stays verbatim — never worse than the base branch's verbatim restore.

  it('returns plain text unchanged with no files', () => {
    const r = restoreQueuedContent('run the tests')
    expect(r.text).toBe('run the tests')
    expect(r.files).toEqual([])
  })

  it('strips a standalone attachment marker and re-stages its path', () => {
    const { txt } = prepareSendPayload('summarize this report', ['/tmp/q3/report.docx'])
    const r = restoreQueuedContent(txt)
    expect(r.text).toBe('summarize this report')
    expect(r.text).not.toContain('[attached_file')
    expect(r.files).toEqual(['/tmp/q3/report.docx'])
  })

  it('strips several standalone markers and re-stages each path', () => {
    const { txt } = prepareSendPayload('compare all of these', ['/tmp/a.csv', '/tmp/b.log'])
    const r = restoreQueuedContent(txt)
    expect(r.text).toBe('compare all of these')
    expect(new Set(r.files)).toEqual(new Set(['/tmp/a.csv', '/tmp/b.log']))
  })

  it('leaves an embedded (@-mentioned) marker verbatim — its path boundary is not provable', () => {
    // `[attached_file 1] /a/b c` inline in prose: a whitespace-bounded capture
    // truncates a spaced path, staging a nonexistent file and re-sending the
    // wrong one. The stash handles this shape; the parser must not guess.
    const { txt } = prepareSendPayload('see @data.csv for details', ['/tmp/data.csv'])
    const r = restoreQueuedContent(txt)
    expect(r.text).toBe(txt)
    expect(r.files).toEqual([])
  })

  it('restores a producer-form image line as a staged image path', () => {
    const { txt } = prepareSendPayload('what is in this picture', ['/tmp/photo.png'])
    const r = restoreQueuedContent(txt)
    expect(r.text).toBe('what is in this picture')
    expect(r.files).toEqual(['/tmp/photo.png'])
  })

  it('restores an image path containing spaces from its wrapped destination', () => {
    // mdImageDest's <...> wrap gives the destination an exact boundary, so an
    // image is the one spaced-path shape the parser CAN claim losslessly.
    const { txt } = prepareSendPayload('look', ['/tmp/My Shots/pic 1.png'])
    const r = restoreQueuedContent(txt)
    expect(r.text).toBe('look')
    expect(r.files).toEqual(['/tmp/My Shots/pic 1.png'])
  })

  it('leaves an inline image reference the user typed themselves alone', () => {
    const r = restoreQueuedContent('the logo ![image](/tmp/logo.png) sits inline in this sentence')
    expect(r.text).toContain('![image](/tmp/logo.png)')
    expect(r.files).toEqual([])
  })

  it('leaves an own-line relative image from pasted markdown verbatim', () => {
    const pasted = 'review this README excerpt:\n\n## Logo\n\n![image](docs/logo.png)\n\nand the table below'
    const r = restoreQueuedContent(pasted)
    expect(r.text).toBe(pasted)
    expect(r.files).toEqual([])
  })

  it('leaves a relative-path file marker from foreign text verbatim', () => {
    const pasted = 'the transcript said:\n[attached_file 1] docs/spec.md\nwhich was odd'
    const r = restoreQueuedContent(pasted)
    expect(r.text).toBe(pasted)
    expect(r.files).toEqual([])
  })

  it('leaves an own-line marker with a spaced remainder verbatim — the ambiguous shape', () => {
    // Equally a spaced bare-upload path and a line-start mention followed by
    // prose; any claim corrupts one reading, so neither is made.
    const { txt } = prepareSendPayload('summarize this', ['/Users/me/Desktop/My Report.pdf'])
    const r = restoreQueuedContent(txt)
    expect(r.text).toBe(txt)
    expect(r.files).toEqual([])
  })

  it('leaves a line-start mention followed by prose verbatim rather than eating the prose', () => {
    const { txt } = prepareSendPayload('@data.csv is broken', ['/tmp/data.csv'])
    expect(txt).toBe('[attached_file 1] /tmp/data.csv is broken')
    const r = restoreQueuedContent(txt)
    expect(r.text).toBe(txt)
    expect(r.files).toEqual([])
  })

  it('survives malformed marker indices without crashing or consuming them', () => {
    // N=999999999 can never renumber identically on re-send, so the
    // round-trip arbiter keeps the whole content verbatim: restoring it
    // would silently rewrite the marker's index.
    const weird = 'a [attached_file 0] /tmp/x.txt b\n[attached_file 999999999] /tmp/y.txt'
    const r = restoreQueuedContent(weird)
    expect(r.text).toBe(weird)
    expect(r.files).toEqual([])
  })

  it('claims each marker index once — a duplicate N stays verbatim', () => {
    // Claiming one of the two lines cannot round-trip (the survivor would
    // renumber), so the arbiter keeps both verbatim.
    const dup = '[attached_file 1] /tmp/a.txt\n[attached_file 1] /tmp/b.txt'
    const r = restoreQueuedContent(dup)
    expect(r.text).toBe(dup)
    expect(r.files).toEqual([])
  })

  it('leaves a mid-text own-line mention marker verbatim — restoring would reorder it', () => {
    // '@data.csv\nthen summarize': the mention serializes INLINE at the line
    // start; claiming it would re-stage the file as an APPENDED token on
    // re-send, moving the marker after the instruction. The round-trip
    // arbiter rejects the claim.
    const { txt } = prepareSendPayload('@data.csv\nthen summarize', ['/tmp/data.csv'])
    expect(txt).toBe('[attached_file 1] /tmp/data.csv\nthen summarize')
    const r = restoreQueuedContent(txt)
    expect(r.text).toBe(txt)
    expect(r.files).toEqual([])
  })

  it('leaves dir markers verbatim — they sit inline where no boundary is provable', () => {
    const project = '/home/me/proj'
    const { llm } = serializeDirTokens('review @a/src/ carefully', project)
    const r = restoreQueuedContent(llm)
    expect(r.text).toBe(llm)
    expect(r.files).toEqual([])
  })

  it('re-sending the restored state does not double markers', () => {
    const original = prepareSendPayload('check these', ['/tmp/data.csv', '/tmp/extra.log'])
    const r = restoreQueuedContent(original.txt)
    const resent = prepareSendPayload(r.text, r.files)
    expect(resent.txt).toBe(original.txt)
    expect(resent.filePaths).toEqual(original.filePaths)
  })

  it('restores a mixed image + document + text payload completely', () => {
    const { txt } = prepareSendPayload('compare these', ['/tmp/shot.png', '/tmp/data.csv'])
    const r = restoreQueuedContent(txt)
    expect(r.text).toBe('compare these')
    expect(new Set(r.files)).toEqual(new Set(['/tmp/shot.png', '/tmp/data.csv']))
  })

  it('preserves interior whitespace and strips only the blank lines removed markers left', () => {
    const { txt } = prepareSendPayload('line one\n\nline  two', ['/tmp/photo.png'])
    const r = restoreQueuedContent(txt)
    expect(r.text).toBe('line one\n\nline  two')
  })

  it('leaves an own-line ABSOLUTE image outside the leading block verbatim', () => {
    // The producer only ever emits image lines as the leading block; an
    // own-line image later in the content is the user's own markdown even
    // when its path is absolute. Claiming it would strip user content and
    // reposition it as a chip on re-send.
    const typed = 'compare with the golden file:\n\n![image](/tmp/golden/expected.png)\n\ndoes it match?'
    const r = restoreQueuedContent(typed)
    expect(r.text).toBe(typed)
    expect(r.files).toEqual([])
  })

  it('preserves a body that begins with a newline (expanded paste) byte-exact', () => {
    // The image block match consumes exactly the producer's `\n\n` separator,
    // so a paste whose expansion starts with a blank/indented line keeps it.
    const { txt } = prepareSendPayload('\n  indented first line\nsecond', ['/tmp/pic.png'])
    const r = restoreQueuedContent(txt)
    expect(r.text).toBe('\n  indented first line\nsecond')
    expect(r.files).toEqual(['/tmp/pic.png'])
  })

  it('keeps leading and trailing blank lines of markerless content untouched', () => {
    const padded = '\n\nhello\n\n'
    const r = restoreQueuedContent(padded)
    expect(r.text).toBe(padded)
    expect(r.files).toEqual([])
  })

  it('claims the leading image block all-or-nothing — one foreign line keeps the whole block verbatim', () => {
    // The producer never emits a relative path, so a block containing one is
    // foreign text; claiming the valid line alone would tear pasted markdown.
    const pasted = '![image](/tmp/real.png)\n![image](docs/logo.png)\n\nfrom the README'
    const r = restoreQueuedContent(pasted)
    expect(r.text).toBe(pasted)
    expect(r.files).toEqual([])
  })

  it('restores a two-image leading block completely', () => {
    const { txt } = prepareSendPayload('diff these', ['/tmp/a.png', '/tmp/b shots/b 2.png'])
    const r = restoreQueuedContent(txt)
    expect(r.text).toBe('diff these')
    expect(r.files).toEqual(['/tmp/a.png', '/tmp/b shots/b 2.png'])
  })
})

describe('restoreQueuedContent with the entry\'s own attachment list', () => {
  // The server echoes each queue entry's ORDERED non-image list (the same
  // `meta.files` a user row carries) on the slot-detail queue, the queue_push
  // frame and the cancel reply. Marker N names files[N-1], so the parser can
  // claim an own-line marker by EXACT text — the one thing the wire text
  // alone could never prove for a path with a space.

  const spaced = '/Users/me/Desktop/My Report.pdf'

  it('claims a spaced bare-upload path whole when the list names it', () => {
    const { txt, filePaths } = prepareSendPayload('summarize this', [spaced])
    expect(txt).toBe(`summarize this\n[attached_file 1] ${spaced}`)
    const r = restoreQueuedContent(txt, filePaths)
    expect(r.text).toBe('summarize this')
    expect(r.files).toEqual([spaced])
  })

  it('the same content without a list stays verbatim — the list is what proves the boundary', () => {
    const { txt } = prepareSendPayload('summarize this', [spaced])
    const r = restoreQueuedContent(txt)
    expect(r.text).toBe(txt)
    expect(r.files).toEqual([])
  })

  it('claims several spaced paths in list order and re-stages them all', () => {
    const paths = ['/tmp/q3 report/final draft.docx', '/tmp/q4 report/final draft.docx']
    const { txt, filePaths } = prepareSendPayload('compare these two', paths)
    const r = restoreQueuedContent(txt, filePaths)
    expect(r.text).toBe('compare these two')
    expect(r.files).toEqual(paths)
  })

  it('restores a leading image block together with a listed spaced document', () => {
    const { txt, filePaths } = prepareSendPayload('caption', ['/tmp/pic.png', spaced])
    const r = restoreQueuedContent(txt, filePaths)
    expect(r.text).toBe('caption')
    expect(r.files).toEqual(['/tmp/pic.png', spaced])
  })

  it('leaves marker-shaped paste text verbatim when the list does not name it', () => {
    // A pasted transcript can contain producer-looking lines. The list is the
    // entry's own; a marker it does not account for is foreign text.
    const pasted = 'from the log:\n[attached_file 1] /var/log/app 2026.log\nis that right'
    const r = restoreQueuedContent(pasted, ['/tmp/other.txt'])
    expect(r.text).toBe(pasted)
    expect(r.files).toEqual([])
  })

  it('leaves marker-shaped paste text verbatim with no list at all', () => {
    const pasted = 'from the log:\n[attached_file 1] /var/log/app 2026.log\nis that right'
    const r = restoreQueuedContent(pasted)
    expect(r.text).toBe(pasted)
    expect(r.files).toEqual([])
  })

  it('still leaves an inline mention verbatim — its @rel spelling is not on the list', () => {
    const { txt, filePaths } = prepareSendPayload('see @My Report.pdf for details', [spaced])
    const r = restoreQueuedContent(txt, filePaths)
    expect(r.text).toBe(txt)
    expect(r.files).toEqual([])
  })

  it('a list whose path disagrees with the marker text claims nothing', () => {
    // The arbiter holds: an exact-text claim that fails to match leaves the
    // content whole rather than staging a path the text never carried.
    const txt = 'summarize this\n[attached_file 1] /tmp/My Report.pdf'
    const r = restoreQueuedContent(txt, ['/tmp/My Other Report.pdf'])
    expect(r.text).toBe(txt)
    expect(r.files).toEqual([])
  })

  it('escapes regex metacharacters in a listed path', () => {
    const odd = '/tmp/report (final) [v2].pdf'
    const { txt, filePaths } = prepareSendPayload('read', [odd])
    const r = restoreQueuedContent(txt, filePaths)
    expect(r.text).toBe('read')
    expect(r.files).toEqual([odd])
  })
})

describe('restoreUnreferencedImages (legacy pane rows: image only on meta.files)', () => {
  it('prepends a producer-form image line for each image the text never names', () => {
    expect(restoreUnreferencedImages('look', { files: ['/tmp/a.png', '/tmp/b.jpg'] }))
      .toBe('![image](/tmp/a.png)\n![image](/tmp/b.jpg)\n\nlook')
  })

  it('leaves a row alone when the markdown already names the image (no doubling)', () => {
    const content = '![image](/tmp/a.png)\n\nlook'
    expect(restoreUnreferencedImages(content, { files: ['/tmp/a.png'] })).toBe(content)
  })

  it('recognises the wrapped destination mdImageDest emits for a spaced path', () => {
    const p = '/tmp/b shots/b 2.png'
    const content = `![image](${mdImageDest(p)})\n\nlook`
    expect(restoreUnreferencedImages(content, { files: [p] })).toBe(content)
  })

  it('ignores non-image files (those become cards, never images)', () => {
    expect(restoreUnreferencedImages('read', { files: ['/tmp/report.pdf'] })).toBe('read')
  })

  it('a caption that merely mentions the path in prose does not suppress the restore', () => {
    // Only a markdown DESTINATION `](dest)` counts as the image being named.
    expect(restoreUnreferencedImages('compare with /tmp/a.png please', { files: ['/tmp/a.png'] }))
      .toBe('![image](/tmp/a.png)\n\ncompare with /tmp/a.png please')
  })

  it('a link to the image (not just an image embed) counts as named', () => {
    const content = 'see [the frame](/tmp/a.png)'
    expect(restoreUnreferencedImages(content, { files: ['/tmp/a.png'] })).toBe(content)
  })

  it('is the identity without meta, with an empty list, or with a malformed list', () => {
    expect(restoreUnreferencedImages('plain')).toBe('plain')
    expect(restoreUnreferencedImages('plain', { files: [] })).toBe('plain')
    expect(restoreUnreferencedImages('plain', { files: 'nope' })).toBe('plain')
    expect(restoreUnreferencedImages('plain', { files: [42, null] })).toBe('plain')
  })

  it('a healed row and a freshly sent one share one content shape', () => {
    // What the pane now sends for the same upload + caption.
    const { displayTxt } = prepareSendPayload('look', ['/tmp/a.png'])
    expect(restoreUnreferencedImages('look', { files: ['/tmp/a.png'] })).toBe(displayTxt)
  })

  it('an image-only legacy row (empty caption) yields just the image line', () => {
    expect(restoreUnreferencedImages('', { files: ['/tmp/a.png'] })).toBe('![image](/tmp/a.png)')
  })
})
