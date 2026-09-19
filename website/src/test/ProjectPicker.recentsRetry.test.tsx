import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ProjectPicker from '../components/ProjectPicker'
import { api } from '../api/client'

const NOTICE = 'Recent projects unavailable'
const RETRY = 'Retry: Recent projects unavailable'

const rect = (top: number, left: number, width = 80, height = 24): DOMRect => ({
  top, left, width, height,
  bottom: top + height,
  right: left + width,
  x: left, y: top,
  toJSON: () => ({}),
} as DOMRect)

const open = () => renderWithProviders(
  <ProjectPicker open={true} onOpenChange={vi.fn()} anchorRect={rect(100, 50)} onSelect={vi.fn()} />
)

beforeEach(() => {
  vi.spyOn(api, 'browseDirs').mockResolvedValue({ path: '/home/u', parent: '/home', dirs: [] })
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('ProjectPicker recents failure', () => {
  it('names a failed recents read instead of only falling back to Browse', async () => {
    vi.spyOn(api, 'recentProjects').mockRejectedValue(new Error('gateway down'))
    open()
    expect(await screen.findByText(NOTICE)).toBeInTheDocument()
    expect(screen.getByLabelText(RETRY)).toBeInTheDocument()
  })

  it('says nothing when the read succeeds', async () => {
    vi.spyOn(api, 'recentProjects').mockResolvedValue({ dirs: ['/home/u/projA'] })
    open()
    expect(await screen.findByText('/home/u/projA')).toBeInTheDocument()
    expect(screen.queryByText(NOTICE)).not.toBeInTheDocument()
  })

  it('clears the notice and shows the rows once Retry recovers the read', async () => {
    const recents = vi.spyOn(api, 'recentProjects').mockRejectedValue(new Error('gateway down'))
    open()
    await screen.findByText(NOTICE)

    recents.mockResolvedValue({ dirs: ['/home/u/projA'] })
    fireEvent.click(screen.getByLabelText(RETRY))

    expect(await screen.findByText('/home/u/projA')).toBeInTheDocument()
    await waitFor(() => expect(screen.queryByText(NOTICE)).not.toBeInTheDocument())
    expect(recents).toHaveBeenCalledTimes(2)
  })

  it('keeps the notice and the control when the retry fails again', async () => {
    const recents = vi.spyOn(api, 'recentProjects').mockRejectedValue(new Error('gateway down'))
    open()
    await screen.findByText(NOTICE)

    fireEvent.click(screen.getByLabelText(RETRY))

    await waitFor(() => expect(recents).toHaveBeenCalledTimes(2))
    expect(screen.getByText(NOTICE)).toBeInTheDocument()
    await waitFor(() => expect(screen.getByLabelText(RETRY)).not.toBeDisabled())
  })

  it('reaches the user on Browse, which the failure itself selected', async () => {
    vi.spyOn(api, 'recentProjects').mockRejectedValue(new Error('gateway down'))
    open()
    await screen.findByText(NOTICE)
    expect(screen.getByText('Browse').className).toContain('border-accent')
  })

  it('stops following the user once she chooses Browse deliberately', async () => {
    vi.spyOn(api, 'recentProjects').mockRejectedValue(new Error('gateway down'))
    open()
    await screen.findByText(NOTICE)

    fireEvent.mouseDown(screen.getByText('Browse'))

    await waitFor(() => expect(screen.queryByText(NOTICE)).not.toBeInTheDocument())
  })

  it('still names the failure on the Recent tab she opens to look for it', async () => {
    vi.spyOn(api, 'recentProjects').mockRejectedValue(new Error('gateway down'))
    open()
    await screen.findByText(NOTICE)

    fireEvent.mouseDown(screen.getByText('Browse'))
    await waitFor(() => expect(screen.queryByText(NOTICE)).not.toBeInTheDocument())
    fireEvent.mouseDown(screen.getByText('Recent'))

    expect(await screen.findByText(NOTICE)).toBeInTheDocument()
  })
})
