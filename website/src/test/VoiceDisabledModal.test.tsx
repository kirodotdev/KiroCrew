import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import VoiceDisabledModal from '../components/VoiceDisabledModal'

describe('VoiceDisabledModal', () => {
  it('renders nothing when closed', () => {
    render(<VoiceDisabledModal open={false} onClose={() => {}} onOpenSettings={() => {}} />)
    expect(screen.queryByText('Turn on voice input')).toBeNull()
  })

  it('shows the enable-STT guidance and settings path when open', () => {
    render(<VoiceDisabledModal open onClose={() => {}} onOpenSettings={() => {}} />)
    expect(screen.getByText('Turn on voice input')).toBeTruthy()
    expect(screen.getByText(/Settings\s*→\s*Voice/)).toBeTruthy()
  })

  it('calls onOpenSettings when "Open settings" is clicked', () => {
    const onOpenSettings = vi.fn()
    render(<VoiceDisabledModal open onClose={() => {}} onOpenSettings={onOpenSettings} />)
    fireEvent.click(screen.getByText('Open settings'))
    expect(onOpenSettings).toHaveBeenCalledTimes(1)
  })

  it('calls onClose when "Not now" is clicked', () => {
    const onClose = vi.fn()
    render(<VoiceDisabledModal open onClose={onClose} onOpenSettings={() => {}} />)
    fireEvent.click(screen.getByText('Not now'))
    expect(onClose).toHaveBeenCalledTimes(1)
  })
})
