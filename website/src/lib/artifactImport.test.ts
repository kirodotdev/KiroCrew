import {
  IMPORT_ACCEPT,
  IMPORTABLE_EXT_KINDS,
  MAX_IMPORT_BYTES,
  decodeTextStrict,
  extensionOf,
  kindForFilename,
  planFileImport,
  wasContentRedacted,
} from './artifactImport'

/** Build a File without depending on a real filesystem. */
function makeFile(name: string, bytes: Uint8Array | string): File {
  const body = typeof bytes === 'string' ? new TextEncoder().encode(bytes) : bytes
  // Report a size even in environments where Blob does not compute one.
  const file = new File([body as BlobPart], name)
  if (file.size !== body.byteLength) {
    Object.defineProperty(file, 'size', { value: body.byteLength })
  }
  return file
}

/** A File that claims a size without allocating the bytes. */
function makeOversizeFile(name: string, size: number): File {
  const file = new File(['x'], name)
  Object.defineProperty(file, 'size', { value: size })
  return file
}

describe('extensionOf', () => {
  it('lowercases the extension', () => {
    expect(extensionOf('NOTES.MD')).toBe('.md')
  })
  it('uses only the final extension', () => {
    expect(extensionOf('archive.tar.gz')).toBe('.gz')
  })
  it('ignores directories in the path', () => {
    expect(extensionOf('a.json/b.txt')).toBe('.txt')
  })
  it('returns empty for a name with no extension', () => {
    expect(extensionOf('README')).toBe('')
  })
  it('treats a dotfile as having no extension', () => {
    // A bare `.md` is a dotfile, not a markdown document — guessing markdown
    // here would let `.gitignore`-style names in through the same branch.
    expect(extensionOf('.md')).toBe('')
    expect(extensionOf('.gitignore')).toBe('')
  })
})

describe('kindForFilename', () => {
  it('maps every importable extension to its kind', () => {
    for (const [ext, kind] of Object.entries(IMPORTABLE_EXT_KINDS)) {
      expect(kindForFilename(`doc${ext}`)).toBe(kind)
    }
  })
  it('rejects a type with no renderer', () => {
    expect(kindForFilename('photo.png')).toBeNull()
    expect(kindForFilename('sheet.csv')).toBeNull()
    expect(kindForFilename('script.py')).toBeNull()
    expect(kindForFilename('paper.pdf')).toBeNull()
  })
})

describe('IMPORT_ACCEPT', () => {
  it('lists every importable extension', () => {
    expect(IMPORT_ACCEPT.split(',').sort()).toEqual(
      Object.keys(IMPORTABLE_EXT_KINDS).sort(),
    )
  })
})

describe('decodeTextStrict', () => {
  it('decodes UTF-8 text', () => {
    const bytes = new TextEncoder().encode('# Héllo — ünïcode 中文')
    expect(decodeTextStrict(bytes.buffer as ArrayBuffer)).toBe('# Héllo — ünïcode 中文')
  })
  it('strips a UTF-8 BOM', () => {
    const bytes = new Uint8Array([0xef, 0xbb, 0xbf, 0x68, 0x69])
    expect(decodeTextStrict(bytes.buffer)).toBe('hi')
  })
  it('rejects invalid UTF-8', () => {
    // 0xC3 starts a 2-byte sequence; 0x28 is not a valid continuation byte.
    expect(decodeTextStrict(new Uint8Array([0xc3, 0x28]).buffer)).toBeNull()
  })
  it('rejects valid UTF-8 containing NUL', () => {
    // A binary blob can decode cleanly yet still be unrenderable.
    expect(decodeTextStrict(new Uint8Array([0x68, 0x00, 0x69]).buffer)).toBeNull()
  })
})

