import { render } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { ChannelBrandIcon } from '../components/ChannelBrandIcon'

describe('ChannelBrandIcon', () => {
  it.each([
    'slack',
    'discord',
    'telegram',
    'teams',
    'webex',
    'wecom',
    'weixin',
  ])('renders the %s brand asset', channel => {
    const { container } = render(<ChannelBrandIcon channel={channel} size={12} />)
    const image = container.querySelector('img')

    expect(image).not.toBeNull()
    expect(image).toHaveAttribute('width', '12')
    expect(image).toHaveAttribute('height', '12')
  })

  it('uses the generic link glyph only for an unknown channel', () => {
    const { container } = render(<ChannelBrandIcon channel="future-channel" />)

    expect(container.querySelector('img')).toBeNull()
    expect(container.querySelector('svg')).not.toBeNull()
  })
})
