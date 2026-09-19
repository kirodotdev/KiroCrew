import React from 'react'
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'

// Pins both project-chip identities: a Project-attached session shows its
// Project name, while a directory-only session shows "<folder> · <branch>" and
// degrades to the folder name alone when there is no branch to show.

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
  it('shows an attached Project name instead of its workspace repository', () => {
    renderWithProviders(
      <ChatInput
        {...defaultProps}
        project="/home/u/projects/launchpad/sources/service"
        projectBranch="main"
        projectBundleName="Launchpad Workspace"
      />,
    )
    // The accessible name explains the inert control, not just names it:
    // keyboard and AT users get the same reason the tooltip carries.
    const explanation = "Project: Launchpad Workspace. This session works inside this Project's files and can't switch."
    const btn = screen.getByRole('button', { name: explanation })
    expect(btn).toHaveTextContent('Launchpad Workspace')
    expect(btn).not.toHaveTextContent('service')
    // Visibly inert (muted, faded, not-allowed cursor) and `aria-disabled`
    // rather than natively disabled: a disabled button takes no focus and no
    // click, and the click is how the explanation is reached without a hover.
    expect(btn).toHaveAttribute('aria-disabled', 'true')
    expect(btn).not.toBeDisabled()
    expect(btn.className).toMatch(/\btext-muted\b/)
    expect(btn.className).toMatch(/\bopacity-60\b/)
    expect(btn.className).toMatch(/\bcursor-not-allowed\b/)
    expect(btn.className).not.toMatch(/hover:(scale|-?translate)/)
    // The bubble replaces the native title: one tooltip for one sentence.
    expect(btn).not.toHaveAttribute('title')
    expect(screen.queryByRole('button', { name: /Copy branch name/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()

    // The first click is not silent: it opens the explanation, and opens no
    // picker. Focus opens it too; blur closes it.
    fireEvent.click(btn)
    expect(defaultProps.onProjectClick).not.toHaveBeenCalled()
    const tip = screen.getByRole('tooltip')
    expect(tip).toHaveTextContent(explanation)
    expect(btn).toHaveAttribute('aria-describedby', tip.id)
    fireEvent.blur(btn)
    expect(screen.queryByRole('tooltip')).not.toBeInTheDocument()
    fireEvent.focus(btn)
    expect(screen.getByRole('tooltip')).toHaveTextContent(explanation)
  })

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

  it('keeps the branch out of the accessible name while a response is running', () => {
    renderWithProviders(<ChatInput {...defaultProps} projectBranch="main" isRunning onStop={vi.fn()} />)
    const btn = screen.getByRole('button', { name: /Stop the current response to switch project/ })
    expect(btn).toBeDisabled()
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
