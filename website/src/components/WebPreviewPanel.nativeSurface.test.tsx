import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { createRef, useState } from 'react'
import WebPreviewPanelNativeSurface from './WebPreviewPanelNativeSurface'
import type { useWebPreviewPanelAnnotate } from './useWebPreviewPanelAnnotate'
import type { useNativeBrowser } from '../hooks/useNativeBrowser'
import { i18nT } from '../i18n/t'

/* ── The Browser panel's native surface, driven through its own props: the
 *    address bar commits what was typed, and each Annotate control calls the
 *    action `useWebPreviewPanelAnnotate` hands it. ── */

type Annotate = ReturnType<typeof useWebPreviewPanelAnnotate>
type Native = ReturnType<typeof useNativeBrowser>

const pick = (id: number, n: number, note: string) => ({
  id, n, note, ref: `e${id}`, selector: `#t${id}`, role: 'button', name: `Target ${id}`, tag: 'button', text: '', detached: false,
})

function annotateState(overrides: Partial<Annotate> = {}): Annotate {
  return {
    canAnnotate: true,
    annotateOn: true,
    picking: false,
    editing: null,
    annotations: [pick(1, 1, 'first note'), pick(2, 2, 'second note')],
    canSend: true,
    annotateBusy: false,
    annotateError: '',
    setAnnotateError: vi.fn(),
    annotateErrorAskAgent: false,
    addedAck: false,
    clearArmed: false,
    noteInputRef: createRef<HTMLInputElement>(),
    toggleAnnotate: vi.fn(async () => {}),
    removeAnnotation: vi.fn(async () => {}),
    editAnnotation: vi.fn(),
    setEditingText: vi.fn(),
    commitNote: vi.fn(),
    cancelNote: vi.fn(),
    clearAnnotations: vi.fn(),
    sendAnnotations: vi.fn(async () => {}),
    ...overrides,
  } as unknown as Annotate
}

const native = { state: { open: true, url: 'http://localhost:5173/' }, hostRef: vi.fn() } as unknown as Native

function Harness({ annotate, commitNative }: { annotate: Annotate; commitNative: (raw: string) => void }) {
  const [draft, setDraft] = useState('')
  return <WebPreviewPanelNativeSurface native={native} annotate={annotate} draft={draft} setDraft={setDraft}
    commitNative={commitNative} nativeInputRef={createRef<HTMLInputElement>()} />
}

afterEach(cleanup)

describe('WebPreviewPanelNativeSurface', () => {
  it('commits the typed address', () => {
    const commitNative = vi.fn()
    render(<Harness annotate={annotateState()} commitNative={commitNative} />)
    const bar = screen.getByLabelText(i18nT('components.webPreviewPanel.preview_url'))
    fireEvent.change(bar, { target: { value: 'localhost:3000' } })
    fireEvent.submit(bar.closest('form')!)
    expect(commitNative).toHaveBeenCalledWith('localhost:3000')
  })

  it('dismisses an annotate error', () => {
    const annotate = annotateState({ annotateError: 'overlay gone' })
    render(<Harness annotate={annotate} commitNative={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: i18nT('components.errorNotice.dismiss') }))
    expect(annotate.setAnnotateError).toHaveBeenCalledWith('')
  })

  it('wires the annotate controls to their actions', () => {
    const annotate = annotateState()
    render(<Harness annotate={annotate} commitNative={vi.fn()} />)
    fireEvent.click(screen.getByTestId('browser-annotate'))
    expect(annotate.toggleAnnotate).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByTestId('browser-annotations-send'))
    expect(annotate.sendAnnotations).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByRole('button', { name: i18nT('components.webPreviewPanel.annotate_clear') }))
    expect(annotate.clearAnnotations).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByRole('button', { name: i18nT('components.webPreviewPanel.annotate_edit_note_n', { n: 2 }) }))
    expect(annotate.editAnnotation).toHaveBeenCalledWith(2)
    fireEvent.click(screen.getByRole('button', { name: i18nT('components.webPreviewPanel.annotate_remove_n', { n: 1 }) }))
    expect(annotate.removeAnnotation).toHaveBeenCalledWith(1)
  })

  it('saves, cancels and removes from the note editor', () => {
    const annotate = annotateState({ editing: { id: 2, text: 'draft' } })
    render(<Harness annotate={annotate} commitNative={vi.fn()} />)
    const input = screen.getByTestId('browser-annotation-note-input')
    fireEvent.change(input, { target: { value: 'draft two' } })
    expect(annotate.setEditingText).toHaveBeenCalledWith('draft two')
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(annotate.commitNote).toHaveBeenCalledTimes(1)
    fireEvent.keyDown(input, { key: 'Escape' })
    expect(annotate.cancelNote).toHaveBeenCalledTimes(1)
    fireEvent.click(screen.getByText(i18nT('components.webPreviewPanel.annotate_save')))
    expect(annotate.commitNote).toHaveBeenCalledTimes(2)
    const editor = screen.getByTestId('browser-annotation-editing')
    fireEvent.click(editor.querySelector<HTMLButtonElement>(`button[aria-label="${i18nT('components.webPreviewPanel.annotate_remove_n', { n: 2 })}"]`)!)
    expect(annotate.removeAnnotation).toHaveBeenCalledWith(2)
  })
})
