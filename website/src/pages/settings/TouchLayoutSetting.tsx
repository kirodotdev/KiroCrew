/**
 * Settings > Display > View: the "Touch screen layout" choice.
 *
 * A touch-first device (coarse pointer that cannot hover) takes the layout form
 * its browser asks for (`lib/touchLayoutForm.ts`). A browser with no "Request
 * desktop site" toggle, such as an installed web app or an in-app browser, leaves
 * no way to change that, so this row picks the form instead. It is saved on this
 * browser only and drawn only on a touch-first device: a computer with a mouse
 * never sees it, and its layout never reads the choice.
 */
import { useState, useSyncExternalStore } from 'react'
import { useTranslation } from 'react-i18next'
import { SettingsSelect } from '../../components/settings'
import {
  TOUCH_LAYOUT_CHOICES,
  isTouchFirst,
  readTouchLayoutChoice,
  saveTouchLayoutChoice,
  type TouchLayoutChoice,
} from '../../lib/touchLayoutForm'

const TOUCH_FIRST_QUERIES = ['(pointer: coarse)', '(hover: none)']

function subscribeTouchFirst(onChange: () => void) {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return () => {}
  const lists = TOUCH_FIRST_QUERIES.map(query => window.matchMedia(query))
  for (const list of lists) list.addEventListener?.('change', onChange)
  return () => { for (const list of lists) list.removeEventListener?.('change', onChange) }
}

export function TouchLayoutSetting() {
  const { t } = useTranslation()
  const touchFirst = useSyncExternalStore(subscribeTouchFirst, isTouchFirst, () => false)
  const [choice, setChoice] = useState<TouchLayoutChoice>(readTouchLayoutChoice)
  if (!touchFirst) return null
  const labels: Record<TouchLayoutChoice, string> = {
    auto: t('pages.settings.displayPanel.touch_layout_auto'),
    phone: t('pages.settings.displayPanel.touch_layout_phone'),
    desktop: t('pages.settings.displayPanel.touch_layout_desktop'),
  }
  return (
    <SettingsSelect
      label={t('pages.settings.displayPanel.touch_layout')}
      hint={t('pages.settings.displayPanel.touch_layout_hint')}
      value={choice}
      options={[...TOUCH_LAYOUT_CHOICES]}
      optionLabels={TOUCH_LAYOUT_CHOICES.map(option => labels[option])}
      onChange={value => {
        const next = TOUCH_LAYOUT_CHOICES.find(option => option === value)
        if (!next) return
        setChoice(next)
        saveTouchLayoutChoice(next)
      }}
    />
  )
}
