/**
 * A ```plantuml fence is drawn by the gateway host's own PlantUML when it can be,
 * and is the plain code block it always was when it cannot.
 *
 * The gateway answer is stubbed at `api.plantUmlSvg`: what is pinned is what the
 * fence shows for each answer, that the picture is an <img> (never inline SVG
 * markup), and that one source is asked for once.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen, waitFor } from '@testing-library/react'
import { api } from '../api/client'
import MarkdownRenderer from '../components/MarkdownRenderer'
import { __resetPlantUmlRenders } from '../components/markdown/PlantUmlBlock'
import { namedCeiling } from './namedCeiling'

const FENCE = '```plantuml\n@startuml\nAlice -> Bob: hi\n@enduml\n```'
const SVG = '<svg xmlns="http://www.w3.org/2000/svg"><text>Alice</text></svg>'

describe('PlantUML fences', () => {
  beforeEach(() => __resetPlantUmlRenders())
  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('shows the picture as an image, with the source one disclosure below it', async () => {
    const spy = vi.spyOn(api, 'plantUmlSvg').mockResolvedValue({ svg: SVG, failed: false })
    const { container } = render(<MarkdownRenderer content={FENCE} />)

    const button = await screen.findByTestId('plantuml-enlarge', {}, namedCeiling('PLANTUML_PICTURE', 3000))
    const img = button.querySelector('img')
    expect(img?.getAttribute('src')).toBe(`data:image/svg+xml;charset=utf-8,${encodeURIComponent(SVG)}`)
    // The viewer's download names the file, not the data URL.
    expect(img?.getAttribute('data-lightbox-filename')).toBe('plantuml-diagram.svg')
    // Never inline markup: the diagram's SVG cannot reach the page's DOM.
    expect(container.querySelector('svg text')).toBeNull()
    expect(container.querySelector('details')?.textContent).toContain('Alice -> Bob: hi')
    expect(container.querySelector('details')?.open).toBe(false)
    expect(spy).toHaveBeenCalledWith('@startuml\nAlice -> Bob: hi\n@enduml')
  })

  it.each([
    ['no renderer on the host', { svg: null, failed: false }, false],
    ['a diagram PlantUML cannot draw', { svg: null, failed: true }, true],
  ])('keeps the code block when there is %s', async (_case, answer, notice) => {
    const spy = vi.spyOn(api, 'plantUmlSvg').mockResolvedValue(answer)
    const { container } = render(<MarkdownRenderer content={FENCE} />)

    await waitFor(() => expect(spy).toHaveBeenCalledTimes(1), namedCeiling('PLANTUML_ASKED', 3000))
    // Let the answer land before reading what the fence settled on.
    await act(async () => { await spy.mock.results[0].value })
    expect(screen.queryByTestId('plantuml-render-error') !== null).toBe(notice)
    expect(screen.queryByTestId('plantuml-enlarge')).toBeNull()
    expect(container.textContent).toContain('Alice -> Bob: hi')
  })

  it('keeps an edit started before the picture arrived', async () => {
    // The code block must not remount when the picture lands: its scratch
    // editor holds what the reader typed and nothing else does.
    let release!: () => void
    vi.spyOn(api, 'plantUmlSvg')
      .mockImplementation(() => new Promise(resolve => { release = () => resolve({ svg: SVG, failed: false }) }))
    const { container } = render(<MarkdownRenderer content={FENCE} />)
    await waitFor(() => expect(release).toBeTypeOf('function'), namedCeiling('PLANTUML_PENDING', 3000))

    act(() => {
      const edit = screen.getAllByRole('button', { name: 'Edit code block' })[0]
      edit.focus()
      edit.click()
    })
    expect(screen.getByText('Not saved back to the chat. Closing discards edits.')).toBeTruthy()
    act(() => { screen.getByRole('button', { name: 'Close editor' }).focus() })
    await act(async () => { release() })

    await screen.findByTestId('plantuml-enlarge', {}, namedCeiling('PLANTUML_AFTER_EDIT', 3000))
    expect(screen.getByText('Not saved back to the chat. Closing discards edits.')).toBeTruthy()
    // The reader is still in the source: it stays open under the picture.
    expect(container.querySelector('details')?.open).toBe(true)
  })

  it('skips a queued source no block shows any more', async () => {
    // A fence still streaming, or a preview being typed into, changes its source
    // faster than PlantUML draws: only the first and the latest are asked for.
    let release!: () => void
    const spy = vi.spyOn(api, 'plantUmlSvg')
      .mockImplementationOnce(() => new Promise(resolve => { release = () => resolve({ svg: SVG, failed: false }) }))
      .mockResolvedValue({ svg: SVG, failed: false })
    const fence = (body: string) => '```plantuml\n' + body + '\n```'
    const { rerender } = render(<MarkdownRenderer content={fence('A -> B')} />)
    await waitFor(() => expect(spy).toHaveBeenCalledTimes(1), namedCeiling('PLANTUML_FIRST', 3000))
    rerender(<MarkdownRenderer content={fence('A -> B: one')} />)
    rerender(<MarkdownRenderer content={fence('A -> B: two')} />)
    await act(async () => { release() })

    await screen.findByTestId('plantuml-enlarge', {}, namedCeiling('PLANTUML_LATEST', 3000))
    expect(spy.mock.calls.map(call => call[0])).toEqual(['A -> B', 'A -> B: two'])
  })

  it('asks again after a request that failed', async () => {
    const spy = vi.spyOn(api, 'plantUmlSvg')
      .mockRejectedValueOnce(new Error('connection dropped'))
      .mockResolvedValue({ svg: SVG, failed: false })
    const first = render(<MarkdownRenderer content={FENCE} />)
    await screen.findByTestId('plantuml-render-error', {}, namedCeiling('PLANTUML_DROPPED', 3000))
    first.unmount()

    render(<MarkdownRenderer content={FENCE} />)
    await screen.findByTestId('plantuml-enlarge', {}, namedCeiling('PLANTUML_RETRIED', 3000))
    expect(spy).toHaveBeenCalledTimes(2)
  })

  it('asks the gateway once for a source rendered twice', async () => {
    const spy = vi.spyOn(api, 'plantUmlSvg').mockResolvedValue({ svg: SVG, failed: false })
    render(<MarkdownRenderer content={`${FENCE}\n\n${FENCE}`} />)

    await waitFor(() => expect(screen.getAllByTestId('plantuml-enlarge')).toHaveLength(2), namedCeiling('PLANTUML_BOTH', 3000))
    expect(spy).toHaveBeenCalledTimes(1)
  })
})

describe('api.plantUmlSvg', () => {
  afterEach(() => vi.restoreAllMocks())
  const answer = (status: number, body: object, headers: Record<string, string> = {}) =>
    vi.spyOn(globalThis, 'fetch').mockImplementation(async () => new Response(JSON.stringify(body), {
      status, headers: { 'Content-Type': 'application/json', ...headers },
    }))

  it.each([
    [200, { svg: SVG }, { svg: SVG, failed: false }],
    [404, { code: 'plantuml_unavailable' }, { svg: null, failed: false }],
    [403, { code: 'owner_only' }, { svg: null, failed: false }],
    [422, { code: 'plantuml_failed' }, { svg: null, failed: true }],
  ])('maps %i to the fence view', async (status, body, expected) => {
    answer(status, body)
    await expect(api.plantUmlSvg('A -> B')).resolves.toEqual(expected)
  })

  it('does not take a lapsed session for a designed answer', async () => {
    // The 403 the auth middleware sends for an expired cookie goes through the
    // recovering parser and rejects, so the block keeps no answer for it.
    answer(403, { error: 'auth required' }, { 'X-Auth-Required': 'true' })
    await expect(api.plantUmlSvg('A -> B')).rejects.toThrow()
  })
})
