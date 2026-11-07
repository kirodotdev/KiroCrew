/**
 * #30416: dropping a folder whose resolved path can't be written as a composer
 * folder token (a space or `@`, or a filesystem root) onto the ChatPane
 * composer surfaces the existing folder-refused notice through the validation-
 * HINT surface (not the danger error banner), and uploads nothing. Guards the
 * sibling of the chat-page controller fix so the shared classifier's
 * refusedDirs bucket is not silently dropped on this surface, and that the
 * hint survives a mixed file+folder drop.
 */
import { createElement, type ReactNode } from 'react'
import { renderHook, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { describe, it, expect, vi, afterEach } from 'vitest'
import { api } from '../api/client'
import { i18nT } from '../i18n/t'
import { useChatPaneUploads } from '../components/useChatPaneUploads'

afterEach(() => {
  vi.restoreAllMocks()
  const w = window as { kirocrew?: unknown }
  delete w.kirocrew
})

const ref = <T,>(current: T) => ({ current })

/** A drop payload: a directory item whose resolved path is `dirPath`, plus the
 *  given regular `files`, for mixed drops. The bridge resolves each item to the
 *  path its File name carries. */
function dirDrop(dirPath: string, files: File[] = []): DataTransfer {
  const w = window as { kirocrew?: { getPathForFile?: (f: File) => string } }
  w.kirocrew = { getPathForFile: f => f.name }
  const items: unknown[] = [
    { kind: 'file', webkitGetAsEntry: () => ({ isDirectory: true }), getAsFile: () => new File(['x'], dirPath, { type: '' }) },
  ]
  for (const f of files) {
    items.push({ kind: 'file', webkitGetAsEntry: () => ({ isDirectory: false }), getAsFile: () => f })
  }
  return { items, files: [] } as unknown as DataTransfer
}
const plainFile = (name = 'a.txt') => new File(['x'], name, { type: 'text/plain' })

function render(setInput: (v: unknown) => void) {
  const dispatch = vi.fn() as never
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client: queryClient }, children)
  return renderHook(() => useChatPaneUploads({
    slotKey: 'slot', dispatch, slotKeyRef: ref('slot'), mountedRef: ref(true),
    setInput: setInput as never, dashCfg: { upload_max_mb: 50 }, dashCfgError: undefined,
  }), { wrapper })
}

function dropEvent(result: ReturnType<typeof render>['result'], dataTransfer: DataTransfer) {
  const event = { dataTransfer, preventDefault: vi.fn(), stopPropagation: vi.fn() } as unknown as Parameters<typeof result.current.dropTargetProps.onDrop>[0]
  act(() => { result.current.dropTargetProps.onDrop(event) })
}

describe('ChatPane drop of a space-named folder (#30416)', () => {
  it('shows the folder-refused hint (not the danger error banner) and uploads nothing', () => {
    const uploadSpy = vi.spyOn(api, 'uploadFiles')
    const setInput = vi.fn()
    const { result } = render(setInput)
    dropEvent(result, dirDrop('/Users/me/p251 folder'))
    // The refusal is a client-side validation refusal (nothing sent): it goes
    // to the hint surface, leaving the danger error banner empty.
    expect(result.current.uploadHint).toBe(i18nT('components.chatInput.tree_drop_folder_refused'))
    expect(result.current.uploadError).toBe('')
    expect(uploadSpy).not.toHaveBeenCalled()
    expect(setInput).not.toHaveBeenCalled()
  })

  it('keeps the folder-refused hint on a mixed drop when the files upload', () => {
    // uploadFiles runs setUploadHint('') synchronously before it mutates and
    // returns false (not refused locally); the handler sets the refused hint
    // after it, so a mixed file+folder drop uploads the file and still shows
    // the folder refusal rather than the cleared empty hint.
    vi.spyOn(api, 'uploadFiles').mockResolvedValue({ paths: ['/tmp/a.txt'] } as never)
    const setInput = vi.fn()
    const { result } = render(setInput)
    dropEvent(result, dirDrop('/Users/me/p251 folder', [plainFile()]))
    expect(setInput).not.toHaveBeenCalled()
    expect(result.current.uploadHint).toBe(i18nT('components.chatInput.tree_drop_folder_refused'))
  })

  it('clears a stale upload-error banner on a refused-folder-only drop', () => {
    // A folder-only drop skips uploadFiles (which clears the error otherwise),
    // so a prior upload-failure banner must be cleared by the handler itself.
    vi.spyOn(api, 'uploadFiles')
    const setInput = vi.fn()
    const { result } = render(setInput)
    act(() => { result.current.setUploadError('a previous upload failed') })
    expect(result.current.uploadError).toBe('a previous upload failed')
    dropEvent(result, dirDrop('/Users/me/p251 folder'))
    expect(result.current.uploadError).toBe('')
    expect(result.current.uploadHint).toBe(i18nT('components.chatInput.tree_drop_folder_refused'))
  })

  it('does NOT overwrite the files-refused hint with the folder hint on a mixed drop (#30416)', () => {
    // Files refused by the local >20 pre-check: uploadFiles sets its own hint
    // and returns true, so the folder hint must NOT replace it -- otherwise the
    // user is never told the files were refused too.
    const uploadSpy = vi.spyOn(api, 'uploadFiles')
    const setInput = vi.fn()
    const { result } = render(setInput)
    const manyFiles = Array.from({ length: 21 }, (_, i) => plainFile(`f${i}.txt`))
    dropEvent(result, dirDrop('/Users/me/p251 folder', manyFiles))
    // The files-refused hint stays; the folder hint did not overwrite it.
    expect(result.current.uploadHint).toBe(i18nT('pages.chatPage.too_many_files_max_20'))
    // Local pre-check refused the files: nothing was sent.
    expect(uploadSpy).not.toHaveBeenCalled()
  })
})
