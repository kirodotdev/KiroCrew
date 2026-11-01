import React from 'react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, waitFor } from '@testing-library/react'

const languageState = { current: 'en' }
const changeLanguage = vi.fn((lng: string) => {
  languageState.current = lng
  return Promise.resolve()
})
const initI18n = vi.fn()

vi.mock('../../i18n/all', () => ({
  get changeLanguage() {
    return changeLanguage
  },
  get initI18n() {
    return initI18n
  },
  get i18next() {
    return { get language() { return languageState.current } }
  },
}))

const readStoredLanguage = vi.fn(() => 'de')
vi.mock('../../i18n/detect', () => ({
  get readStoredLanguage() {
    return readStoredLanguage
  },
}))

const getMochiConfig = vi.fn(async () => ({ language: 'fr' }))
let configListener: ((mochi: { language?: string }) => void) | undefined
const offConfig = vi.fn()
const onConfigUpdated = vi.fn((cb: (mochi: { language?: string }) => void) => {
  configListener = cb
  return offConfig
})

vi.mock('./src/mochiApi', () => ({
  get api() {
    return { getMochiConfig, onConfigUpdated }
  },
}))

import { MochiLocalized, initMochiI18n } from './mochiLanguage'

function Probe({ onMount }: { onMount: () => void }) {
  React.useEffect(() => {
    onMount()
  }, [onMount])
  return <span>probe</span>
}

describe('initMochiI18n', () => {
  beforeEach(() => {
    initI18n.mockClear()
  })

  it('seeds the ambient i18n instance once with no override', () => {
    initMochiI18n()
    expect(initI18n).toHaveBeenCalledTimes(1)
    expect(initI18n).toHaveBeenCalledWith()
  })
})

describe('MochiLocalized', () => {
  beforeEach(() => {
    languageState.current = 'en'
    changeLanguage.mockClear()
    readStoredLanguage.mockClear()
    getMochiConfig.mockClear()
    onConfigUpdated.mockClear()
    offConfig.mockClear()
    configListener = undefined
  })

  it('applies the configured override language on mount', async () => {
    render(
      <MochiLocalized>
        <span>child</span>
      </MochiLocalized>,
    )
    await waitFor(() => expect(changeLanguage).toHaveBeenCalledWith('fr'))
    expect(getMochiConfig).toHaveBeenCalledTimes(1)
    expect(languageState.current).toBe('fr')
  })

  it('follows the stored KiroCrew language when the override is empty', async () => {
    getMochiConfig.mockResolvedValueOnce({ language: '' })
    render(
      <MochiLocalized>
        <span>child</span>
      </MochiLocalized>,
    )
    await waitFor(() => expect(changeLanguage).toHaveBeenCalledWith('de'))
    expect(readStoredLanguage).toHaveBeenCalled()
    expect(languageState.current).toBe('de')
  })

  it('treats a non-string override as following KiroCrew', async () => {
    getMochiConfig.mockResolvedValueOnce({ language: 42 as unknown as string })
    render(
      <MochiLocalized>
        <span>child</span>
      </MochiLocalized>,
    )
    await waitFor(() => expect(changeLanguage).toHaveBeenCalledWith('de'))
  })

  it('remounts its subtree when the language changes and remount is true', async () => {
    const onMount = vi.fn()
    const { findByText } = render(
      <MochiLocalized>
        <Probe onMount={onMount} />
      </MochiLocalized>,
    )
    await findByText('probe')
    await waitFor(() => expect(changeLanguage).toHaveBeenCalledWith('fr'))
    await waitFor(() => expect(onMount).toHaveBeenCalledTimes(2))
  })

  it('does not remount its subtree when remount is false', async () => {
    const onMount = vi.fn()
    const { findByText } = render(
      <MochiLocalized remount={false}>
        <Probe onMount={onMount} />
      </MochiLocalized>,
    )
    await findByText('probe')
    await waitFor(() => expect(changeLanguage).toHaveBeenCalledWith('fr'))
    expect(onMount).toHaveBeenCalledTimes(1)
  })

  it('applies a live config update and unsubscribes on unmount', async () => {
    const { unmount } = render(
      <MochiLocalized>
        <span>child</span>
      </MochiLocalized>,
    )
    await waitFor(() => expect(changeLanguage).toHaveBeenCalledWith('fr'))
    expect(onConfigUpdated).toHaveBeenCalledTimes(1)

    configListener?.({ language: 'ja' })
    await waitFor(() => expect(changeLanguage).toHaveBeenCalledWith('ja'))

    unmount()
    expect(offConfig).toHaveBeenCalledTimes(1)
  })
})
