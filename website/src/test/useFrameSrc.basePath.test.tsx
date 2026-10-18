import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, cleanup } from '@testing-library/react'
import { useRef } from 'react'

// Under a sub-path build (KIROCREW_BASE_PATH) the DOM shim rebases the `src`
// attribute React writes, so the attribute never equals the minted
// root-relative url. The hook must neither read the attribute back as "the
// document shown" (it would navigate on the first mount) nor hand
// `location.replace` an un-based url (it would leave the mount).

const BASE = '/proxy/kirocrew'

vi.mock('../lib/basePath', async (importOriginal) => {
  const real = await importOriginal<typeof import('../lib/basePath')>()
  return { ...real, BASE_PATH: BASE, withBase: (p: string) => real.withBase(p, BASE) }
})

const { installBasePathDomShims } = await import('../lib/basePath')
const { useFrameSrc } = await import('../hooks/useFrameSrc')

const FIRST = '/sandbox-doc/first/tok'
const SECOND = '/sandbox-doc/second/tok'

function Frame({ url }: { url: string }) {
  const ref = useRef<HTMLIFrameElement>(null)
  const src = useFrameSrc(ref, url)
  return <iframe ref={ref} src={src} title="t" />
}

const nativeSetAttribute = Element.prototype.setAttribute
const nativeIframeSrc = Object.getOwnPropertyDescriptor(HTMLIFrameElement.prototype, 'src')

afterEach(() => {
  cleanup()
  Element.prototype.setAttribute = nativeSetAttribute
  if (nativeIframeSrc) Object.defineProperty(HTMLIFrameElement.prototype, 'src', nativeIframeSrc)
})

describe('useFrameSrc under a sub-path build', () => {
  it('does not renavigate a first document the shim rebased, and rebases the next one', () => {
    installBasePathDomShims(window as never, BASE)
    const replace = vi.fn()
    // The frame's window is installed the moment the element exists, so a
    // navigation on the FIRST mount would be caught too.
    const nativeCreate = document.createElement.bind(document)
    vi.spyOn(document, 'createElement').mockImplementation(((tag: string, opts?: ElementCreationOptions) => {
      const el = nativeCreate(tag, opts)
      if (tag.toLowerCase() === 'iframe') {
        Object.defineProperty(el, 'contentWindow', { configurable: true, value: { location: { replace } } })
      }
      return el
    }) as typeof document.createElement)

    const { rerender } = render(<Frame url={FIRST} />)
    const el = document.querySelector('iframe') as HTMLIFrameElement
    expect(el.getAttribute('src')).toBe(`${BASE}${FIRST}`)
    expect(replace).not.toHaveBeenCalled()

    rerender(<Frame url={SECOND} />)
    expect(replace).toHaveBeenCalledTimes(1)
    expect(replace).toHaveBeenCalledWith(`${BASE}${SECOND}`)
  })
})
