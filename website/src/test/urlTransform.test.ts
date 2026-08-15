import { describe, it, expect } from 'vitest'
import { urlTransform, decodeLocalPath, WINDOWS_ABS_PATH_RE } from '../utils/urlTransform'

describe('urlTransform', () => {
  it('allows vscode remote SSH URL', () => {
    const url = 'vscode://vscode-remote/ssh-remote+dev-host.example.com/home/user/workspace/KiroCrew'
    expect(urlTransform(url)).toBe(url)
  })

  it('allows vscode file URL', () => {
    const url = 'vscode://file/home/user/project'
    expect(urlTransform(url)).toBe(url)
  })

  it('allows vscode-insiders:// URL', () => {
    const url = 'vscode-insiders://vscode-remote/ssh-remote+host/path'
    expect(urlTransform(url)).toBe(url)
  })

  it('preserves vscode URL with query params', () => {
    const url = 'vscode://vscode-remote/ssh-remote+host/path?windowId=1'
    expect(urlTransform(url)).toBe(url)
  })

  it('rejects bare vscode://', () => {
    expect(urlTransform('vscode://')).toBe('')
  })

  it('rejects bare vscode-insiders://', () => {
    expect(urlTransform('vscode-insiders://')).toBe('')
  })

  it('falls back to default for malformed URL', () => {
    expect(urlTransform('vscode://[invalid')).toBe('')
  })

  it('passes http URLs through default sanitizer', () => {
    expect(urlTransform('https://example.com')).toBe('https://example.com')
  })

  it('strips javascript: URLs', () => {
    expect(urlTransform('javascript:alert(1)')).toBe('')
  })

  // Windows local-image paths (issue #3497): defaultUrlTransform parses the
  // drive letter as an unknown `c:` scheme and empties the URL, so a pasted
  // image on Windows rendered as NOTHING (ImgWithFallback returns null on an
  // empty src). The pass-through is scoped to image `src` only.
  describe('windows absolute paths', () => {
    it('passes a drive path through for image src', () => {
      const url = 'C:/Users/me/.kiro/crew/uploads/shot.png'
      expect(urlTransform(url, 'src')).toBe(url)
    })

    it('passes a backslash drive path through for image src (history replay)', () => {
      const url = 'C:\\Users\\me\\uploads\\shot.png'
      expect(urlTransform(url, 'src')).toBe(url)
    })

    it('still strips a drive path on href (links keep the strict default)', () => {
      expect(urlTransform('C:/Users/me/doc.html', 'href')).toBe('')
    })

    it('still strips a drive path when no key is given (md-notebook href path)', () => {
      expect(urlTransform('C:/Users/me/doc.html')).toBe('')
    })

    it('still strips javascript: even on src', () => {
      expect(urlTransform('javascript:alert(1)', 'src')).toBe('')
    })

    it('does not treat a backslash UNC path as a pass-through drive path', () => {
      // A UNC src names a HOST; the pass-through must never admit it. The
      // string still survives defaultUrlTransform (scheme-less = relative
      // URL), but ImgWithFallback classifies it non-local, so it never
      // reaches /api/file-raw — pinned by the render-level test.
      const url = '\\\\fileserver\\home\\me\\uploads\\shot.png'
      expect(WINDOWS_ABS_PATH_RE.test(url)).toBe(false)
    })

    it('does not treat a multi-letter scheme as a drive path', () => {
      expect(urlTransform('js://payload', 'src')).toBe('')
    })

    it('does not treat a single letter without separator as a drive path', () => {
      expect(urlTransform('c:foo', 'src')).toBe('')
    })
  })

  // decodeLocalPath: the inverse of mdImageDest's %-escape + micromark's
  // destination encoding, with fail-safe rails (issue #3497 round 1 findings).
  describe('decodeLocalPath', () => {
    it('decodes micromark-encoded spaces', () => {
      expect(decodeLocalPath('C:/Users/John%20Doe/shot.png')).toBe('C:/Users/John Doe/shot.png')
    })

    it('restores a producer-escaped literal %', () => {
      expect(decodeLocalPath('/tmp/photo%2520copy.png')).toBe('/tmp/photo%20copy.png')
    })

    it('keeps the raw form on malformed sequences', () => {
      expect(decodeLocalPath('/tmp/bad%zz.png')).toBe('/tmp/bad%zz.png')
    })

    it('keeps the raw form when decoding would produce control characters', () => {
      // `%00` would embed a NUL — os.path.realpath on the backend raises on
      // NUL, so the decode is refused and the raw (harmless) form is kept.
      expect(decodeLocalPath('/tmp/x%00.png')).toBe('/tmp/x%00.png')
      expect(decodeLocalPath('/tmp/x%0a.png')).toBe('/tmp/x%0a.png')
    })
  })
})
