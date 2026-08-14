import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'
import { prepareSendPayload } from '../utils/fileTokens'

/**
 * REGRESSION GUARD — issue #3497: a pasted/dropped image on Windows must
 * render an inline preview in the sender's chat bubble.
 *
 * The upload endpoint returns the server-native absolute path. On Windows
 * that is a drive-letter path, which broke in three stacked ways:
 *
 *  1. `![image](C:\Users\me\.kiro\…)` — CommonMark treats `\` before ASCII
 *     punctuation as an escape, so the parsed destination is a mangled path.
 *  2. react-markdown's defaultUrlTransform parses `C:/…` as an unknown `c:`
 *     scheme and returns '' — ImgWithFallback then renders NOTHING (null),
 *     matching the report: no thumbnail, no broken-image placeholder.
 *  3. ImgWithFallback's isLocal check only recognized `/`, `~`, `.` prefixes,
 *     so a surviving drive path would never route to /api/file-raw.
 *
 * These tests pin the full chain: the send payload emits a markdown-safe
 * destination, and the renderer maps it to the file-raw endpoint.
 */

function srcOf(container: HTMLElement): string {
  const img = container.querySelector('img')
  if (!img) throw new Error('no <img> rendered')
  return img.getAttribute('src') || ''
}

describe('windows image paths render through file-raw (issue #3497)', () => {
  it('renders an <img> for a forward-slash drive path', () => {
    const { container } = render(
      <MarkdownRenderer content="![image](C:/Users/me/.kiro/crew/uploads/shot.png)" />,
    )
    expect(srcOf(container)).toBe(
      `/api/file-raw?path=${encodeURIComponent('C:/Users/me/.kiro/crew/uploads/shot.png')}`,
    )
  })

  it('renders an <img> for a space-containing destination (angle-bracket form)', () => {
    const { container } = render(
      <MarkdownRenderer content="![image](<C:/Users/John Doe/uploads/shot.png>)" />,
    )
    expect(srcOf(container)).toBe(
      `/api/file-raw?path=${encodeURIComponent('C:/Users/John Doe/uploads/shot.png')}`,
    )
  })

  it('END-TO-END: the exact displayTxt sent for a Windows upload renders an <img>', () => {
    // The sender bubble renders prepareSendPayload's displayTxt — this is the
    // reported repro path: paste image on Windows, send, look at own bubble.
    const { displayTxt } = prepareSendPayload(
      'look at this',
      ['C:\\Users\\me\\.kiro\\crew\\uploads\\shot.png'],
    )
    const { container } = render(<MarkdownRenderer content={displayTxt} />)
    expect(srcOf(container)).toContain('/api/file-raw?path=')
    expect(srcOf(container)).toContain(
      encodeURIComponent('C:/Users/me/.kiro/crew/uploads/shot.png'),
    )
  })

  it('POSIX paths keep rendering through file-raw (no regression)', () => {
    const { container } = render(
      <MarkdownRenderer content="![image](/home/me/.kiro/crew/uploads/shot.png)" />,
    )
    expect(srcOf(container)).toBe(
      `/api/file-raw?path=${encodeURIComponent('/home/me/.kiro/crew/uploads/shot.png')}`,
    )
  })

  it('END-TO-END: a UNC upload path (roaming profile) renders through file-raw', () => {
    const { displayTxt } = prepareSendPayload(
      '',
      ['\\\\fileserver\\home\\me\\.kiro\\crew\\uploads\\shot.png'],
    )
    const { container } = render(<MarkdownRenderer content={displayTxt} />)
    expect(srcOf(container)).toBe(
      `/api/file-raw?path=${encodeURIComponent('//fileserver/home/me/.kiro/crew/uploads/shot.png')}`,
    )
  })

  it('attacker-authored backslash-UNC markdown never routes to file-raw (review round 3)', () => {
    // A UNC src names a HOST: mapping it to /api/file-raw would hand Windows
    // an outbound SMB probe. Only producer-normalized `//…` uploads take the
    // file-raw route, and the backend validates those against its trusted
    // attachment roots before any resolution.
    const { container } = render(
      <MarkdownRenderer content={'![pwn](\\\\evil\\share\\x.png)'} />,
    )
    const img = container.querySelector('img')
    if (img) {
      expect(img.getAttribute('src') || '').not.toContain('/api/file-raw')
    }
  })

  it('END-TO-END: a literal-% filename round-trips exactly (review round 1)', () => {
    // Producer wraps + escapes % -> %25; the renderer decodes only that
    // wrapped form — the on-disk name with a literal %20 must come back
    // byte-identical, not as a space.
    const { displayTxt } = prepareSendPayload('', ['/tmp/photo%20copy.png'])
    const { container } = render(<MarkdownRenderer content={displayTxt} />)
    expect(srcOf(container)).toBe(
      `/api/file-raw?path=${encodeURIComponent('/tmp/photo%20copy.png')}`,
    )
  })

  it('LEGACY: an unwrapped destination with %XX is preserved verbatim (review round 4)', () => {
    // Pre-existing history wrote raw paths. A file literally named
    // `photo%20copy.png` resolved correctly before destinations were ever
    // encoded — decoding it now would fetch `photo copy.png` instead. The
    // absence of the producer's <…> wrap marks it as decode-exempt.
    const { container } = render(
      <MarkdownRenderer content={'![image](/tmp/photo%20copy.png)'} />,
    )
    expect(srcOf(container)).toBe(
      `/api/file-raw?path=${encodeURIComponent('/tmp/photo%20copy.png')}`,
    )
  })

  it('END-TO-END: backslash-punctuation POSIX path survives the escaped <…> form', () => {
    const { displayTxt } = prepareSendPayload('', ['/tmp/my dir\\.hidden.png'])
    const { container } = render(<MarkdownRenderer content={displayTxt} />)
    expect(srcOf(container)).toBe(
      `/api/file-raw?path=${encodeURIComponent('/tmp/my dir\\.hidden.png')}`,
    )
  })

  it('END-TO-END: a parenthesized duplicate-name path renders (review round 2)', () => {
    // `screenshot (1).png` — the default Windows duplicate-name shape.
    const { displayTxt } = prepareSendPayload('', ['C:\\Users\\me\\uploads\\screenshot (1).png'])
    const { container } = render(<MarkdownRenderer content={displayTxt} />)
    expect(srcOf(container)).toBe(
      `/api/file-raw?path=${encodeURIComponent('C:/Users/me/uploads/screenshot (1).png')}`,
    )
  })

  it('refuses to decode a %00 NUL into the file-raw query (review round 1)', () => {
    // decodeURIComponent('%00') is a real NUL; the backend's realpath raises
    // on it (HTTP 500). The guard keeps the raw form, which 404s harmlessly.
    const { container } = render(<MarkdownRenderer content="![image](/tmp/x%00.png)" />)
    expect(srcOf(container)).toBe(
      `/api/file-raw?path=${encodeURIComponent('/tmp/x%00.png')}`,
    )
  })

  it('remote images stay untouched', () => {
    const { container } = render(
      <MarkdownRenderer content="![image](https://example.com/x.png)" />,
    )
    expect(srcOf(container)).toBe('https://example.com/x.png')
  })

  it('a drive-path LINK still renders with no live href (strict default kept)', () => {
    const { container } = render(
      <MarkdownRenderer content="[open](C:/Users/me/doc.html)" />,
    )
    const a = container.querySelector('a')
    expect(a).not.toBeNull()
    expect(a!.getAttribute('href') || '').toBe('')
  })
})
