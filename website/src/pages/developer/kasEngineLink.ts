/**
 * Where the engine choice lives and how to link to it. Constants only, so the
 * Settings and Developer redirects can forward old links without pulling the
 * card's component graph into their chunks.
 */
import { settingsPath } from '../../components/settingsPath'
import { resolveLegacyHighlightId } from '../../hooks/useSettingHighlight'
import { AGENT_HARNESS_SETTINGS_TAB } from './kiroSignInLink'

/** The config field the engine choice writes. */
export const KAS_BUNDLE_CONFIG_KEY = 'agent.kas_bundle'

/** The engine choice's Settings search id (`settingsRegistry.gen.ts`). */
export const KAS_ENGINE_SETTING_ID = 'agent.engine'

/** The KAS row on Agent Harness, with the engine choice ringed. */
export const KAS_ENGINE_PATH = settingsPath({ tab: AGENT_HARNESS_SETTINGS_TAB, highlight: KAS_ENGINE_SETTING_ID })

/**
 * Whether a `highlight` value asks for the engine choice: its config key
 * (`key:agent.kas_bundle`), its search id, or the id the switch had while it sat
 * in Developer > Feature previews. Old links on that page are forwarded to
 * `KAS_ENGINE_PATH`.
 */
export function isKasEngineHighlight(highlight: string | null | undefined): boolean {
  if (!highlight) return false
  return highlight === `key:${KAS_BUNDLE_CONFIG_KEY}` || resolveLegacyHighlightId(highlight) === KAS_ENGINE_SETTING_ID
}
