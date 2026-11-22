import { afterEach, describe, expect, it, vi } from 'vitest'
import { noteTerminalRun, terminalRunNotePath } from '../utils/terminalRunNote'

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('noteTerminalRun', () => {
  it('posts the outcome to the slot route with the slot key encoded', () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response('{}'))
    vi.stubGlobal('fetch', fetchMock)
    noteTerminalRun('member a/b', { command: 'ls', outcome: 'failed', reason: 'no_tab', source: 'code_block' })
    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe(terminalRunNotePath('member a/b'))
    expect(url).toBe('/api/chat/slots/member%20a%2Fb/terminal-run')
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body)).toEqual({ command: 'ls', outcome: 'failed', reason: 'no_tab', source: 'code_block' })
  })

  it('sends an empty reason when none is given', () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response('{}'))
    vi.stubGlobal('fetch', fetchMock)
    noteTerminalRun('member-atlas', { command: 'ls', outcome: 'ran', source: 'run_yourself' })
    expect(JSON.parse(fetchMock.mock.calls[0][1].body).reason).toBe('')
  })

  it('sends nothing without a slot or a command', () => {
    const fetchMock = vi.fn()
    vi.stubGlobal('fetch', fetchMock)
    noteTerminalRun('', { command: 'ls', outcome: 'ran', source: 'code_block' })
    noteTerminalRun('member-atlas', { command: '', outcome: 'ran', source: 'code_block' })
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('swallows a rejected request and a missing fetch', async () => {
    const fetchMock = vi.fn().mockRejectedValue(new Error('offline'))
    vi.stubGlobal('fetch', fetchMock)
    expect(() => noteTerminalRun('member-atlas', { command: 'ls', outcome: 'ran', source: 'code_block' })).not.toThrow()
    await Promise.resolve()
    vi.stubGlobal('fetch', () => { throw new Error('no fetch') })
    expect(() => noteTerminalRun('member-atlas', { command: 'ls', outcome: 'ran', source: 'code_block' })).not.toThrow()
  })
})
