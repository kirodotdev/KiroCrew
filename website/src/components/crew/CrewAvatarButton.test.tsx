/**
 * CrewAvatarButton — the "this face is editable" affordance (issue #9103).
 *
 * What is pinned here is the CONTRACT a host relies on, not pixels: the face
 * is a real button named "Edit avatar"; the scrim and the touch badge exist
 * with the classes that make one hover-revealed and the other hover:none-only;
 * the first-run chip appears exactly once and leaves through the same click
 * that opens the builder. The visual result is the capture harness's job.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import CrewAvatarButton from './CrewAvatarButton'

/** The persisted flag, spelled out: a rename in the component must be a
 *  deliberate migration, not a silent reset of every user's dismissal. */
const AVATAR_HINT_DISMISSED_KEY = 'mc-avatar-edit-hint-dismissed'
import CrewAvatar from '../CrewAvatar'

const ghost = (size = 28) => <CrewAvatar seed="oncall" size={size} />

beforeEach(() => {
  localStorage.clear()
})

describe('CrewAvatarButton', () => {
  it('is a button named "Edit avatar" that calls the one open path', () => {
    const onEdit = vi.fn()
    render(<CrewAvatarButton size={28} onEdit={onEdit} data-testid="face">{ghost(28)}</CrewAvatarButton>)
    const btn = screen.getByTestId('face')
    expect(btn.tagName).toBe('BUTTON')
    expect(btn).toHaveAccessibleName('Edit avatar')
    expect(btn).toHaveAttribute('title', 'Edit avatar')
    // A pointer cursor is part of the affordance, not a browser default the
    // host can rely on: Tailwind's preflight is what sets it on <button>, and
    // the class pins it independently of that.
    expect(btn.className).toContain('cursor-pointer')
    fireEvent.click(btn)
    expect(onEdit).toHaveBeenCalledTimes(1)
  })

  it('carries a hover/focus scrim that is hidden where hover cannot happen', () => {
    render(<CrewAvatarButton size={28} onEdit={() => {}}>{ghost(28)}</CrewAvatarButton>)
    const scrim = screen.getByTestId('avatar-edit-scrim')
    // Rest state invisible, revealed by the group's hover AND keyboard focus
    // (a mouse-only reveal would leave keyboard users with the old bare face).
    expect(scrim.className).toContain('opacity-0')
    expect(scrim.className).toContain('group-hover/avatar:opacity-100')
    expect(scrim.className).toContain('group-focus-visible/avatar:opacity-100')
    // Transition present, and deferring to prefers-reduced-motion.
    expect(scrim.className).toContain('transition-opacity')
    expect(scrim.className).toContain('motion-reduce:transition-none')
    // Under (hover: none) the scrim is gone outright — the badge takes over.
    expect(scrim.className).toContain('[@media(hover:none)]:hidden')
    expect(scrim).toHaveAttribute('aria-hidden', 'true')
    expect(scrim.querySelector('svg')).not.toBeNull()
  })

  it('carries a persistent pencil badge that only renders under (hover: none)', () => {
    render(<CrewAvatarButton size={28} onEdit={() => {}}>{ghost(28)}</CrewAvatarButton>)
    const badge = screen.getByTestId('avatar-edit-badge')
    // `hidden` at rest, forced to flex where hover is impossible — the
    // touchActions escape hatch, applied to a single badge.
    expect(badge.className).toContain('hidden')
    expect(badge.className).toContain('[@media(hover:none)]:flex')
    expect(badge).toHaveAttribute('aria-hidden', 'true')
    expect(badge.querySelector('svg')).not.toBeNull()
  })

  it('does not fire while disabled', () => {
    const onEdit = vi.fn()
    render(<CrewAvatarButton size={28} onEdit={onEdit} disabled hint data-testid="face">{ghost(28)}</CrewAvatarButton>)
    fireEvent.click(screen.getByTestId('face'))
    expect(onEdit).not.toHaveBeenCalled()
    // A disabled face must not advertise a nudge it cannot honour.
    expect(screen.queryByTestId('avatar-edit-hint')).toBeNull()
  })

  describe('first-run hint chip', () => {
    it('renders once while the host says the face is the default, and clicking it opens AND dismisses', () => {
      const onEdit = vi.fn()
      render(<CrewAvatarButton size={30} onEdit={onEdit} hint>{ghost(30)}</CrewAvatarButton>)
      const chip = screen.getByTestId('avatar-edit-hint')
      expect(chip).toHaveTextContent('Edit this avatar')
      fireEvent.click(chip)
      expect(onEdit).toHaveBeenCalledTimes(1)
      // Gone in the same render the builder opens — not on the next mount.
      expect(screen.queryByTestId('avatar-edit-hint')).toBeNull()
      expect(localStorage.getItem(AVATAR_HINT_DISMISSED_KEY)).toBe('1')
    })

    it('clicking the face itself also dismisses the chip', () => {
      render(<CrewAvatarButton size={30} onEdit={() => {}} hint data-testid="face">{ghost(30)}</CrewAvatarButton>)
      expect(screen.getByTestId('avatar-edit-hint')).toBeInTheDocument()
      fireEvent.click(screen.getByTestId('face'))
      expect(screen.queryByTestId('avatar-edit-hint')).toBeNull()
      expect(localStorage.getItem(AVATAR_HINT_DISMISSED_KEY)).toBe('1')
    })

    it('never returns once dismissed, even for a fresh mount with hint=true', () => {
      localStorage.setItem(AVATAR_HINT_DISMISSED_KEY, '1')
      render(<CrewAvatarButton size={30} onEdit={() => {}} hint>{ghost(30)}</CrewAvatarButton>)
      expect(screen.queryByTestId('avatar-edit-hint')).toBeNull()
    })

    it('is absent when the host reports a customized face', () => {
      render(<CrewAvatarButton size={30} onEdit={() => {}} hint={false}>{ghost(30)}</CrewAvatarButton>)
      expect(screen.queryByTestId('avatar-edit-hint')).toBeNull()
    })
  })
})