describe('planFileImport', () => {
  it('accepts a markdown file and stamps the markdown kind', async () => {
    const result = await planFileImport(makeFile('notes.md', '# Title\n'))
    expect(result).toEqual({
      ok: true,
      plan: { name: 'notes.md', kind: 'markdown', content: '# Title\n' },
    })
  })

  it('stamps html for an .html file rather than sniffing it as a widget', async () => {
    // The backend infers `widget` from HTML-ish inline content when no kind is
    // supplied, so the explicit kind is what keeps an imported page an `html`
    // artifact.
    const result = await planFileImport(makeFile('page.html', '<div>hi</div>'))
    expect(result.ok && result.plan.kind).toBe('html')
  })

  it('stamps json for a .json file', async () => {
    const result = await planFileImport(makeFile('data.json', '{"a":1}'))
    expect(result.ok && result.plan.kind).toBe('json')
  })

  it('refuses an unsupported type before reading the file', async () => {
    // arrayBuffer() would throw if it were called — proves the cheap
    // metadata check short-circuits ahead of the read.
    const file = makeFile('photo.png', 'irrelevant')
    Object.defineProperty(file, 'arrayBuffer', {
      value: () => Promise.reject(new Error('should not read')),
    })
    await expect(planFileImport(file)).resolves.toEqual({
      ok: false,
      reason: 'unsupported-type',
    })
  })

  it('refuses a file over the content cap without reading it', async () => {
    const file = makeOversizeFile('big.txt', MAX_IMPORT_BYTES + 1)
    Object.defineProperty(file, 'arrayBuffer', {
      value: () => Promise.reject(new Error('should not read')),
    })
    await expect(planFileImport(file)).resolves.toEqual({
      ok: false,
      reason: 'too-large',
    })
  })

  it('accepts a file exactly at the cap', async () => {
    const file = makeFile('at-limit.txt', 'x')
    Object.defineProperty(file, 'size', { value: MAX_IMPORT_BYTES })
    const result = await planFileImport(file)
    expect(result.ok).toBe(true)
  })

  it('refuses an empty file', async () => {
    await expect(planFileImport(makeFile('blank.txt', ''))).resolves.toEqual({
      ok: false,
      reason: 'empty',
    })
  })

  it('refuses a binary file wearing a text extension', async () => {
    // PNG magic bytes renamed to .txt — the extension is a claim, the bytes
    // are the evidence.
    const png = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])
    await expect(planFileImport(makeFile('sneaky.txt', png))).resolves.toEqual({
      ok: false,
      reason: 'not-text',
    })
  })

  it('reports a read failure instead of rejecting', async () => {
    // A picked file can go unreadable before it is read (ejected volume,
    // dropped network share, file deleted or replaced). The rejection must
    // become a result the caller can show, not an unhandled rejection that
    // aborts the import silently.
    const file = makeFile('gone.md', '# was here')
    Object.defineProperty(file, 'arrayBuffer', {
      value: () => Promise.reject(new DOMException('NotReadableError')),
    })
    await expect(planFileImport(file)).resolves.toEqual({
      ok: false,
      reason: 'unreadable',
    })
  })
})

describe('wasContentRedacted', () => {
  it('accepts a verbatim round-trip', () => {
    expect(wasContentRedacted('# notes\nplain text', '# notes\nplain text')).toBe(false)
  })

  it('detects content the store rewrote', () => {
    // What the API does to credential material on read: the value is replaced,
    // so the returned text no longer matches what was posted.
    expect(
      wasContentRedacted('aws_secret_access_key: AKIAIOSFODNN7EXAMPLE', 'aws_secret_access_key: [REDACTED]'),
    ).toBe(true)
  })

  it('treats absent content as a round-trip, not a mismatch', () => {
    // A response without a `content` field is no evidence of redaction —
    // refusing the import on that basis would reject every good file if the
    // API ever stopped echoing content back.
    expect(wasContentRedacted('anything', undefined)).toBe(false)
    expect(wasContentRedacted('anything', null)).toBe(false)
  })

  it('detects a truncated round-trip', () => {
    expect(wasContentRedacted('full text here', 'full text')).toBe(true)
  })

  it('does not flag empty-string equality', () => {
    expect(wasContentRedacted('', '')).toBe(false)
  })
})
