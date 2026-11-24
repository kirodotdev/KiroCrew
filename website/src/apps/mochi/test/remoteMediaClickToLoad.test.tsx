/**
 * Remote raw-HTML video, audio and source in a Mochi chat bubble wait for a
 * click, the same way remote images do (remoteImageClickToLoad.test.tsx).
 *
 * The bubble map now routes these tags through the main chat's handlers
 * (DeferredMedia, MdSourceEl), so a remote media URL gets the same
 * click-to-load chip, and local media mounts directly as in the main chat.
 */
import React from 'react'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const { readLocalImage } = vi.hoisted(() => ({ readLocalImage: vi.fn((_path: string) => new Promise<string | null>(() => {})) }))
vi.mock('../src/mochiApi', () => ({ api: { readLocalImage } }))

import { Bubble } from '../src/renderer/ChatPanel'

afterEach(() => { cleanup(); vi.clearAllMocks() })

function renderAssistant(html: string) {
  // Visible text alongside the media: the bubble skips a message with none.
  const content = `look ${html}`
  return render(<Bubble animate={false} message={{ id: 'm1', role: 'assistant', content, timestamp: 1 }} />)
}

/** Every src / poster / srcset value in the rendered bubble that names the remote host. */
function remoteRefs(container: HTMLElement): string[] {
  const refs: string[] = []
  for (const el of Array.from(container.querySelectorAll('*'))) {
    for (const attr of ['src', 'poster', 'srcset']) {
      const value = el.getAttribute(attr)
      if (value && value.includes('media.example')) refs.push(`${el.tagName.toLowerCase()}[${attr}]`)
    }
  }
  return refs
}

describe('mochi Bubble remote raw-HTML media', () => {
  it.each([
    ['video poster', '<video controls poster="https://media.example/frame"></video>', 'video', /external video/i],
    ['video src', '<video controls src="https://media.example/clip"></video>', 'video', /external video/i],
    ['video child source', '<video controls><source src="https://media.example/clip" type="video/mp4"></video>', 'video', /external video/i],
    ['audio src', '<audio controls src="https://media.example/track"></audio>', 'audio', /external audio/i],
  ])('a remote %s makes no remote reference until the reader clicks', (_case, html, tag, name) => {
    const { container } = renderAssistant(html)
    expect(container.querySelector(tag)).toBeNull()
    expect(remoteRefs(container)).toEqual([])
    fireEvent.click(screen.getByRole('button', { name }))
    expect(container.querySelector(tag)).not.toBeNull()
    expect(remoteRefs(container).length).toBeGreaterThan(0)
  })

  // No image extension in these URLs: the bubble lifts `/….png`-style paths
  // out of the text before markdown runs.
  it('drops a remote <picture> source, as the main chat does', () => {
    const { container } = renderAssistant(
      '<picture><source srcset="https://media.example/p?f=webp 1x" type="image/webp"><img src="/api/file-raw?path=ok" alt="pic"></picture>',
    )
    expect(container.querySelector('source')).toBeNull()
    expect(remoteRefs(container)).toEqual([])
  })

  it('mounts a local-bytes video directly, with no button', () => {
    const { container } = renderAssistant('<video controls src="/api/file-raw?path=clip.mp4"></video>')
    expect(container.querySelector('video')?.getAttribute('src')).toBe('/api/file-raw?path=clip.mp4')
    expect(screen.queryByRole('button', { name: /click to load/i })).toBeNull()
  })

  it('refuses a gateway-route media src with no button, as the main chat does', () => {
    const { container } = renderAssistant('<video controls src="/api/link-meta?url=https://media.example/v.mp4"></video>')
    expect(container.querySelector('video')).toBeNull()
    expect(screen.queryByRole('button', { name: /click to load/i })).toBeNull()
    expect(screen.getByTestId('remote-media-gateway-refused')).not.toBeNull()
  })
})
