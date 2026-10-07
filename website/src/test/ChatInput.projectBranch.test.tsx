import React from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'

// Pins the project-chip branch contract: the active project button
// shows "<folder> · <branch>" so the session's git context is visible without
// opening a picker, and degrades to the folder name alone when there is no
// branch to show (not a repo, git unavailable, path gone).

const defaultProps = {
  value: '',
  onChange: vi.fn(),
  onSend: vi.fn(),
  onProjectClick: vi.fn(),
  project: '/home/u/work/KiroCrew',
}

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
})

const chip = () => screen.getByRole('button', { name: /Project: |Select project/ })
const branchBtn = () => screen.getByRole('button', { name: /Cop(y|ied) (branch name|commit) / })

describe('ChatInput project chip branch label', () => {
  it('renders the branch beside the folder name', () => {
    renderWithProviders(<ChatInput {...defaultProps} projectBranch="feat/example" />)
    expect(chip()).toHaveTextContent('KiroCrew')
    expect(branchBtn()).toHaveTextContent('feat/example')
    expect(chip().getAttribute('title')).toContain('Branch: feat/example')
    expect(chip().getAttribute('title')).toContain('/home/u/work/KiroCrew')
  })

  it('shows only the folder name when no branch is known', () => {
    renderWithProviders(<ChatInput {...defaultProps} />)
    const btn = chip()
    expect(btn).toHaveTextContent('KiroCrew')
    expect(btn.getAttribute('title')).toBe('Project: /home/u/work/KiroCrew')
    expect(screen.queryByRole('button', { name: /Copy branch name/ })).not.toBeInTheDocument()
    // No separator glyph without a branch to separate.
    expect(btn.textContent).not.toContain('·')
  })

  it('labels a detached HEAD as a commit, not a branch', () => {
    renderWithProviders(<ChatInput {...defaultProps} projectBranch="a1b2c3d" projectDetached />)
    expect(branchBtn()).toHaveTextContent('a1b2c3d')
    expect(chip().getAttribute('title')).toContain('Detached HEAD at a1b2c3d')
    expect(chip().getAttribute('title')).not.toContain('Branch:')
    // The copy affordance calls it a commit, not a branch.
    expect(screen.getByRole('button', { name: 'Copy commit a1b2c3d' })).toBeInTheDocument()
  })

  it('names the same project and branch while a response is running, plus when a pick lands', () => {
    renderWithProviders(<ChatInput {...defaultProps} projectBranch="main" isRunning onStop={vi.fn()} />)
    const btn = chip()
    expect(btn).toBeEnabled()
    expect(btn).toHaveAccessibleName(/Branch: main/)
    expect(btn).toHaveAccessibleName(/Changes apply from the next response\./)
  })

  it('leaves the branch copyable while a response is running', () => {
    renderWithProviders(<ChatInput {...defaultProps} projectBranch="main" isRunning onStop={vi.fn()} />)
    // Switching project mid-run is unsafe; reading the branch name is not.
    expect(branchBtn()).not.toBeDisabled()
  })

  it('falls back to the full path when the project has no basename', () => {
    renderWithProviders(<ChatInput {...defaultProps} project="/" projectBranch="main" />)
    expect(branchBtn()).toHaveTextContent('main')
  })

  it('does not nest the copy button inside the picker button', () => {
    // A <button> inside a <button> is invalid HTML and browsers collapse it, so
    // the two segments must be siblings.
    renderWithProviders(<ChatInput {...defaultProps} projectBranch="feat/example" />)
    expect(chip().querySelector('button')).toBeNull()
    expect(branchBtn().querySelector('button')).toBeNull()
  })
})

