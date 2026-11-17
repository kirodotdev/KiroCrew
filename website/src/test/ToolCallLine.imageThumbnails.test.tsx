/**
 * An image-mode read shows a thumbnail of every image it read, on the folded
 * row, so the reader sees which picture the agent looked at without opening
 * the Input panel.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { act, fireEvent, screen } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import ToolCallLine from '../pages/chat/ToolCallLine'
import chatReducer, { sseChatMessagePatchByTs } from '../store/chatSlice'
import type { RootState } from '../store'
import type { ChatMessage } from '../types'

type ChatState = RootState['chat']

const SLOT = 'slot-1'
const TC = 'tc_img'

if (typeof globalThis.ResizeObserver === 'undefined') {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver
}

beforeEach(() => { localStorage.clear() })

const imageInput = (paths: string[]) => JSON.stringify({ operations: [{ mode: 'Image', image_paths: paths }] })

/** A replayed tool row: no toolLog entry, persisted meta only. */
function row(input: string): ChatMessage {
  return {
    role: 'tool',
    content: '🔧 Reading image',
    cls: 'msg msg-tool',
    meta: { tool_call_id: TC, input, output: 'ok', kind: 'read' },
  }
}

/** Render, then let the providers' mount-time effects settle, so no state
 *  update lands after the test's last barrier. */
async function render(msg: ChatMessage, chat: Partial<ChatState> = {}) {
  const store = createTestStore({
    chat: { messages: [msg], toolLog: [], slotRunning: false, activeSlot: SLOT, ...chat } as unknown as ChatState,
  })
  const out = renderWithProviders(<ToolCallLine message={msg} running={false} />, { store })
  await act(async () => {})
  return out
}

describe('ToolCallLine image-read thumbnails', () => {
  it('draws one thumbnail per image on the folded row, inside one control', async () => {
    await render(row(imageInput(['/shots/a.png', '/shots/b.png'])))
    const strip = screen.getByTestId('tool-image-thumbnails')
    expect(strip.tagName).toBe('BUTTON')
    const imgs = screen.getAllByTestId('tool-image-thumb') as HTMLImageElement[]
    expect(imgs).toHaveLength(2)
    imgs.forEach(img => expect(strip.contains(img)).toBe(true))
    expect(imgs[0].getAttribute('src')).toBe(`/api/file-raw?path=${encodeURIComponent('/shots/a.png')}&v=${TC}`)
    expect(imgs[1].getAttribute('alt')).toBe('/shots/b.png')
    // Folded: the details panel is not open, the thumbnails show anyway.
    expect(screen.getByRole('button', { name: /show details/i }).getAttribute('aria-expanded')).toBe('false')
  })

  it('opens the viewer at the clicked image with the row images as one set, without toggling the row', async () => {
    await render(row(imageInput(['/shots/a.png', '/shots/b.png'])))
    const spy = vi.fn()
    window.addEventListener('lightbox', spy)
    try {
      fireEvent.click(screen.getAllByTestId('tool-image-thumb')[1])
      expect(spy).toHaveBeenCalledTimes(1)
      const detail = (spy.mock.calls[0][0] as CustomEvent).detail as { images: { alt: string }[]; index: number }
      expect(detail.images.map(i => i.alt)).toEqual(['/shots/a.png', '/shots/b.png'])
      expect(detail.index).toBe(1)
    } finally {
      window.removeEventListener('lightbox', spy)
    }
    expect(screen.getByRole('button', { name: /show details/i }).getAttribute('aria-expanded')).toBe('false')
  })

  it('opens the first image when the control is activated off a thumbnail', async () => {
    await render(row(imageInput(['/shots/a.png', '/shots/b.png'])))
    const spy = vi.fn()
    window.addEventListener('lightbox', spy)
    try {
      fireEvent.click(screen.getByTestId('tool-image-thumbnails'))
      const detail = (spy.mock.calls[0][0] as CustomEvent).detail as { index: number }
      expect(detail.index).toBe(0)
    } finally {
      window.removeEventListener('lightbox', spy)
    }
  })

  it('keeps a failed image as a same-size tile and names it in an error notice', async () => {
    await render(row(imageInput(['/shots/ok.png', '/gone/missing.png'])))
    const imgs = screen.getAllByTestId('tool-image-thumb') as HTMLImageElement[]
    fireEvent.error(imgs[1])
    expect(screen.getAllByTestId('tool-image-thumb')).toHaveLength(1)
    expect(screen.getByTestId('tool-image-thumb-missing').getAttribute('title')).toBe('/gone/missing.png')
    expect(screen.getByTestId('tool-image-thumb-error').textContent).toContain('missing.png')
    expect((screen.getByTestId('tool-image-thumbnails') as HTMLButtonElement).disabled).toBe(false)
  })

  it('disables the control when no image loaded', async () => {
    await render(row(imageInput(['/gone/missing.png'])))
    fireEvent.error(screen.getByTestId('tool-image-thumb'))
    expect((screen.getByTestId('tool-image-thumbnails') as HTMLButtonElement).disabled).toBe(true)
  })

  it('draws nothing for a read that is not an image read', async () => {
    await render(row(JSON.stringify({ operations: [{ mode: 'Line', path: '/notes.md' }] })))
    expect(screen.queryByTestId('tool-image-thumbnails')).toBeNull()
  })

  it('draws nothing for a rejected image read', async () => {
    const msg = row(imageInput(['/shots/a.png']))
    const perm: ChatMessage = {
      role: 'permission',
      content: 'Reading image',
      cls: '',
      meta: { tool_call_id: TC, resolved: 'rejected' },
    }
    await render(msg, { messages: [msg, perm] } as unknown as Partial<ChatState>)
    expect(screen.queryByTestId('tool-image-thumbnails')).toBeNull()
  })
})

