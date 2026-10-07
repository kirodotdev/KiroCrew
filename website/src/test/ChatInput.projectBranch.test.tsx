import React from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'
import { api } from '../api/client'
import type { GitBranchList } from '../api/client/files'

// Pins the project-chip branch contract: the active project pill shows
// "<folder> · <branch>" so the session's git context is visible without opening
// a picker, degrades to the folder name alone when there is no branch to show
// (not a repo, git unavailable, path gone), and the branch segment opens the
// branch picker for the session's project folder.

const PROJECT = '/home/u/work/KiroCrew'

const defaultProps = {
  value: '',
  onChange: vi.fn(),
  onSend: vi.fn(),
  onProjectClick: vi.fn(),
  project: PROJECT,
}

const LIST: GitBranchList = {
  repo: true,
  current: 'feat/example',
  local: [
    { name: 'feat/example', date: '2026-10-01T00:00:00Z', author: 'Ada', subject: 'wip', switchable: true, current: true },
    { name: 'main', date: '2026-09-30T00:00:00Z', author: 'Ada', subject: 'release', switchable: true },
  ],
  remote: [],
}

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
})

let originalClipboard: PropertyDescriptor | undefined

const stubClipboard = () => {
  originalClipboard = Object.getOwnPropertyDescriptor(navigator, 'clipboard')
  const writeText = vi.fn().mockResolvedValue(undefined)
  Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
  return writeText
}

afterEach(() => {
  if (originalClipboard) Object.defineProperty(navigator, 'clipboard', originalClipboard)
  else delete (navigator as { clipboard?: unknown }).clipboard
  originalClipboard = undefined
})

const chip = () => screen.getByRole('button', { name: /Project: |Select project/ })
const branchBtn = () => screen.getByTestId('branch-switcher-trigger')

describe('ChatInput project chip branch label', () => {
  it('renders the branch beside the folder name', () => {
    renderWithProviders(<ChatInput {...defaultProps} projectBranch="feat/example" />)
    expect(chip()).toHaveTextContent('KiroCrew')
    expect(branchBtn()).toHaveTextContent('feat/example')
    expect(branchBtn()).toHaveAccessibleName('Switch branch (current: feat/example)')
    expect(chip().getAttribute('title')).toContain('Branch: feat/example')
    expect(chip().getAttribute('title')).toContain(PROJECT)
  })

  it('shows only the folder name when no branch is known', () => {
    renderWithProviders(<ChatInput {...defaultProps} />)
    const btn = chip()
    expect(btn).toHaveTextContent('KiroCrew')
    expect(btn.getAttribute('title')).toBe(`Project: ${PROJECT}`)
    expect(screen.queryByTestId('branch-switcher-trigger')).not.toBeInTheDocument()
    // No separator glyph without a branch to separate.
    expect(btn.textContent).not.toContain('·')
  })

  it('labels a detached HEAD as a commit, not a branch', () => {
    renderWithProviders(<ChatInput {...defaultProps} projectBranch="a1b2c3d" projectDetached />)
    expect(branchBtn()).toHaveTextContent('a1b2c3d')
    expect(chip().getAttribute('title')).toContain('Detached HEAD at a1b2c3d')
    expect(chip().getAttribute('title')).not.toContain('Branch:')
  })

  it('keeps the branch out of the accessible name while a response is running', () => {
    renderWithProviders(<ChatInput {...defaultProps} projectBranch="main" isRunning onStop={vi.fn()} />)
    const btn = screen.getByRole('button', { name: /Stop the current response to switch project/ })
    expect(btn).toBeDisabled()
  })

  it('leaves the branch copyable while a response is running', async () => {
    const writeText = stubClipboard()
    const list = vi.spyOn(api, 'projectGitBranches').mockResolvedValue(LIST)
    renderWithProviders(<ChatInput {...defaultProps} projectBranch="main" isRunning onStop={vi.fn()} />)
    // Switching mid-run is unsafe, since a checkout would change files under the
    // agent; reading the branch name is not. The segment stays a live control
    // that copies, and its tooltip says how to switch.
    expect(branchBtn()).not.toBeDisabled()
    expect(branchBtn()).toHaveAccessibleName('Copy branch name main')
    expect(branchBtn()).toHaveAccessibleDescription('Copy branch name. Stop the current response to switch branch.')
    fireEvent.click(branchBtn())
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('main'))
    await waitFor(() => expect(branchBtn()).toHaveAccessibleName('Copied branch name main'))
    expect(screen.queryByTestId('branch-switcher')).not.toBeInTheDocument()
    expect(list).not.toHaveBeenCalled()
  })

  it('calls a detached HEAD a commit when copying it mid-run', () => {
    renderWithProviders(<ChatInput {...defaultProps} projectBranch="a1b2c3d" projectDetached isRunning onStop={vi.fn()} />)
    expect(screen.getByRole('button', { name: 'Copy commit a1b2c3d' })).toBeInTheDocument()
  })

  it('keeps the message input focused while copying the branch mid-run', async () => {
    // A real pointer click moves focus to the pressed button before `click`
    // fires. The trigger cancels that transfer on mousedown, so a user who is
    // typing a steer can copy the branch and keep typing. `userEvent` replays
    // the full mousedown -> focus -> mouseup -> click sequence; `fireEvent.click`
    // alone would never move focus and could not fail here.
    const user = userEvent.setup()
    const writeText = stubClipboard()
    renderWithProviders(<ChatInput {...defaultProps} projectBranch="feat/example" isRunning onStop={vi.fn()} />)
    const input = screen.getByRole('textbox', { name: 'Message input' })
    input.focus()
    await user.click(branchBtn())
    expect(input).toHaveFocus()
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('feat/example'))
  })

  it('falls back to the full path when the project has no basename', () => {
    renderWithProviders(<ChatInput {...defaultProps} project="/" projectBranch="main" />)
    expect(branchBtn()).toHaveTextContent('main')
  })

  it('does not nest the branch button inside the project button', () => {
    // A <button> inside a <button> is invalid HTML and browsers collapse it, so
    // the two segments must be siblings.
    renderWithProviders(<ChatInput {...defaultProps} projectBranch="feat/example" />)
    expect(chip().querySelector('button')).toBeNull()
    expect(branchBtn().querySelector('button')).toBeNull()
  })
})

