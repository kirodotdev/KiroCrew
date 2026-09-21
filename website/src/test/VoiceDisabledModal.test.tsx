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
    expect(screen.getByText('Turn on voice input')).toBeInTheDocument()
    expect(screen.getByText(/Settings\s*→\s*Voice/)).toBeInTheDocument()
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

  it('renders the per-code reason for a missing voice extra', () => {
    render(
      <VoiceDisabledModal open reason="unavailable" provider="local" code="stt_extra_missing" onClose={() => {}} onOpenSettings={() => {}} />,
    )
    expect(screen.getByText(/voice packages are not installed/i)).toBeInTheDocument()
  })

  it('shows the install command and still routes to settings', () => {
    const onOpenSettings = vi.fn()
    render(
      <VoiceDisabledModal
        open
        reason="unavailable"
        provider="local"
        code="stt_extra_missing"
        installCommand="pip install 'kiro-crew[voice]'"
        onClose={() => {}}
        onOpenSettings={onOpenSettings}
      />,
    )
    expect(screen.getByText("pip install 'kiro-crew[voice]'")).toBeInTheDocument()
    fireEvent.click(screen.getByText('Open settings'))
    expect(onOpenSettings).toHaveBeenCalledTimes(1)
  })
})
