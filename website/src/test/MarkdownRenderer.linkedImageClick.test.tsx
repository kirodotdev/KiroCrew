import { describe, it, expect, afterEach } from 'vitest'
import { render, cleanup } from '@testing-library/react'
import MarkdownRenderer from '../components/MarkdownRenderer'
import { BasePathCtx } from '../components/markdown/contexts'

/**
 * Markdown often wraps an image in a link to the image itself, and that link
 * opens in a new tab. A click on such an image in the side panel used to open
 * the image viewer AND a new tab, which loaded the dashboard again. A plain
 * click must do one thing: open the viewer. A modified click still belongs to
 * the link, and so does a link that goes somewhere other than the image.
 */

afterEach(cleanup)

function clickImage(content: string, init: MouseEventInit = {}) {
  const opened: Event[] = []
  const onLightbox = (e: Event) => { opened.push(e) }
  window.addEventListener('lightbox', onLightbox)
  try {
    const { container } = render(
      <BasePathCtx.Provider value="/home/u/docs">
        <MarkdownRenderer content={content} sourcePos />
      </BasePathCtx.Provider>,
    )
    const img = container.querySelector('img')
    if (!img) throw new Error('no <img> rendered')
    const click = new MouseEvent('click', { bubbles: true, cancelable: true, button: 0, ...init })
    img.dispatchEvent(click)
    return { followsLink: !click.defaultPrevented, opensViewer: opened.length === 1 }
  } finally {
    window.removeEventListener('lightbox', onLightbox)
  }
}

describe('a click on a linked markdown image', () => {
  it('opens only the viewer when the link goes to the image', () => {
    expect(clickImage('[![arch](diagram.png)](diagram.png)')).toEqual({ followsLink: false, opensViewer: true })
  })

  it('treats ./diagram.png and diagram.png as the same image', () => {
    expect(clickImage('[![arch](./diagram.png)](diagram.png)')).toEqual({ followsLink: false, opensViewer: true })
  })

  it('leaves a modified click to the link, without the viewer', () => {
    for (const mod of ['metaKey', 'ctrlKey', 'shiftKey', 'altKey'] as const) {
      expect(clickImage('[![arch](diagram.png)](diagram.png)', { [mod]: true })).toEqual({ followsLink: true, opensViewer: false })
    }
  })

  it('leaves a link that goes somewhere else to that link, without the viewer', () => {
    for (const href of ['https://ci.example.com/build/1', 'diagram-large.png', '#details']) {
      expect(clickImage(`[![arch](diagram.png)](${href})\n\n## Details`).opensViewer).toBe(false)
    }
  })

  it('tells ../diagram.png and ../../diagram.png apart', () => {
    expect(clickImage('[![arch](../diagram.png)](../../diagram.png)').opensViewer).toBe(false)
    expect(clickImage('[![arch](../diagram.png)](../diagram.png)').opensViewer).toBe(true)
  })

  it('leaves a link that climbs past any known folder to the link', () => {
    const deep = '../'.repeat(70) + 'diagram.png'
    expect(clickImage(`[![arch](${deep})](${deep})`).opensViewer).toBe(false)
  })

  it('still matches the same absolute path', () => {
    expect(clickImage('[![arch](/home/u/docs/diagram.png)](/home/u/docs/diagram.png)').opensViewer).toBe(true)
  })

  it('keeps the link for every other destination', () => {
    for (const href of ['/home/u/other.png']) {
      expect(clickImage(`[![arch](diagram.png)](${href})\n\n## Details`).opensViewer).toBe(false)
    }
  })

  it('still scrolls to the heading an image links to', () => {
    const { container } = render(
      <BasePathCtx.Provider value="/home/u/docs">
        <MarkdownRenderer content={'[![arch](diagram.png)](#details)\n\n## Details'} />
      </BasePathCtx.Provider>,
    )
    const heading = container.querySelector('h2')!
    let scrolled = false
    heading.scrollIntoView = () => { scrolled = true }
    container.querySelector('img')!.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, button: 0 }))
    expect(scrolled).toBe(true)
  })

  it('still opens the viewer for an image with no link', () => {
    expect(clickImage('![arch](diagram.png)').opensViewer).toBe(true)
  })
})
