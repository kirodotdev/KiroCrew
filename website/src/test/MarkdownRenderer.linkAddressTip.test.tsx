/**
 * A plain web link shows its full address in a bubble once the pointer rests on
 * it (`LINK_ADDRESS_DELAY_MS`) or keyboard focus lands on it. Links that are
 * in-app or already explain themselves get none, and neither does a link an
 * app's link-preview card answers: one hover, one pop-up.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, fireEvent, render, screen } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'
import { LINK_ADDRESS_DELAY_MS, linkAddress } from '../components/markdown/linkAddressTip'
import { resolveLinkPreviews, setServedLinkPreviews } from '../linkPreview/linkPreviewRegistry'

const TICKETS_APP = {
  name: 'cards-app',
  enabled: true,
  manifest: { contributes: { linkPreviews: [{ id: 'cards', entry: 'dist/lp.mjs', hosts: ['tickets.example.com'] }] } },
}

beforeEach(() => { vi.useFakeTimers() })
afterEach(() => {
  setServedLinkPreviews([])
  vi.useRealTimers()
})

/** Rest the pointer on `el` for `ms`. */
function rest(el: Element, ms = LINK_ADDRESS_DELAY_MS) {
  fireEvent.mouseEnter(el)
  act(() => { vi.advanceTimersByTime(ms) })
}

describe('link address bubble', () => {
  it('shows the full address after the pause and hides when the pointer leaves', () => {
    render(<MarkdownRenderer content={'Check [the runbook](https://example.com/ops/runbook?step=3#rollback) first.'} />)
    const link = screen.getByRole('link', { name: 'the runbook' })

    rest(link, LINK_ADDRESS_DELAY_MS - 1)
    expect(screen.queryByRole('tooltip')).toBeNull()
    act(() => { vi.advanceTimersByTime(1) })
    expect(screen.getByRole('tooltip')).toHaveTextContent('https://example.com/ops/runbook?step=3#rollback')

    fireEvent.mouseLeave(link)
    expect(screen.queryByRole('tooltip')).toBeNull()
  })

  it('shows at once on a keyboard focus and hides on blur', () => {
    render(<MarkdownRenderer content={'See [the docs](https://example.com/docs) now.'} />)
    const link = screen.getByRole('link', { name: 'the docs' })

    act(() => { link.focus() })
    expect(screen.getByRole('tooltip')).toHaveTextContent('https://example.com/docs')

    act(() => { link.blur() })
    expect(screen.queryByRole('tooltip')).toBeNull()
  })

  it('paints nothing for the focus a mouse click leaves on the link', () => {
    render(<MarkdownRenderer content={'See [the docs](https://example.com/docs) now.'} />)
    const link = screen.getByRole('link', { name: 'the docs' })
    const matches = link.matches.bind(link)
    vi.spyOn(link, 'matches').mockImplementation(sel => sel === ':focus-visible' ? false : matches(sel))

    act(() => { link.focus() })
    expect(screen.queryByRole('tooltip')).toBeNull()
  })

  it('caps the bubble at the window width as well as 28rem, since InstantTip clamps only its start', () => {
    render(<MarkdownRenderer content={'See [the docs](https://example.com/docs) now.'} />)

    rest(screen.getByRole('link', { name: 'the docs' }))
    expect(screen.getByRole('tooltip')).toHaveClass('max-w-[min(28rem,calc(100vw_-_1rem))]')
  })

  it('adds no description or title to the anchor', () => {
    render(<MarkdownRenderer content={'See [the docs](https://example.com/docs) now.'} />)
    const link = screen.getByRole('link', { name: 'the docs' })
    expect(link).not.toHaveAttribute('aria-describedby')
    expect(link).not.toHaveAttribute('title')
  })

  it("shows an email link's whole mailto: address, pre-filled subject included", () => {
    render(<MarkdownRenderer content={'Or [write to us](mailto:team@example.com?subject=Hello).'} />)

    rest(screen.getByRole('link', { name: 'write to us' }))
    expect(screen.getByRole('tooltip')).toHaveTextContent('mailto:team@example.com?subject=Hello')
  })

  it('stays off an in-page heading link, which has no address to show', () => {
    render(<MarkdownRenderer content={'Jump to [the top](#top).'} />)

    rest(screen.getByText('the top'))
    expect(screen.queryByRole('tooltip')).toBeNull()
  })

  it("leaves a link to an app's link-preview card in a preview scope", () => {
    setServedLinkPreviews(resolveLinkPreviews([TICKETS_APP]))
    render(
      <div data-link-preview-scope="">
        <MarkdownRenderer content={'Ticket [T-1](https://tickets.example.com/T-1) and [notes](https://example.com/notes).'} />
      </div>,
    )

    rest(screen.getByRole('link', { name: 'T-1' }))
    expect(screen.queryByRole('tooltip')).toBeNull()

    rest(screen.getByRole('link', { name: 'notes' }))
    expect(screen.getByRole('tooltip')).toHaveTextContent('https://example.com/notes')
  })

  it("shows a previewed link's address on keyboard focus, which the card layer does not answer", () => {
    setServedLinkPreviews(resolveLinkPreviews([TICKETS_APP]))
    render(
      <div data-link-preview-scope="">
        <MarkdownRenderer content={'Ticket [T-1](https://tickets.example.com/T-1).'} />
      </div>,
    )
    const link = screen.getByRole('link', { name: 'T-1' })

    act(() => { link.focus() })
    expect(screen.getByRole('tooltip')).toHaveTextContent('https://tickets.example.com/T-1')

    // The pointer arriving hands the link to the app's card: the bubble goes,
    // and the hover's pause does not bring it back.
    rest(link)
    expect(screen.queryByRole('tooltip')).toBeNull()
  })

  it('shows the address for a claimed host outside a preview scope, where no card opens', () => {
    setServedLinkPreviews(resolveLinkPreviews([TICKETS_APP]))
    render(<MarkdownRenderer content={'Ticket [T-1](https://tickets.example.com/T-1).'} />)

    rest(screen.getByRole('link', { name: 'T-1' }))
    expect(screen.getByRole('tooltip')).toHaveTextContent('https://tickets.example.com/T-1')
  })
})

describe('linkAddress', () => {
  it('shows http(s) and mailto: addresses whole', () => {
    expect(linkAddress('https://example.com/a?b=1#c')).toBe('https://example.com/a?b=1#c')
    expect(linkAddress('http://example.com/')).toBe('http://example.com/')
    expect(linkAddress('mailto:team@example.com?subject=Hi')).toBe('mailto:team@example.com?subject=Hi')
  })

  it('shows nothing for a credential-carrying URL, a script, an editor scheme or an in-app path', () => {
    for (const href of ['https://user:pw@example.com/x', 'javascript:alert(1)', 'vscode://file/a.ts', '#top', '/workspace/notes.md', 'src/a.ts']) {
      expect(linkAddress(href)).toBeUndefined()
    }
  })
})
