/**
 * Isolated capture + measurement entry for the Settings dropdown that cannot be
 * scrolled by touch on a phone (Settings → Voice → Language, ~40 BCP-47 codes).
 *
 * WHY ISOLATED: the defect is a real-layout + real-input-injection one. It needs
 * a live Radix Select popup measured against a phone viewport and driven by a
 * genuine touch gesture (CDP Input.synthesizeScrollGesture) — happy-dom computes
 * no layout and synthetic TouchEvents never produce native scrolling, so neither
 * can observe it. The live dashboard cannot stand in either: it is token-gated.
 *
 * The faithful part is the COMPONENT: the real `SettingsSelect` is imported, so
 * the popup under test is the same SimpleSelect → ui/select.tsx → Radix Select
 * stack production ships, with the same option count the STT language row has.
 *
 * window.__measure() reports the popup viewport's scroll geometry and the
 * computed styles that decide whether a touch drag can move it at all.
 */
import { useState } from 'react'
import { createRoot } from 'react-dom/client'
import { initI18n } from '../src/i18n'
import { SettingsCard, SettingsSelect } from '../src/components/settings'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'light'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

/** The STT language list, as the speech provider reports it. */
const LANGS = [
  'en-US', 'en-GB', 'en-AU', 'en-IN', 'en-NZ', 'en-IE', 'fr-FR', 'fr-CA', 'de-DE', 'de-CH',
  'es-ES', 'es-US', 'es-MX', 'it-IT', 'pt-BR', 'pt-PT', 'nl-NL', 'sv-SE', 'da-DK', 'nb-NO',
  'fi-FI', 'pl-PL', 'cs-CZ', 'sk-SK', 'ro-RO', 'hu-HU', 'el-GR', 'tr-TR', 'ru-RU', 'uk-UA',
  'he-IL', 'ar-SA', 'hi-IN', 'th-TH', 'vi-VN', 'id-ID', 'ms-MY', 'ja-JP', 'ko-KR', 'zh-CN',
  'zh-TW', 'zh-HK',
]

function Scene() {
  // `?value=` defaults to a code the provider list does NOT contain, which is the
  // reported state: the trigger shows zh-CN, no row is checked, and the popup
  // therefore opens at the TOP of the list with the FIRST item focused rather
  // than scrolled to a selection.
  const [lang, setLang] = useState(params.get('value') ?? 'zh-CN')
  const options = params.get('value') === 'in-list' ? LANGS : LANGS.filter(l => l !== 'zh-CN')
  return (
    <div className="bg-bg text-text min-h-[844px] p-4">
      {/* Filler so the row sits low enough for the popup to open UPWARD, which is
          where the reported screenshot has it. */}
      <div className="text-[11px] text-muted" style={{ height: Number(params.get('filler') ?? 520) }}>Tap vs hold cutoff · Try it</div>
      <SettingsCard>
        <SettingsSelect
          label="Language"
          hint="BCP-47 language code for speech recognition"
          value={lang}
          options={options}
          onChange={setLang}
        />
      </SettingsCard>
    </div>
  )
}

interface Measure {
  items: number
  scrollTop: number
  scrollHeight: number
  clientHeight: number
  scrollable: boolean
  viewportOverflowY: string
  viewportTouchAction: string
  viewportFlex: string
  contentDisplay: string
  contentOverflow: string
  contentMaxHeight: string
  contentTouchAction: string
  rect: { x: number; y: number; w: number; h: number }
  bodyPointerEvents: string
}

declare global {
  interface Window {
    __measure: () => Measure | null
  }
}

window.__measure = () => {
  const vp = document.querySelector<HTMLElement>('[data-radix-select-viewport]')
  if (!vp) return null
  const content = vp.closest<HTMLElement>('[role="listbox"]')!
  const vcs = getComputedStyle(vp)
  const ccs = getComputedStyle(content)
  const r = content.getBoundingClientRect()
  return {
    items: vp.querySelectorAll('[role="option"]').length,
    scrollTop: Math.round(vp.scrollTop),
    scrollHeight: vp.scrollHeight,
    clientHeight: vp.clientHeight,
    scrollable: vp.scrollHeight > vp.clientHeight,
    viewportOverflowY: vcs.overflowY,
    viewportTouchAction: vcs.touchAction,
    viewportFlex: vcs.flex,
    contentDisplay: ccs.display,
    contentOverflow: ccs.overflow,
    contentMaxHeight: ccs.maxHeight,
    contentTouchAction: ccs.touchAction,
    rect: { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) },
    bodyPointerEvents: getComputedStyle(document.body).pointerEvents,
  }
}

initI18n('en')
createRoot(document.getElementById('root')!).render(<Scene />)
