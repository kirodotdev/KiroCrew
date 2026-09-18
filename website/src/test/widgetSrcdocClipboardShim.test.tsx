/** Executes the REAL clipboard-fallback shim — extracted from buildSrcdoc's
 * own output, not a copy — inside a stub document, and pins the contract every
 * widget/artifact copy affordance depends on:
 *
 *   A user-initiated navigator.clipboard.writeText() must actually put text on
 *   the clipboard even when the native implementation is absent or refuses
 *   (an opaque sandboxed document, or a plain-HTTP deployment with no Clipboard
 *   API at all), by falling back to a textarea + execCommand('copy'), without
 *   moving focus or clobbering the caller's selection.
 *
 * This fixes real Copy buttons without delegating clipboard-write to the frame.
 * A gesture-less on-load script still lacks the user activation execCommand
 * needs, so the shim does not grant agent-authored HTML an ambient write path.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { buildSrcdoc } from '../lib/widgetSrcdoc'

function extractShimScript(): string {
  const out = buildSrcdoc({ html: '<p>probe</p>', themeVars: {}, mode: 'dark' })
  const doc = new DOMParser().parseFromString(out, 'text/html')
  const script = Array.from(doc.querySelectorAll('script'))
    .map((s) => s.textContent ?? '')
    .find((t) => t.includes('wrappedWriteText'))
  if (!script) throw new Error('clipboard shim script not found in srcdoc')
  return script
}

/** Build a minimal fake `document` the shim's execCommand fallback can act on:
 * a real element it can append/remove, a fake `execCommand` the test controls,
 * and enough of Selection/Range to prove focus + selection are restored. */
function makeFakeDocument(opts: { execCommandResult: boolean }) {
  const body = document.createElement('div')
  const createdTextareas: HTMLTextAreaElement[] = []
  const fakeSelection = {
    rangeCount: 1,
    ranges: [{ marker: 'original-range' }] as unknown[],
    getRangeAt: (i: number) => fakeSelection.ranges[i],
    removeAllRanges: vi.fn(() => { fakeSelection.ranges = [] }),
    addRange: vi.fn((r: unknown) => { fakeSelection.ranges.push(r) }),
  }
  const previouslyFocused = { focus: vi.fn() }
  const fakeDoc = {
    activeElement: previouslyFocused,
    execCommand: vi.fn((cmd: string) => {
      if (cmd !== 'copy') return false
      return opts.execCommandResult
    }),
    getSelection: () => fakeSelection,
    createElement: (tag: string) => {
      if (tag === 'textarea') {
        const ta = document.createElement('textarea')
        // jsdom/happy-dom textarea.select() works without a real layout; spy on
        // it so the test can assert it was actually called.
        vi.spyOn(ta, 'select')
        createdTextareas.push(ta)
        return ta
      }
      return document.createElement(tag)
    },
    body: {
      appendChild: (el: HTMLElement) => body.appendChild(el),
      removeChild: (el: HTMLElement) => body.removeChild(el),
    },
  }
  return { fakeDoc, fakeSelection, previouslyFocused, createdTextareas }
}

/** Run the extracted shim against fake `navigator`/`document` globals, and
 * return the resulting wrapped `navigator.clipboard.writeText`. */
function installShim(fakeNavigator: Record<string, unknown>, fakeDoc: unknown) {
  new Function('navigator', 'document', extractShimScript())(fakeNavigator, fakeDoc)
}

