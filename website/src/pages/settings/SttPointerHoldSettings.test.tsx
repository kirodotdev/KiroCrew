import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'

import { i18nT } from '../../i18n/t'
import {
  loadPttConfig,
  PTT_STORAGE_KEY,
  type PttConfig,
} from '../../lib/pushToTalk'
import { PointerHoldConfig } from './SttPointerHoldSettings'

const INITIAL_CONFIG: PttConfig = {
  enabled: true,
  mode: 'hybrid',
  binding: { code: 'AltRight' },
  holdMs: 500,
  pointerHold: false,
  pointerHoldMs: 500,
}

describe('PointerHoldConfig', () => {
  beforeEach(() => {
    localStorage.clear()
    localStorage.setItem(PTT_STORAGE_KEY, JSON.stringify(INITIAL_CONFIG))
  })

  it('preserves a keyboard setting another tab changed after mount', () => {
    render(<PointerHoldConfig />)
    fireEvent.click(screen.getByRole('button', {
      name: i18nT('pages.settings.sttSettings.pointer_heading'),
    }))
    const configFromOtherTab = { ...INITIAL_CONFIG, enabled: false }
    // A storage write can precede this tab's storage event. Saving must merge
    // from the store itself rather than the component's mount-time snapshot.
    localStorage.setItem(PTT_STORAGE_KEY, JSON.stringify(configFromOtherTab))

    fireEvent.click(screen.getByRole('switch', {
      name: i18nT('pages.settings.sttSettings.pointer_enabled'),
    }))

    expect(loadPttConfig()).toEqual({ ...configFromOtherTab, pointerHold: true })
  })
})
