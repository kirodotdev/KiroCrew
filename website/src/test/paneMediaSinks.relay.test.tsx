/** Real component byte requests under a capability-prefixed pane. */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent, cleanup, waitFor } from '@testing-library/react'
import { initDashboardRuntime } from '../lib/dashboardRuntime'

// Fresh module → fresh runtime singleton. Pin the pane before any component
// resolves it; the sinks read the runtime live when they render.
initDashboardRuntime({ pathname: '/instance-pane/K_cap01/' })
const PREFIX = '/instance-pane/K_cap01'

import { ImageViewer, PdfViewer } from '../components/FileRenderers'
import { ImgWithFallback } from '../components/markdown/ImgWithFallback'
import { NoteImage } from '../apps/md-notebook/NoteImage'
import WaitingScreen from '../apps/design-critique/WaitingScreen'
import ChatInput from '../components/ChatInput'
import { renderWithProviders } from './helpers'

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('host file viewers under a relayed pane', () => {
  it('ImageViewer loads the raw bytes from the pane origin', () => {
    render(<ImageViewer filePath="/tmp/host.png" />)
    expect(screen.getByRole('img').getAttribute('src')).toBe(
      `${PREFIX}/api/file-raw?path=%2Ftmp%2Fhost.png`,
    )
  })

  it('PdfViewer relocates both byte sinks — the iframe and the new-tab open', () => {
    const opened = vi.spyOn(window, 'open').mockImplementation(() => null)
    const { container } = render(<PdfViewer filePath="/tmp/doc.pdf" />)
    const expected = `${PREFIX}/api/file-raw?path=%2Ftmp%2Fdoc.pdf`
    expect(container.querySelector('iframe')?.getAttribute('src')).toBe(expected)
    fireEvent.click(screen.getByRole('button'))
    expect(opened).toHaveBeenCalledWith(expected, '_blank')
  })
})

describe('markdown / notebook image sinks under a relayed pane', () => {
  it('ImgWithFallback points the <img> and the missing-file probe at the pane origin', async () => {
    // Stub fetch so the fallback's HEAD probe URL is observable.
    const probe = vi.fn().mockResolvedValue({ status: 200 } as Response)
    vi.stubGlobal('fetch', probe)
    render(<ImgWithFallback src="/tmp/shot.png" alt="shot" />)
    const expected = `${PREFIX}/api/file-raw?path=%2Ftmp%2Fshot.png`
    const img = screen.getByAltText('shot')
    expect(img.getAttribute('src')).toBe(expected)

    fireEvent.error(img)
    await waitFor(() =>
      expect(probe).toHaveBeenCalledWith(expected, { method: 'HEAD' }),
    )
  })

  it('NoteImage loads the classified local src from the pane origin', () => {
    render(
      <NoteImage src="/api/file-raw?path=%2Ftmp%2Fn.png" alt="note" rawSrc="n.png" />,
    )
    expect(screen.getByAltText('note').getAttribute('src')).toBe(
      `${PREFIX}/api/file-raw?path=%2Ftmp%2Fn.png`,
    )
  })
})

describe('composer + design-critique live image sinks under a relayed pane', () => {
  it('ChatInput staged-attachment preview loads the thumbnail from the pane origin', () => {
    renderWithProviders(
      <ChatInput value="" onChange={vi.fn()} onSend={vi.fn()} pendingFiles={['/tmp/att.png']} />,
    )
    expect(screen.getByAltText('/tmp/att.png').getAttribute('src')).toBe(
      `${PREFIX}/api/file-raw?path=%2Ftmp%2Fatt.png`,
    )
  })

  it('WaitingScreen loads each waiting shot from the pane origin', () => {
    render(
      <WaitingScreen
        phase="analyzing"
        elapsed={0}
        writing={false}
        reduceMotion
        screens={[{ step: 1, label: 'Home', url: '/api/file-raw?path=%2Ftmp%2Fs.png' }]}
        pendingKind={null}
        onCancel={() => {}}
      />,
    )
    expect(screen.getByAltText('Home').getAttribute('src')).toBe(
      `${PREFIX}/api/file-raw?path=%2Ftmp%2Fs.png`,
    )
  })
})
