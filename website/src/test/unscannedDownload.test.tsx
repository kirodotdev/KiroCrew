import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, act, waitFor, within } from '@testing-library/react'

vi.mock('../utils/download', () => ({ downloadBlob: vi.fn(), downloadUrl: vi.fn() }))

import { downloadBlob, downloadUrl } from '../utils/download'
import { downloadFileToDisk } from '../utils/fileReadUrl'
import UnscannedDownloadConfirmHost, { unscannedDownloadOptions } from '../components/UnscannedDownloadConfirmHost'
import FileDownloadLink from '../components/FileDownloadLink'

const PATH = '/home/me/project/recording.mov'

function answer409() {
  return new Response(
    JSON.stringify({ error: 'over', code: 'large_file_unscanned', size: 3_000_000_000, limit: 52_428_800 }),
    { status: 409, headers: { 'Content-Type': 'application/json' } },
  )
}

describe('a download over the scan limit asks first', () => {
  beforeEach(() => {
    vi.mocked(downloadBlob).mockClear()
    vi.mocked(downloadUrl).mockClear()
    vi.stubGlobal('fetch', vi.fn(async () => answer409()))
  })
  afterEach(() => { vi.unstubAllGlobals() })

  it('shows the dialog with the name and both sizes, and "Download anyway" retries with the flag', async () => {
    render(<UnscannedDownloadConfirmHost />)
    const onError = vi.fn()
    let done: Promise<void> = Promise.resolve()
    act(() => { done = downloadFileToDisk(PATH, onError) })

    const dialog = await screen.findByRole('dialog')
    expect(dialog.textContent).toContain('Too large to scan')
    expect(dialog.textContent).toMatch(/recording\.mov is 3\s*GB\./)
    expect(dialog.textContent).toMatch(/Files over 52\.4\s*MB/)
    expect(dialog.textContent).toContain('not checked for secrets')
    expect(downloadUrl).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: 'Download anyway' }))
    await act(async () => { await done })

    expect(downloadUrl).toHaveBeenCalledTimes(1)
    const [url, name] = vi.mocked(downloadUrl).mock.calls[0]
    expect(url).toBe('/api/file-download?path=' + encodeURIComponent(PATH) + '&confirm_unscanned=1')
    expect(name).toBe('recording.mov')
    expect(downloadBlob).not.toHaveBeenCalled()
    expect(onError).not.toHaveBeenCalled()
  })

  it('Cancel downloads nothing and reports no error', async () => {
    render(<UnscannedDownloadConfirmHost />)
    const onError = vi.fn()
    let done: Promise<void> = Promise.resolve()
    act(() => { done = downloadFileToDisk(PATH, onError) })
    await screen.findByRole('dialog')
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    await act(async () => { await done })
    expect(downloadUrl).not.toHaveBeenCalled()
    expect(onError).not.toHaveBeenCalled()
  })

  it('Escape cancels', async () => {
    render(<UnscannedDownloadConfirmHost />)
    let done: Promise<void> = Promise.resolve()
    act(() => { done = downloadFileToDisk(PATH, vi.fn()) })
    const dialog = await screen.findByRole('dialog')
    fireEvent.keyDown(dialog, { key: 'Escape' })
    await act(async () => { await done })
    expect(downloadUrl).not.toHaveBeenCalled()
  })

  it('a plain click on a Download link takes the same path', async () => {
    render(
      <>
        <UnscannedDownloadConfirmHost />
        <FileDownloadLink filePath={PATH}>Download</FileDownloadLink>
      </>,
    )
    const link = screen.getByRole('link', { name: 'Download' })
    expect(link.getAttribute('href')).toBe('/api/file-download?path=' + encodeURIComponent(PATH))
    fireEvent.click(link)
    await screen.findByRole('dialog')
    fireEvent.click(screen.getByRole('button', { name: 'Download anyway' }))
    await waitFor(() => expect(downloadUrl).toHaveBeenCalledTimes(1))
    expect(vi.mocked(downloadUrl).mock.calls[0][0]).toContain('&confirm_unscanned=1')
  })

  it('the dialog paints above the full-screen preview layer', () => {
    expect(unscannedDownloadOptions({ name: 'a', size: 2, limit: 1 }).layer).toBe('top')
  })

  it('a Windows path saves under its file name, not the whole path', async () => {
    const win = 'C:\\work\\report.docx'
    render(<UnscannedDownloadConfirmHost />)
    let done: Promise<void> = Promise.resolve()
    act(() => { done = downloadFileToDisk(win, vi.fn()) })
    const dialog = await screen.findByRole('dialog')
    expect(dialog.textContent).toMatch(/^Too large to scanreport\.docx is/)
    fireEvent.click(screen.getByRole('button', { name: 'Download anyway' }))
    await act(async () => { await done })
    expect(vi.mocked(downloadUrl).mock.calls[0][1]).toBe('report.docx')

    vi.stubGlobal('fetch', vi.fn(async () => new Response('hello', { status: 200 })))
    await downloadFileToDisk(win, vi.fn())
    expect(vi.mocked(downloadBlob).mock.calls[0][1]).toBe('report.docx')
  })

  it('a failed link download offers the agent hand-off', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ error: 'not found' }), { status: 404 })))
    render(<FileDownloadLink filePath={PATH}>Download</FileDownloadLink>)
    fireEvent.click(screen.getByRole('link', { name: 'Download' }))
    const alert = await screen.findByTestId('file-download-link-error')
    expect(alert.textContent).toContain('Download failed')
    expect(within(alert).getByRole('button', { name: /agent/i })).toBeTruthy()
  })

  it('a small file is fetched and saved as before, with no dialog', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('hello', { status: 200 })))
    render(<UnscannedDownloadConfirmHost />)
    await downloadFileToDisk(PATH, vi.fn())
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(downloadBlob).toHaveBeenCalledTimes(1)
    expect(downloadUrl).not.toHaveBeenCalled()
  })
})
