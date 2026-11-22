import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { AccountAvatar } from '../components/AccountAvatar'
import { emailInitials } from '../lib/emailInitials'
import type { KiroAccountUsage } from '../components/KiroAccountModal'

const reading = (email: string) => ({ used: 250, limit: 1000, email }) as unknown as KiroAccountUsage

describe('emailInitials', () => {
  it('takes the first letters of the first two words of the local part', () => {
    expect(emailInitials('zezhen.xu@example.com')).toBe('ZX')
    expect(emailInitials('ann_lee-smith@example.com')).toBe('AL')
  })

  it('takes the first two letters of a one-word local part, and ? for none', () => {
    expect(emailInitials('kiro@example.com')).toBe('KI')
    expect(emailInitials('@example.com')).toBe('?')
  })
})

describe('AccountAvatar', () => {
  it("draws the reading's initials", () => {
    render(<AccountAvatar usage={reading('zezhen.xu@example.com')} size={26} />)
    expect(screen.getByTestId('account-avatar-initials')).toHaveTextContent('ZX')
    expect(screen.queryByTestId('account-avatar-empty')).toBeNull()
  })

  it.each<KiroAccountUsage>([null, 'none', 'failed', 'signin-required'])('draws an empty person circle for %s', usage => {
    render(<AccountAvatar usage={usage} size={26} />)
    expect(screen.getByTestId('account-avatar-empty')).toBeInTheDocument()
    expect(screen.queryByTestId('account-avatar-initials')).toBeNull()
  })
})
