import { describe, it, expect } from 'vitest'
import { fileReadUrl, fileDownloadUrl } from '../utils/fileReadUrl'

// Issue #2493: a dashboard file-read was captured with the leading slash
// stripped from an absolute path (`path=home/<user>/…&resolve=1`), which the
// backend correctly 400s. fileReadUrl itself must preserve the path verbatim
// and only mark genuinely relative paths for server-side resolution — these
// tests lock that contract so a future "normalization" can't reintroduce the
// mangled shape at the URL-builder layer.
describe('fileReadUrl', () => {
  it('preserves an absolute path verbatim with no resolve=1', () => {
    const url = fileReadUrl('/home/user/project/notes.md')
    expect(url).toBe('/api/file-read?path=' + encodeURIComponent('/home/user/project/notes.md'))
    expect(url).not.toContain('resolve=1')
  })

  it('preserves a home-relative path verbatim with no resolve=1', () => {
    const url = fileReadUrl('~/project/notes.md')
    expect(url).toBe('/api/file-read?path=' + encodeURIComponent('~/project/notes.md'))
    expect(url).not.toContain('resolve=1')
  })

  it('appends resolve=1 for a relative path', () => {
    const url = fileReadUrl('src/main.py')
    expect(url).toBe('/api/file-read?path=' + encodeURIComponent('src/main.py') + '&resolve=1')
  })

  it('a rootless absolute-looking path is treated as relative — callers must not strip the slash', () => {
    // This is the mangled shape from issue #2493. fileReadUrl cannot know the
    // caller meant `/home/user/x`; it faithfully marks it relative. The fix
    // for #2493 therefore lives at the caller (DiffBlock header extraction),
    // not here — this test documents WHY the builder must stay verbatim.
    const url = fileReadUrl('home/user/project/notes.md')
    expect(url).toContain('resolve=1')
    expect(url).toContain(encodeURIComponent('home/user/project/notes.md'))
  })
})

describe('fileDownloadUrl', () => {
  it('mirrors the same verbatim-path and resolve semantics', () => {
    expect(fileDownloadUrl('/tmp/report.pdf')).toBe('/api/file-download?path=' + encodeURIComponent('/tmp/report.pdf'))
    expect(fileDownloadUrl('/tmp/report.pdf')).not.toContain('resolve=1')
    expect(fileDownloadUrl('docs/report.pdf')).toContain('&resolve=1')
  })
})
