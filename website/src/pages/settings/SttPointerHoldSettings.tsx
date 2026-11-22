import { useEffect, useState } from 'react'

import { SettingsSection, SettingsStepper, SettingsToggle } from '../../components/settings'
import ErrorNotice from '../../components/ErrorNotice'
import { i18nT } from '../../i18n/t'
import { clampHoldMs, HOLD_MS_STEP, loadPttConfig, PTT_CHANGED_EVENT, savePttConfig, toSeconds } from '../../lib/pushToTalk'

/**
 * Press-and-hold on the message box: the pointer's way into dictation, beside
 * the key's. Same browser-local store as the key (`lib/pushToTalk`), so the two
 * share one save path and one change event, but its own switch and its own hold
 * time: a trackpad press is a different gesture from a bare modifier key, and a
 * user who turned the key off to stop triggering it by accident may still want a
 * hold they have to mean. Off by default -- a press on a text box has always
 * placed the caret, and keeps doing only that until asked.
 *
 * Its own module rather than another block in SttSettings.tsx, which the
 * message-box section would have pushed toward the file-line cap. The key's
 * `PushToTalkConfig` stays there; the two only share the stored record and its
 * change event, not component code.
 *
 * No test strip: the gesture is tried where it lives, on the message box, and
 * a hold there with the microphone off is indistinguishable from a click.
 */
export function PointerHoldConfig() {
  const [cfg, setCfg] = useState(() => loadPttConfig())
  const [saveError, setSaveError] = useState<string | null>(null)
  const patch = (next: Partial<typeof cfg>) => {
    const merged = { ...loadPttConfig(), ...next }
    if (!savePttConfig(merged)) {
      setSaveError(i18nT('pages.settings.sttSettings.ptt_save_refused'))
      return
    }
    setSaveError(null)
    setCfg(merged)
  }
  // Two editors of one stored record: the key section and another browser tab
  // save the same blob, so every signal re-reads the complete current record.
  useEffect(() => {
    const onChange = () => setCfg(loadPttConfig())
    window.addEventListener(PTT_CHANGED_EVENT, onChange)
    window.addEventListener('storage', onChange)
    return () => {
      window.removeEventListener(PTT_CHANGED_EVENT, onChange)
      window.removeEventListener('storage', onChange)
    }
  }, [])

  return (
    <SettingsSection title={i18nT('pages.settings.sttSettings.pointer_heading')} collapsible>
      <p className="text-[12px] text-muted mb-1">{i18nT('pages.settings.sttSettings.pointer_heading_desc')}</p>
      <SettingsToggle
        label={i18nT('pages.settings.sttSettings.pointer_enabled')}
        hint={i18nT('pages.settings.sttSettings.pointer_enabled_hint')}
        checked={cfg.pointerHold}
        onChange={v => patch({ pointerHold: v })}
      />
      {saveError && (
        <ErrorNotice variant="inline" message={saveError} askAgent testId="pointer-hold-save-error" className="my-1" actionPlacement="below-on-phone" />
      )}
      {/* Dimmed rather than hidden while off: the value still reads as what the
          gesture WOULD use, which is the one thing a reader deciding whether to
          turn it on wants to know. */}
      <SettingsStepper
        label={i18nT('pages.settings.sttSettings.pointer_hold_time')}
        hint={i18nT('pages.settings.sttSettings.pointer_hold_time_desc')}
        value={i18nT('pages.settings.sttSettings.ptt_hold_seconds', { secs: toSeconds(cfg.pointerHoldMs) })}
        onIncrement={() => patch({ pointerHoldMs: clampHoldMs(cfg.pointerHoldMs + HOLD_MS_STEP) })}
        onDecrement={() => patch({ pointerHoldMs: clampHoldMs(cfg.pointerHoldMs - HOLD_MS_STEP) })}
        disabled={!cfg.pointerHold}
      />
    </SettingsSection>
  )
}
