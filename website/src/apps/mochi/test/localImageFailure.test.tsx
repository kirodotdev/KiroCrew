/**
 * A local image that cannot be read must not look like it is still loading.
 *
 * `api.readLocalImage` resolves `null` for a missing file, a path the raw-file
 * route refuses, or any non-2xx answer. `LocalImage` renders a "..." loading
 * placeholder until the read settles, so a failed read has to replace that
 * placeholder with something final, and a read for a path the element no
 * longer shows must never paint over the current one.
 */
import React from 'react'
import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

const { readLocalImage } = vi.hoisted(() => ({ readLocalImage: vi.fn() }))
vi.mock('../src/mochiApi', () => ({ api: { readLocalImage } }))

import { LocalImage } from '../src/renderer/ChatPanelMarkdown'

afterEach(() => { cleanup(); vi.clearAllMocks() })

async function settle() {
  await act(async () => { await Promise.resolve() })
}

describe('mochi LocalImage failed read', () => {
  it('replaces the loading placeholder with an error naming the path when the read returns null', async () => {
    readLocalImage.mockResolvedValue(null)
    render(<LocalImage path="/tmp/gone.png" />)
    await settle()
    expect(readLocalImage).toHaveBeenCalledWith('/tmp/gone.png')
    expect(screen.queryByText('...')).toBeNull()
    expect(screen.getByRole('alert')).toHaveTextContent('/tmp/gone.png')
  })

  it('replaces the loading placeholder with an error naming the path when the read rejects', async () => {
    readLocalImage.mockRejectedValue(new Error('network'))
    render(<LocalImage path="/tmp/boom.png" />)
    await settle()
    expect(screen.queryByText('...')).toBeNull()
    expect(screen.getByRole('alert')).toHaveTextContent('/tmp/boom.png')
  })

  it('drops the previous image when the path changes to one that fails', async () => {
    readLocalImage.mockImplementation(async (p: string) => (p === '/tmp/a.png' ? 'QUFB' : null))
    const { rerender, container } = render(<LocalImage path="/tmp/a.png" />)
    await settle()
    expect(container.querySelector('img')?.getAttribute('src')).toBe('data:image/png;base64,QUFB')
    rerender(<LocalImage path="/tmp/b.png" />)
    await settle()
    expect(container.querySelector('img')).toBeNull()
    expect(screen.getByRole('alert')).toHaveTextContent('/tmp/b.png')
  })

  it('ignores a read that settles after the path has changed', async () => {
    let resolveA: (b64: string | null) => void = () => {}
    readLocalImage.mockImplementation((p: string) => (p === '/tmp/a.png'
      ? new Promise<string | null>((r) => { resolveA = r })
      : Promise.resolve('QkJC')))
    const { rerender, container } = render(<LocalImage path="/tmp/a.png" />)
    rerender(<LocalImage path="/tmp/b.png" />)
    await settle()
    await act(async () => { resolveA('QUFB') })
    expect(container.querySelector('img')?.getAttribute('src')).toBe('data:image/png;base64,QkJC')
  })

  it('reports a failure when no image reader is available', async () => {
    readLocalImage.mockReturnValue(undefined)
    render(<LocalImage path="/tmp/noreader.png" />)
    await settle()
    expect(screen.queryByText('...')).toBeNull()
    expect(screen.getByRole('alert')).toHaveTextContent('/tmp/noreader.png')
  })
})
