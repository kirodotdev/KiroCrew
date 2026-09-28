/**
 * Exactly one produced message carries a clickable address, and nothing else does.
 *
 * The safety half matters as much as the affordance: `friendlyErrText` passes
 * server-authored text through, so linkifying on shape rather than on provenance would
 * let a hostile 403 body offer the reader a link. Keying on the produced framed message
 * makes that impossible by construction -- and makes a half-linked longer URL
 * impossible too, since an arbitrary message is never scanned at all.
 */
import { describe, it, expect, afterEach, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { withOriginLink } from '../components/withOriginLink'
import { ErrorCard } from '../pages/chat/ErrorCard'
import ErrorNotice from '../components/ErrorNotice'
import { edgeChallengeMessage } from '../api/edgeAuthChallenge'

const ORIGIN = 'https://crew-remote-3.internal:8443'

/**
 * Only `location` is stubbed: react-dom reads real `window` properties, so replacing
 * the whole object breaks rendering rather than the code under test.
 */
const at = (origin: string) => {
  vi.stubGlobal('location', { origin })
}

/** The framed message as the product actually produces it, origin included. */
const framedMessage = () => edgeChallengeMessage('framed') as string

afterEach(() => { vi.unstubAllGlobals() })

describe('withOriginLink', () => {
  it('links the address in the one message that names one', () => {
    at(ORIGIN)
    render(<div>{withOriginLink(framedMessage())}</div>)
    const link = screen.getByRole('link', { name: ORIGIN })
    expect(link).toHaveAttribute('href', ORIGIN)
    expect(link).toHaveAttribute('target', '_blank')
    // Without noreferrer the opened tab can reach back through window.opener.
    expect(link.getAttribute('rel')).toContain('noopener')
    expect(link.getAttribute('rel')).toContain('noreferrer')
  })

  it('keeps the surrounding words, so the remedy still reads as a sentence', () => {
    at(ORIGIN)
    const message = framedMessage()
    const { container } = render(<div>{withOriginLink(message)}</div>)
    expect(container.textContent).toBe(message)
  })

  it('does NOT link a URL that came from the response body', () => {
    at(ORIGIN)
    // The attacker-authored shape: a same-origin action route named by a hostile 403.
    const hostile = 'HTTP 403: see https://evil.example/projects?applied=9&autoRun=true'
    const { container } = render(<div>{withOriginLink(hostile)}</div>)
    expect(container.querySelector('a')).toBeNull()
    expect(container.textContent).toBe(hostile)
  })

  it.each([
    ['a longer address starting with the origin', `${ORIGIN}/api/sessions failed`],
    ['prose that merely quotes the origin', `Could not reach ${ORIGIN} just now.`],
    ['an ordinary message', 'Session expired.'],
  ])('does not link %s', (_name, text) => {
    at(ORIGIN)
    const { container } = render(<div>{withOriginLink(text)}</div>)
    expect(container.querySelector('a')).toBeNull()
    expect(container.textContent).toBe(text)
  })

  it('does not link on an opaque-origin document', () => {
    at('null')
    const { container } = render(<div>{withOriginLink(framedMessage())}</div>)
    expect(container.querySelector('a')).toBeNull()
  })

  it('survives a location that refuses to answer', () => {
    const loc = {} as Record<string, unknown>
    Object.defineProperty(loc, 'origin', {
      get() { throw new DOMException('cross-origin', 'SecurityError') },
    })
    vi.stubGlobal('location', loc)
    const { container } = render(<div>{withOriginLink('Session expired.')}</div>)
    expect(container.textContent).toBe('Session expired.')
  })
})

describe('the card that actually renders it', () => {
  it('links the address, so the helper being wired is pinned and not just its logic', () => {
    at(ORIGIN)
    render(<ErrorCard content={framedMessage()} />)
    expect(screen.getByRole('link', { name: ORIGIN })).toHaveAttribute('href', ORIGIN)
  })

  it('leaves a card with no address alone', () => {
    at(ORIGIN)
    const { container } = render(<ErrorCard content="HTTP 403" />)
    expect(container.querySelector('a')).toBeNull()
  })
})

/**
 * One case per wired surface, because the helper being correct says nothing about
 * where it is called. Both components branch on their props, so a single render
 * exercises ONE branch and leaves the others free to lose the call unnoticed --
 * which is exactly what happened: six of the seven call sites were wired and
 * unpinned, and mutation controls that unwired them stayed green.
 */
describe('every surface that renders an error message', () => {
  const framed = () => framedMessage()
  it.each([
    ['a signed-out row, which offers sign-in instead of Resume',
      () => <ErrorCard content={framed()} onOpenSignIn={() => {}} />],
    ['an entitlement row, which offers the two model actions',
      () => <ErrorCard content={framed()} onPickModel={() => {}} onOpenDefaultModel={() => {}} />],
    ['a resumable row, which carries Resume beside the prose',
      () => <ErrorCard content={framed()} onContinue={() => {}} />],
    ['a plain row',
      () => <ErrorCard content={framed()} />],
    ['the inline notice',
      () => <ErrorNotice message={framed()} variant="inline" />],
    ['the block notice',
      () => <ErrorNotice message={framed()} />],
    ['the block notice with a styled message, a separately wrapped branch',
      () => <ErrorNotice message={framed()} messageClassName="text-[12px]" />],
  ])('links the address on %s', (_name, mount) => {
    at(ORIGIN)
    render(mount())
    expect(screen.getByRole('link', { name: ORIGIN })).toHaveAttribute('href', ORIGIN)
  })
})
