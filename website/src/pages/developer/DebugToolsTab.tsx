import { useState } from 'react'

import { SettingsCard, SettingsToggle } from '../../components/settings'
import { INSPECTOR_KEYS } from '../../dev/scrollInspector'
import { safeGetItem, safeSetItem } from '../../utils/safeStorage'
import { i18nT } from '../../i18n/t'

/**
 * Developer > Debug tools — overlays that report what the app is doing to itself,
 * on the device where it is happening.
 *
 * These are not feature previews and not settings. A preview flag hides a surface
 * that is not finished; a setting is a preference the operator keeps. A debug
 * overlay is neither: it is an instrument, it paints over real product UI while
 * on, and nobody wants it on by default. So it lives behind the Developer Mode
 * gate on this page rather than in Settings, where the toggle would also be
 * indexed into Settings search by `gen-settings-registry.mjs` and advertised to
 * readers it is not for.
 *
 * Each tool owns its own storage key and CustomEvent (see the module it drives).
 * The event is what lets an already-open chat pick the change up without a
 * reload: these overlays live outside React by design, because the paths they
 * instrument are the ones a re-render would disturb.
 */
export function DebugToolsTab() {
  const [scrollInspector, setScrollInspector] = useState(
    () => safeGetItem(INSPECTOR_KEYS.ENABLED_KEY) === '1',
  )

  const toggleScrollInspector = (v: boolean) => {
    safeSetItem(INSPECTOR_KEYS.ENABLED_KEY, v ? '1' : '0')
    setScrollInspector(v)
    window.dispatchEvent(new CustomEvent(INSPECTOR_KEYS.ENABLED_EVENT, { detail: v }))
  }

  return (
    <SettingsCard>
      <SettingsToggle
        label={i18nT('pages.developer.debugToolsTab.scroll_inspector')}
        description={i18nT('pages.developer.debugToolsTab.overlays_the_chat_with_live_scroll_and_anchor_dia')}
        checked={scrollInspector}
        onChange={toggleScrollInspector}
      />
    </SettingsCard>
  )
}