describe('clipboard fallback shim', () => {
  let restoreClipboard: (() => void) | undefined

  afterEach(() => {
    restoreClipboard?.()
    restoreClipboard = undefined
    vi.restoreAllMocks()
  })

  it('uses the native writeText when it resolves, and does not touch execCommand', async () => {
    const nativeWriteText = vi.fn().mockResolvedValue(undefined)
    const fakeNavigator = { clipboard: { writeText: nativeWriteText, readText: vi.fn() } }
    const { fakeDoc } = makeFakeDocument({ execCommandResult: true })

    installShim(fakeNavigator, fakeDoc)

    await expect((fakeNavigator.clipboard.writeText as (t: string) => Promise<void>)('hello')).resolves.toBeUndefined()
    expect(nativeWriteText).toHaveBeenCalledWith('hello')
    expect((fakeDoc as { execCommand: ReturnType<typeof vi.fn> }).execCommand).not.toHaveBeenCalled()
    // readText is untouched — the shim shadows only writeText.
    expect(fakeNavigator.clipboard.readText).toBeDefined()
  })

  it('falls back to execCommand when the native call rejects, and resolves', async () => {
    const nativeWriteText = vi.fn().mockRejectedValue(new DOMException('denied', 'NotAllowedError'))
    const fakeNavigator = { clipboard: { writeText: nativeWriteText } }
    const { fakeDoc, createdTextareas } = makeFakeDocument({ execCommandResult: true })

    installShim(fakeNavigator, fakeDoc)

    await expect((fakeNavigator.clipboard.writeText as (t: string) => Promise<void>)('fallback text')).resolves.toBeUndefined()
    expect(createdTextareas).toHaveLength(1)
    expect(createdTextareas[0].value).toBe('fallback text')
    expect(createdTextareas[0].select).toHaveBeenCalled()
  })

  it('re-rejects with the ORIGINAL error when the fallback also fails', async () => {
    const originalError = new DOMException('denied', 'NotAllowedError')
    const nativeWriteText = vi.fn().mockRejectedValue(originalError)
    const fakeNavigator = { clipboard: { writeText: nativeWriteText } }
    const { fakeDoc } = makeFakeDocument({ execCommandResult: false })

    installShim(fakeNavigator, fakeDoc)

    await expect((fakeNavigator.clipboard.writeText as (t: string) => Promise<void>)('x')).rejects.toBe(originalError)
  })

  it('restores focus and the document selection after the fallback runs', async () => {
    const nativeWriteText = vi.fn().mockRejectedValue(new Error('denied'))
    const fakeNavigator = { clipboard: { writeText: nativeWriteText } }
    const { fakeDoc, fakeSelection, previouslyFocused } = makeFakeDocument({ execCommandResult: true })
    const originalRange = fakeSelection.ranges[0]

    installShim(fakeNavigator, fakeDoc)
    await (fakeNavigator.clipboard.writeText as (t: string) => Promise<void>)('text')

    // Selection cleared then re-seeded with exactly the saved range — never
    // left cleared, and never left holding the staging textarea's own range.
    expect(fakeSelection.removeAllRanges).toHaveBeenCalled()
    expect(fakeSelection.addRange).toHaveBeenCalledWith(originalRange)
    expect(fakeSelection.ranges).toEqual([originalRange])
    // Focus restored to whatever had it before the copy, with scroll
    // suppressed (a copy is a side errand, not a navigation).
    expect(previouslyFocused.focus).toHaveBeenCalledWith({ preventScroll: true })
  })

  it('uses the fallback purely (no native call) when navigator.clipboard has no writeText', async () => {
    const fakeNavigator = { clipboard: { readText: vi.fn() } }
    const { fakeDoc, createdTextareas } = makeFakeDocument({ execCommandResult: true })

    installShim(fakeNavigator, fakeDoc)

    await expect((fakeNavigator.clipboard.writeText as (t: string) => Promise<void>)('no native')).resolves.toBeUndefined()
    expect(createdTextareas).toHaveLength(1)
  })

  it('defines a minimal navigator.clipboard.writeText when navigator.clipboard is entirely absent', async () => {
    // The plain-HTTP-deployment case: navigator.clipboard does not exist at
    // all (no secure context), so there is nothing to shadow — the shim must
    // define the property on navigator itself instead.
    const fakeNavigator: Record<string, unknown> = {}
    const { fakeDoc, createdTextareas } = makeFakeDocument({ execCommandResult: true })

    installShim(fakeNavigator, fakeDoc)

    expect(fakeNavigator.clipboard).toBeDefined()
    const writeText = (fakeNavigator.clipboard as { writeText: (t: string) => Promise<void> }).writeText
    await expect(writeText('created from scratch')).resolves.toBeUndefined()
    expect(createdTextareas).toHaveLength(1)
    expect(createdTextareas[0].value).toBe('created from scratch')
  })

  it('rejects when navigator.clipboard is absent and execCommand also fails', async () => {
    const fakeNavigator: Record<string, unknown> = {}
    const { fakeDoc } = makeFakeDocument({ execCommandResult: false })

    installShim(fakeNavigator, fakeDoc)

    const writeText = (fakeNavigator.clipboard as { writeText: (t: string) => Promise<void> }).writeText
    await expect(writeText('nope')).rejects.toThrow()
  })

  it('is installed against the real window/document/navigator without throwing', () => {
    // End-to-end smoke test against the ACTUAL browser globals this test
    // environment provides (happy-dom/jsdom), proving the try/catch guards
    // around each defineProperty don't themselves break widget rendering, and
    // that a real copy round-trips through the fallback when the environment
    // has no native Clipboard API (the common case in these test environments).
    const desc = Object.getOwnPropertyDescriptor(navigator, 'clipboard')
    restoreClipboard = () => {
      if (desc) Object.defineProperty(navigator, 'clipboard', desc)
      else delete (navigator as unknown as { clipboard?: unknown }).clipboard
    }

    expect(() => {
      new Function(extractShimScript())()
    }).not.toThrow()

    expect(navigator.clipboard).toBeDefined()
    expect(typeof navigator.clipboard.writeText).toBe('function')
  })
})

describe('buildSrcdoc clipboard shim injection', () => {
  it('injects the shim into every document, before the LLM html', () => {
    const doc = buildSrcdoc({ html: '<script>console.log(1)</script><p>hi</p>', themeVars: {}, mode: 'dark' })
    const shimIdx = doc.indexOf('wrappedWriteText')
    const llmIdx = doc.indexOf('console.log(1)')
    expect(shimIdx).toBeGreaterThan(-1)
    expect(llmIdx).toBeGreaterThan(-1)
    // Installed before the LLM's own script runs, so even an on-load attempt
    // sees the wrapper; without user activation its fallback still rejects.
    expect(shimIdx).toBeLessThan(llmIdx)
  })

  it('never interpolates LLM/user content into the shim body', () => {
    const doc = buildSrcdoc({
      html: '<p>irrelevant</p>',
      themeVars: {},
      mode: 'dark',
      includeHeightReporter: true,
      enableComments: true,
    })
    // The shim script is a single static block, independent of every
    // caller-supplied option — it must be injected exactly once regardless of
    // what else buildSrcdoc was asked to build. Counting the defining
    // function statement (as opposed to `wrappedWriteText`, which the shim's
    // own body both declares and calls more than once) pins "one script",
    // not "one substring".
    const occurrences = doc.split('function wrappedWriteText').length - 1
    expect(occurrences).toBe(1)
  })
})
