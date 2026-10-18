import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, cleanup } from '@testing-library/react'
import { useRef } from 'react'
import { readdirSync, readFileSync } from 'node:fs'
import { join, relative, sep } from 'node:path'
import { useFrameSrc } from '../hooks/useFrameSrc'

// A sandbox-doc URL is served once. Changing a mounted frame's `src` attribute
// leaves the old URL in the top page's history, and Back then renavigates the
// frame onto it: the gateway's `404: Not Found` is painted as the artifact body.
// Measured in Chromium (Playwright): an attribute change grew history.length
// by one and the second Back showed "404: Not Found"; location.replace grew it
// by nothing and Back left the page instead. These tests pin the shape that
// avoids the entry: the attribute is written once, later URLs go through
// location.replace.

const FIRST = '/sandbox-doc/first/tok'
const SECOND = '/sandbox-doc/second/tok'

function Frame({ url }: { url: string | null }) {
  const ref = useRef<HTMLIFrameElement>(null)
  const src = useFrameSrc(ref, url)
  return url ? <iframe ref={ref} src={src} title="t" /> : null
}

function frame(): HTMLIFrameElement {
  const el = document.querySelector('iframe')
  if (!el) throw new Error('frame not mounted')
  return el
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

// Every frame that shows a minted document must take its src from useFrameSrc,
// or the next re-mint leaves a spent url in history. Listed rather than spot
// fixed: the reported surface was one of six.
//
// The one exception keeps its frame keyed on the sandbox grant, so a grant
// change creates a NEW element; this hook pins the attribute to the first url
// it saw, which a keyed remount would then load. That frame needs a design of
// its own and is left as it was.
const NOT_MIGRATED: Record<string, string> = {
  'pages/members/CrewDynamicDashboard.tsx': 'frame keyed on its sandbox grant',
}

function sourceFiles(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((e) => {
    const p = join(dir, e.name)
    if (e.isDirectory()) return e.name === 'test' ? [] : sourceFiles(p)
    return /\.tsx?$/.test(e.name) && !/\.test\./.test(e.name) ? [p] : []
  })
}

describe('sandbox-doc frames', () => {
  const root = join(process.cwd(), 'src')
  const callers = sourceFiles(root)
    .filter((p) => /useSandboxDoc\(/.test(readFileSync(p, 'utf8')))
    .map((p) => relative(root, p).split(sep).join('/'))
    .filter((p) => p !== 'hooks/useSandboxDoc.ts')
    .sort()

  it('finds the callers this list was written against', () => {
    expect(callers).toEqual([
      'components/ArtifactBody.tsx',
      'components/WidgetFrame.tsx',
      'components/library/ArtifactThumbs.tsx',
      'pages/RemoteArtifactDetailPage.tsx',
      'pages/chat/command-center/SessionStatusFrame.tsx',
      'pages/members/CrewDynamicDashboard.tsx',
    ])
  })

  it.each(callers.filter((p) => !(p in NOT_MIGRATED)))('%s renders its frame through useFrameSrc', (p) => {
    const text = readFileSync(join(root, p), 'utf8')
    expect(text).toMatch(/useFrameSrc\(/)
    expect(text).toMatch(/src=\{frameSrc\}/)
  })
})

describe('useFrameSrc', () => {
  it('gives the frame its first url through the attribute', () => {
    render(<Frame url={FIRST} />)
    expect(frame().getAttribute('src')).toBe(FIRST)
  })

  it('replaces later urls in place instead of rewriting the attribute', () => {
    const { rerender } = render(<Frame url={FIRST} />)
    const el = frame()
    const replace = vi.fn()
    Object.defineProperty(el, 'contentWindow', {
      configurable: true,
      value: { location: { replace } },
    })

    rerender(<Frame url={SECOND} />)

    expect(replace).toHaveBeenCalledTimes(1)
    expect(replace).toHaveBeenCalledWith(SECOND)
    // The attribute still names the first document: rewriting it is the
    // navigation that records a history entry.
    expect(el.getAttribute('src')).toBe(FIRST)

    // A re-render with the same url navigates nothing.
    rerender(<Frame url={SECOND} />)
    expect(replace).toHaveBeenCalledTimes(1)
  })

  it('leaves the attribute alone when the frame has no window', () => {
    const { rerender } = render(<Frame url={FIRST} />)
    const el = frame()
    Object.defineProperty(el, 'contentWindow', { configurable: true, value: null })
    rerender(<Frame url={SECOND} />)
    expect(el.getAttribute('src')).toBe(FIRST)
  })

  it('creates a remounted frame with the current url, not the first one', () => {
    const { rerender } = render(<Frame url={FIRST} />)
    rerender(<Frame url={null} />)
    expect(document.querySelector('iframe')).toBeNull()
    rerender(<Frame url={SECOND} />)
    expect(frame().getAttribute('src')).toBe(SECOND)
  })

})
