import { cleanup, render } from '@testing-library/react'
import React from 'react'
import lottie from 'lottie-web/build/player/lottie_light'
import { afterEach, expect, it, vi } from 'vitest'

import { LottieRenderer } from './LottieRenderer'

const loadAnimation = vi.mocked(lottie.loadAnimation)

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  loadAnimation.mockClear()
})

it('leaves a diagnostic when an imported animation is malformed', () => {
  const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {})

  render(<LottieRenderer animationData="{not json" width={64} height={64} />)

  expect(loadAnimation).not.toHaveBeenCalled()
  expect(errorSpy).toHaveBeenCalledWith(
    '[crew-companion] lottie JSON parse failed',
    expect.objectContaining({ bytes: 9 }),
  )
})