describe('ChatInput project chip branch switcher', () => {
  it('opens the branch picker for the session project, above the chip', async () => {
    const list = vi.spyOn(api, 'projectGitBranches').mockResolvedValue(LIST)
    renderWithProviders(<ChatInput {...defaultProps} projectBranch="feat/example" />)
    fireEvent.click(branchBtn())
    const pop = await screen.findByTestId('branch-switcher')
    expect(list).toHaveBeenCalledWith(PROJECT)
    // The composer sits at the bottom of the page, so the picker opens upward.
    expect(pop).toHaveAttribute('data-placement', 'above')
    expect(pop.style.bottom).not.toBe('')
    expect(pop.style.top).toBe('')
    expect(await screen.findByText('main')).toBeInTheDocument()
  })

  it('switches the project folder to the chosen branch', async () => {
    vi.spyOn(api, 'projectGitBranches').mockResolvedValue(LIST)
    const sw = vi.spyOn(api, 'projectGitSwitch').mockResolvedValue({ ok: true, branch: 'main' })
    renderWithProviders(<ChatInput {...defaultProps} projectBranch="feat/example" />)
    fireEvent.click(branchBtn())
    fireEvent.mouseDown(await screen.findByText('main'))
    await waitFor(() => expect(sw).toHaveBeenCalledWith({ path: PROJECT, branch: 'main' }))
    await waitFor(() => expect(screen.queryByTestId('branch-switcher')).not.toBeInTheDocument())
  })

  it('keeps click-to-copy in the picker footer', async () => {
    vi.spyOn(api, 'projectGitBranches').mockResolvedValue(LIST)
    renderWithProviders(<ChatInput {...defaultProps} projectBranch="feat/example" />)
    fireEvent.click(branchBtn())
    expect(await screen.findByRole('button', { name: 'Copy branch name feat/example' })).toBeInTheDocument()
  })

  it('clicking the branch does not open the project picker', () => {
    vi.spyOn(api, 'projectGitBranches').mockResolvedValue(LIST)
    const onProjectClick = vi.fn()
    renderWithProviders(
      <ChatInput {...defaultProps} onProjectClick={onProjectClick} projectBranch="feat/example" />,
    )
    fireEvent.click(branchBtn())
    expect(onProjectClick).not.toHaveBeenCalled()
  })

  it('clicking the folder segment still opens the project picker', () => {
    const onProjectClick = vi.fn()
    renderWithProviders(
      <ChatInput {...defaultProps} onProjectClick={onProjectClick} projectBranch="feat/example" />,
    )
    fireEvent.click(chip())
    expect(onProjectClick).toHaveBeenCalled()
  })

  it('closes the picker when a response starts', async () => {
    vi.spyOn(api, 'projectGitBranches').mockResolvedValue(LIST)
    const { rerender } = renderWithProviders(<ChatInput {...defaultProps} projectBranch="feat/example" />)
    fireEvent.click(branchBtn())
    await screen.findByTestId('branch-switcher')
    rerender(<ChatInput {...defaultProps} projectBranch="feat/example" isRunning onStop={vi.fn()} />)
    await waitFor(() => expect(screen.queryByTestId('branch-switcher')).not.toBeInTheDocument())
  })

  it('does not steal focus from the message input on press', async () => {
    // The trigger cancels mousedown so pressing it does not blur the composer
    // before the picker's own filter input takes focus.
    const user = userEvent.setup()
    vi.spyOn(api, 'projectGitBranches').mockResolvedValue(LIST)
    renderWithProviders(<ChatInput {...defaultProps} projectBranch="feat/example" />)
    const input = screen.getByRole('textbox', { name: 'Message input' })
    input.focus()
    await user.pointer({ keys: '[MouseLeft>]', target: branchBtn() })
    expect(input).toHaveFocus()
    await user.pointer({ keys: '[/MouseLeft]', target: branchBtn() })
    // Once open, the picker's filter owns focus.
    expect(await screen.findByRole('combobox')).toHaveFocus()
  })
})