/** Claude Code's Read row: the backend's mark names the argument its file is in. */
function claudeRow(filePath: string, mark: string | null = 'file_path'): ChatMessage {
  const meta: Record<string, unknown> = {
    tool_call_id: TC,
    input: JSON.stringify({ file_path: filePath }, null, 2),
    output: '',
    kind: 'read',
  }
  if (mark !== null) meta.read_path_arg = mark
  return { role: 'tool', content: `🔧 Read ${filePath}`, cls: 'msg msg-tool', meta }
}

describe('ToolCallLine thumbnails for a backend-marked read', () => {
  it('draws the Claude Code Read image through the same file-raw URL an image-mode read uses', async () => {
    await render(claudeRow('/work/website/public/icon-192.png'))
    const [img] = screen.getAllByTestId('tool-image-thumb') as HTMLImageElement[]
    expect(img.getAttribute('src')).toBe(`/api/file-raw?path=${encodeURIComponent('/work/website/public/icon-192.png')}&v=${TC}`)
    expect(img.getAttribute('alt')).toBe('/work/website/public/icon-192.png')
    expect(screen.getByRole('button', { name: /show details/i }).getAttribute('aria-expanded')).toBe('false')
  })

  it('draws the live row from its tool-log entry input', async () => {
    const msg = claudeRow('/work/a.png')
    const entry = { type: 'tool', text: 'Read /work/a.png', tool_call_id: TC, input: msg.meta?.input as string, kind: 'read', ts: 1 }
    await render(msg, { toolLog: [entry], slotRunning: true } as unknown as Partial<ChatState>)
    expect((screen.getAllByTestId('tool-image-thumb') as HTMLImageElement[]).map(i => i.alt)).toEqual(['/work/a.png'])
  })

  it('draws nothing for the same input on an unmarked row', async () => {
    await render(claudeRow('/work/a.png', null))
    expect(screen.queryByTestId('tool-image-thumbnails')).toBeNull()
  })

  it('draws nothing for a marked read of a file that is not an image', async () => {
    await render(claudeRow('/work/notes.md'))
    expect(screen.queryByTestId('tool-image-thumbnails')).toBeNull()
  })

  it('draws nothing for a marked read of a credential-directory image', async () => {
    await render(claudeRow('/home/me/.ssh/key.png'))
    expect(screen.queryByTestId('tool-image-thumbnails')).toBeNull()
  })

  it('draws nothing for a rejected marked read', async () => {
    const msg = claudeRow('/work/a.png')
    const perm: ChatMessage = {
      role: 'permission',
      content: 'Read /work/a.png',
      cls: '',
      meta: { tool_call_id: TC, resolved: 'rejected' },
    }
    await render(msg, { messages: [msg, perm] } as unknown as Partial<ChatState>)
    expect(screen.queryByTestId('tool-image-thumbnails')).toBeNull()
  })

  it('draws nothing once the backend withdraws the mark of a read that failed', async () => {
    const msg: ChatMessage = { ...claudeRow('/work/a.png'), ts: 't-read' }
    const before = { messages: [msg], activeSlot: SLOT, slotMessages: {} } as unknown as ChatState
    // The gateway's patch for a Read that ended failed (`_withdraw_read_path_arg`).
    const after = chatReducer(before, sseChatMessagePatchByTs({ slot: SLOT, ts: 't-read', meta: { read_path_arg: '' } }))
    const [patched] = after.messages
    expect(patched.meta?.read_path_arg).toBe('')
    await render(patched)
    expect(screen.queryByTestId('tool-image-thumbnails')).toBeNull()
  })
})
