import { useEffect, useState, useSyncExternalStore } from 'react'

import { SettingsCard, SettingsToggle } from '../../components/settings'
import { INSPECTOR_KEYS } from '../../dev/scrollInspector'
import { SLOW_FRAME_LOG_KEYS, slowFrameSnapshot, subscribeSlowFrameLog } from '../../dev/slowFrameLog'
import { safeGetItem, safeSetItem } from '../../utils/safeStorage'
import { i18nT } from '../../i18n/t'
import { SlowFrameLogReadings } from './SlowFrameLogReadings'

/**
 * Settings > Developer > Debug tools — instruments that report what the app is doing to itself,
 * on the device where it is happening.
 *
 * These are not feature previews and not settings. A preview flag hides a surface
 * that is not finished; a setting is a preference the operator keeps. A debug
 * tool is neither: it is an instrument, nobody wants it on by default, and an
 * overlay paints over real product UI while on. So it lives on Settings > Developer >
 * Debug tools, beside the other internals views, off by default. It is deliberately
 * kept out of Settings search: indexing it via gen-settings-registry.mjs would
 * advertise it to readers it is not for.
 *
 * Each tool owns its own storage key and CustomEvent (see the module it drives).
 * The event is what lets an already-open chat pick the change up without a
 * reload: these tools live outside React by design, because the paths they
 * instrument are the ones a re-render would disturb.
 */
export function DebugToolsTab() {
  const [scrollInspector, setScrollInspector] = useState(
    () => safeGetItem(INSPECTOR_KEYS.ENABLED_KEY) === '1',
  )
  // Another tab's switch turns the inspector on or off here too (the inspector
  // hears the same storage event), so this switch re-reads the key with it.
  useEffect(() => {
    const onStorage = (e: StorageEvent) => {
      if (e.key === INSPECTOR_KEYS.ENABLED_KEY) setScrollInspector(safeGetItem(INSPECTOR_KEYS.ENABLED_KEY) === '1')
    }
    window.addEventListener('storage', onStorage)
    return () => window.removeEventListener('storage', onStorage)
  }, [])
  // The log's own state, not a copy: another tab's switch or a lock that lands
  // later turns the log on or off here too, and this switch must say so.
  const slowFrameLog = useSyncExternalStore(subscribeSlowFrameLog, slowFrameSnapshot).enabled

  const toggleScrollInspector = (v: boolean) => {
    safeSetItem(INSPECTOR_KEYS.ENABLED_KEY, v ? '1' : '0')
    setScrollInspector(v)
    window.dispatchEvent(new CustomEvent(INSPECTOR_KEYS.ENABLED_EVENT, { detail: v }))
  }

  const toggleSlowFrameLog = (v: boolean) => {
    safeSetItem(SLOW_FRAME_LOG_KEYS.ENABLED_KEY, v ? '1' : '0')
    window.dispatchEvent(new CustomEvent(SLOW_FRAME_LOG_KEYS.ENABLED_EVENT, { detail: v }))
  }

  return (
    <SettingsCard>
      <SettingsToggle
        label={i18nT('pages.developer.debugToolsTab.scroll_inspector')}
        description={i18nT('pages.developer.debugToolsTab.overlays_the_chat_with_live_scroll_and_anchor_dia')}
        checked={scrollInspector}
        onChange={toggleScrollInspector}
      />
      <SettingsToggle
        label={i18nT('pages.developer.debugToolsTab.slow_frame_log')}
        description={i18nT('pages.developer.debugToolsTab.slow_frame_log_desc')}
        checked={slowFrameLog}
        onChange={toggleSlowFrameLog}
      />
      <SlowFrameLogReadings />
    </SettingsCard>
  )
}