describe('ChatInput project chip branch copy', () => {
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

  it('copies the raw branch name and confirms', async () => {
    const writeText = stubClipboard()
    renderWithProviders(<ChatInput {...defaultProps} projectBranch="feat/example" />)
    fireEvent.click(branchBtn())
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('feat/example'))
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Copied branch name feat/example' })).toBeInTheDocument(),
    )
  })

  it('copies the untruncated branch name even when the label is clipped', async () => {
    const writeText = stubClipboard()
    const long = 'feat/a-very-long-branch-name-that-the-css-will-visually-truncate'
    renderWithProviders(<ChatInput {...defaultProps} projectBranch={long} />)
    fireEvent.click(branchBtn())
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(long))
  })

  it('withholds confirmation when both clipboard paths fail', async () => {
    originalClipboard = Object.getOwnPropertyDescriptor(navigator, 'clipboard')
    const writeText = vi.fn().mockRejectedValue(new DOMException('denied', 'NotAllowedError'))
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })

    const originalExecCommand = Object.getOwnPropertyDescriptor(document, 'execCommand')
    const execCommand = vi.fn().mockReturnValue(false)
    Object.defineProperty(document, 'execCommand', { value: execCommand, configurable: true })
    try {
      renderWithProviders(<ChatInput {...defaultProps} projectBranch="feat/example" />)
      fireEvent.click(branchBtn())
      await waitFor(() => expect(execCommand).toHaveBeenCalledWith('copy'))
      expect(screen.getByRole('button', { name: 'Copy branch name feat/example' })).toBeInTheDocument()
      expect(screen.queryByRole('button', { name: 'Copied branch name feat/example' })).not.toBeInTheDocument()
    } finally {
      if (originalExecCommand) Object.defineProperty(document, 'execCommand', originalExecCommand)
      else delete (document as { execCommand?: unknown }).execCommand
    }
  })

  it('clicking the branch does not open the project picker', () => {
    stubClipboard()
    const onProjectClick = vi.fn()
    renderWithProviders(
      <ChatInput {...defaultProps} onProjectClick={onProjectClick} projectBranch="feat/example" />,
    )
    fireEvent.click(branchBtn())
    expect(onProjectClick).not.toHaveBeenCalled()
  })

  it('clicking the folder segment still opens the picker', () => {
    stubClipboard()
    const onProjectClick = vi.fn()
    renderWithProviders(
      <ChatInput {...defaultProps} onProjectClick={onProjectClick} projectBranch="feat/example" />,
    )
    fireEvent.click(chip())
    expect(onProjectClick).toHaveBeenCalled()
  })

  it('keeps the message input focused while copying the branch', async () => {
    // A real pointer click moves focus to the pressed button before `click`
    // fires. The copy button cancels that transfer on mousedown so a user who
    // is mid-sentence can copy the branch and keep typing. `userEvent`
    // replays the full mousedown -> focus -> mouseup -> click sequence;
    // `fireEvent.click` alone would never move focus and could not fail here.
    const user = userEvent.setup()
    const writeText = stubClipboard()
    renderWithProviders(<ChatInput {...defaultProps} projectBranch="feat/example" />)
    const input = screen.getByRole('textbox', { name: 'Message input' })
    input.focus()

    await user.click(branchBtn())

    expect(input).toHaveFocus()
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('feat/example'))
  })
})

// #7263: a project pick made while a response is streaming is staged, not
// refused -- the project route arms a deferred reset that the NEXT turn
// consumes -- so the chip stays clickable mid-turn and says when the change
// lands. The agent and model chips stay locked: their routes answer 409.
describe('ChatInput project chip while a response is running', () => {
  it('stays enabled, opens the picker, and says the change applies from the next response', () => {
    const onProjectClick = vi.fn()
    renderWithProviders(<ChatInput {...defaultProps} onProjectClick={onProjectClick} isRunning onStop={vi.fn()} />)
    const btn = chip()
    expect(btn).toBeEnabled()
    const title = btn.getAttribute('title') || ''
    expect(title).toContain('Project: /home/u/work/KiroCrew')
    expect(title).toContain('Changes apply from the next response.')
    expect(btn.getAttribute('aria-label')).toBe(title)
    fireEvent.click(btn)
    expect(onProjectClick).toHaveBeenCalledTimes(1)
  })

  it('omits the next-response line when no response is running', () => {
    renderWithProviders(<ChatInput {...defaultProps} />)
    expect(chip().getAttribute('title')).not.toContain('next response')
  })

  it('keeps the agent and model chips locked mid-turn', () => {
    renderWithProviders(
      <ChatInput {...defaultProps} isRunning onStop={vi.fn()} agentName="kirocrew" onAgentClick={vi.fn()} modelName="claude-sonnet-4" onModelClick={vi.fn()} />,
    )
    expect(screen.getByRole('button', { name: /switch agents/ })).toBeDisabled()
    expect(screen.getByRole('button', { name: /switch model/ })).toBeDisabled()
  })
})
