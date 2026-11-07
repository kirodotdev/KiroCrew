/**
 * A Windows drive-path image in a Mochi chat bubble renders through LocalImage.
 *
 * On a Windows gateway an agent names screenshots and uploads by native path
 * (`C:\…`), and a user may paste one. The bubble's image checks used to accept
 * only `/`-rooted paths, so a drive path stayed text, and `C:/Users/…` was read
 * as `/Users/…` with the drive dropped (issue #27797). UNC paths stay refused.
 */
import React from 'react'
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

// A read that never settles: these tests assert which path is read, and no
// state update lands after the test ends.
const { readLocalImage } = vi.hoisted(() => ({ readLocalImage: vi.fn((_path: string) => new Promise<string | null>(() => {})) }))
vi.mock('../src/mochiApi', () => ({ api: { readLocalImage } }))

import { Bubble } from '../src/renderer/ChatPanel'
import { WINDOWS_ABS_PATH_RE } from '../../../utils/urlTransform'

afterEach(() => { cleanup(); vi.clearAllMocks() })

function renderBubble(role: 'user' | 'assistant', content: string) {
  return render(<Bubble animate={false} message={{ id: 'm1', role, content, timestamp: 1 }} />)
}

describe('mochi Bubble Windows drive-path images', () => {
  it('reads a user line holding a backslash drive path as an image', () => {
    renderBubble('user', 'C:\\u\\shot.png')
    expect(readLocalImage).toHaveBeenCalledWith('C:\\u\\shot.png')
  })

  it('reads a drive path in an assistant markdown image', () => {
    renderBubble('assistant', 'Here it is ![shot](C:\\work\\shot.png)')
    expect(readLocalImage).toHaveBeenCalledWith('C:\\work\\shot.png')
  })

  it('keeps the drive of a bare forward-slash drive path in assistant text', () => {
    renderBubble('assistant', 'Saved to C:/Users/u/shot.png')
    expect(readLocalImage).toHaveBeenCalledTimes(1)
    expect(readLocalImage).toHaveBeenCalledWith('C:/Users/u/shot.png')
  })

  it('does not read the scheme letter of a URL as a drive', () => {
    renderBubble('assistant', 'see https://example.com/a.png')
    const reads = readLocalImage.mock.calls.map(([p]) => p)
    expect(reads.filter((p) => WINDOWS_ABS_PATH_RE.test(p))).toEqual([])
  })

  it('leaves a UNC user line as text', () => {
    renderBubble('user', '\\\\host\\share\\a.png')
    expect(readLocalImage).not.toHaveBeenCalled()
    expect(screen.getByText('\\\\host\\share\\a.png')).toBeInTheDocument()
  })
})
