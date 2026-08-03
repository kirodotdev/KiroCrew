import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import { LinkChip, LinkCard } from '../components/LinkPreview'
import type { LinkMeta } from '../lib/linkMeta'

const HREF = 'https://example.com/post'

const meta = (over: Partial<LinkMeta> = {}): LinkMeta => ({
  url: HREF,
  title: 'Example Title',
  description: 'A description of the page that is long enough to clamp.',
  siteName: 'Example',
  domain: 'example.com',
  icon: 'data:image/png;base64,AAAA',
  fetchedAt: 1770000000,
  ...over,
})

describe('LinkChip', () => {
  it('copies the original URL from the chip, restoring what plain text allowed', async () => {
    // Before unfurling, a link was raw URL text: selecting it and copying gave
    // you the URL. Rendering the page title in its place takes that capability
    // away, so the chip carries the same copy affordance the card does.
    const writeText = vi.fn(async () => {})
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } })
    const deep = 'https://example.com/a/deep/path?x=1'

    render(<LinkChip meta={meta()} href={deep} />)
    const button = screen.getByRole('button', { name: `Copy URL of ${meta().title}` })
    await act(async () => { fireEvent.click(button) })

    expect(writeText).toHaveBeenCalledWith(deep)
    expect(screen.getByRole('button', { name: 'Copied' })).toBeTruthy()
  })

  it('keeps the chip copy button OUTSIDE the anchor', () => {
    // Same invalid-nesting rule as the card: a <button> inside an <a> is
    // interactive content nested in a link, and one click would fire both.
    const { container } = render(<LinkChip meta={meta()} href={HREF} />)
    expect(container.querySelector('a')!.querySelector('button')).toBeNull()
    expect(container.querySelector('button')).not.toBeNull()
  })

  it('does not let a copy click reach the surrounding message handlers', () => {
    // The chip renders inside prose whose container delegates clicks for
    // artifact links and path chips; without stopPropagation one click would
    // both copy and navigate.
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText: vi.fn(async () => {}) } })
    // The listener goes on a node ABOVE React's root, attached imperatively.
    // Both details matter: a JSX click handler on a plain span would trip the
    // repo's non-native-interactive rule (which scans added lines in tests too),
    // and a listener on the root itself would fire regardless — `stopPropagation`
    // prevents further BUBBLING, not other listeners already bound to the same
    // node, so that version of this test would pass vacuously.
    const outer = document.createElement('div')
    const mount = document.createElement('div')
    outer.appendChild(mount)
    document.body.appendChild(outer)
    const reachedContainer = vi.fn()
    outer.addEventListener('click', reachedContainer)

    render(<LinkChip meta={meta()} href={HREF} />, { container: mount })
    fireEvent.click(mount.querySelector('button')!)

    expect(reachedContainer).not.toHaveBeenCalled()
    outer.remove()
  })

  it('exposes an accessible name from the page title, not the url', () => {
    render(<LinkChip meta={meta()} href={HREF} />)
    const link = screen.getByRole('link', { name: 'Example Title' })
    expect(link).toHaveAttribute('href', HREF)
  })

  it('opens in a new tab with noopener noreferrer', () => {
    render(<LinkChip meta={meta()} href={HREF} />)
    const link = screen.getByRole('link', { name: 'Example Title' })
    expect(link).toHaveAttribute('target', '_blank')
    expect(link.getAttribute('rel')).toBe('noopener noreferrer')
  })

  it('renders the favicon as a decorative image that is not announced', () => {
    const { container } = render(<LinkChip meta={meta()} href={HREF} />)
    const img = container.querySelector('img')!
    expect(img.getAttribute('alt')).toBe('')
    expect(img).toHaveAttribute('src', 'data:image/png;base64,AAAA')
    expect(img.closest('[aria-hidden="true"]')).not.toBeNull()
    // The title is the whole accessible name — no icon text leaks into it.
    expect(screen.getByRole('link').textContent).toBe('Example Title')
  })

  it('hides the img on load error and keeps the fixed-size box', () => {
    const { container } = render(<LinkChip meta={meta()} href={HREF} />)
    const box = container.querySelector('[aria-hidden="true"]')!
    const before = box.className
    fireEvent.error(container.querySelector('img')!)
    expect(container.querySelector('img')).toBeNull()
    // Same box, same classes: the placeholder occupies the reserved space, so
    // a broken icon cannot reflow the sentence around the chip.
    expect(container.querySelector('[aria-hidden="true"]')!.className).toBe(before)
    expect(screen.getByRole('link', { name: 'Example Title' })).toBeTruthy()
  })

  it('renders no img at all when there is no icon', () => {
    const { container } = render(<LinkChip meta={meta({ icon: '' })} href={HREF} />)
    expect(container.querySelector('img')).toBeNull()
    expect(container.querySelector('[aria-hidden="true"]')).not.toBeNull()
  })

  it('truncates on one line', () => {
    render(<LinkChip meta={meta()} href={HREF} />)
    const label = screen.getByText('Example Title')
    expect(label.className).toContain('truncate')
  })

  it('falls back to the domain when the title is empty', () => {
    render(<LinkChip meta={meta({ title: '' })} href={HREF} />)
    expect(screen.getByRole('link', { name: 'example.com' })).toBeTruthy()
  })

  it('falls back to the original anchor content when title and domain are empty', () => {
    render(<LinkChip meta={meta({ title: '', domain: '' })} href={HREF}>original text</LinkChip>)
    expect(screen.getByRole('link', { name: 'original text' })).toBeTruthy()
  })

  it('uses no hardcoded colors', () => {
    const { container } = render(<LinkChip meta={meta()} href={HREF} />)
    expect(container.innerHTML).not.toMatch(/#[0-9a-fA-F]{3,8}\b/)
    expect(container.innerHTML).not.toMatch(/rgba?\(/)
  })
})

describe('LinkCard', () => {
  it('exposes an accessible name from the page title', () => {
    render(<LinkCard meta={meta()} href={HREF} />)
    const link = screen.getByRole('link', { name: /Example Title/ })
    expect(link).toHaveAttribute('href', HREF)
    expect(link).toHaveAttribute('target', '_blank')
    expect(link.getAttribute('rel')).toBe('noopener noreferrer')
  })

  it('renders title, 2-line-clamped description and domain', () => {
    render(<LinkCard meta={meta()} href={HREF} />)
    expect(screen.getByText('Example Title').className).toContain('font-semibold')
    const desc = screen.getByText('A description of the page that is long enough to clamp.')
    expect(desc.className).toContain('line-clamp-2')
    expect(screen.getByText('example.com').className).toContain('text-muted-strong')
  })

  it('omits the description row when there is none', () => {
    render(<LinkCard meta={meta({ description: '' })} href={HREF} />)
    expect(document.querySelector('.line-clamp-2')).toBeNull()
  })

  it('hides the img on load error', () => {
    const { container } = render(<LinkCard meta={meta()} href={HREF} />)
    fireEvent.error(container.querySelector('img')!)
    expect(container.querySelector('img')).toBeNull()
    expect(screen.getByRole('link', { name: /Example Title/ })).toBeTruthy()
  })

  it('keeps the favicon out of the accessible name', () => {
    const { container } = render(<LinkCard meta={meta()} href={HREF} />)
    expect(container.querySelector('img')!.getAttribute('alt')).toBe('')
    expect(container.querySelector('img')!.closest('[aria-hidden="true"]')).not.toBeNull()
  })

  it('uses no hardcoded colors', () => {
    const { container } = render(<LinkCard meta={meta()} href={HREF} />)
    expect(container.innerHTML).not.toMatch(/#[0-9a-fA-F]{3,8}\b/)
    expect(container.innerHTML).not.toMatch(/rgba?\(/)
  })

  it('copies the original URL, not the rendered title, and confirms with a check', async () => {
    // Unfurling puts the page title where the URL used to be, so this button is
    // the affordance that still yields the URL. It must copy the href verbatim.
    const writeText = vi.fn(async () => {})
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } })
    const deep = 'https://example.com/a/deep/path?x=1'

    render(<LinkCard meta={meta()} href={deep} />)
    // The accessible name names the TARGET, not just the action: a paragraph can
    // hold several of these, and three buttons all called "Copy URL" give a
    // screen-reader user no way to tell which link each one belongs to.
    const button = screen.getByRole('button', { name: `Copy URL of ${meta().title}` })
    await act(async () => { fireEvent.click(button) })

    expect(writeText).toHaveBeenCalledWith(deep)
    expect(screen.getByRole('button', { name: 'Copied' })).toBeTruthy()
  })

  it('keeps the copy button OUTSIDE the anchor so one click does one thing', () => {
    // A <button> inside an <a> is interactive content nested in a link: invalid
    // markup, and a click would both copy and navigate.
    const { container } = render(<LinkCard meta={meta()} href={HREF} />)
    const anchor = container.querySelector('a')!
    expect(anchor.querySelector('button')).toBeNull()
    expect(container.querySelector('button')).not.toBeNull()
  })

  it('publishes the original URL as an attribute, since the text no longer holds it', () => {
    // A plain text selection over an unfurled link captures the TITLE — the URL
    // is not in the rendered characters at all — so it is exposed on the node.
    const { container } = render(<LinkCard meta={meta()} href={HREF} />)
    expect(container.querySelector(`[data-unfurl-url="${HREF}"]`)).not.toBeNull()
  })
})


afterEach(() => { vi.unstubAllGlobals() })
