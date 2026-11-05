import { useEffect, useMemo, useState } from 'react'

import ErrorNotice from '../../components/ErrorNotice'
import { SettingsToggle } from '../../components/settings'
import type { CustomThemeData } from '../../hooks/useTheme'
import { i18nT } from '../../i18n/t'
import {
  experiencePack,
  readExperience,
  saveExperienceChoice,
  subscribeExperience,
  type ExperienceChoice,
  type ExperienceState,
} from '../../utils/themeConsent'

/**
 * The parts of the ACTIVE Level-2 pack, each on or off on its own. Sounds and
 * Overlays apply as soon as they are toggled. Turning Persona on stores the
 * choice without a grant, so ThemeExperienceLayer's consent dialog opens and
 * shows the persona text; the grant is written only when Save is pressed there,
 * so what the backend injects is the text the user read. Turning it off revokes
 * the grant. Stays in sync with ThemeExperienceLayer through the change and
 * storage events. A toggle the browser cannot store still applies for this tab
 * and shows a notice under the section until the next stored toggle.
 */
export function ThemeExperienceSettings({
  colorTheme,
  customThemeDataMap,
}: {
  colorTheme: string
  customThemeDataMap: Map<string, CustomThemeData>
}) {
  const slug = colorTheme.startsWith('custom-') ? colorTheme.slice('custom-'.length) : ''
  const data = slug ? customThemeDataMap.get(slug) : undefined
  const isL2 = (data?.level ?? 0) >= 2
  const assets = isL2 ? data?.assets : undefined
  const assetsKey = assets ? JSON.stringify(assets) : ''
  // eslint-disable-next-line react-hooks/exhaustive-deps -- keyed on content: the theme list rebuilds `assets` on every catalog change
  const pack = useMemo(() => experiencePack(assets), [assetsKey])
  const [state, setState] = useState<ExperienceState | null>(() => (isL2 ? readExperience(slug, pack) : null))
  // The pack whose last toggle could not be stored; cleared by the next stored toggle.
  const [failedSlug, setFailedSlug] = useState<string | null>(null)

  useEffect(() => {
    if (!isL2) {
      setState(null)
      return
    }
    const reread = () => setState(readExperience(slug, pack))
    reread()
    return subscribeExperience(slug, reread)
  }, [slug, isL2, pack])

  if (!isL2 || !state || !(pack.sounds || pack.overlays || pack.persona)) return null

  const current: ExperienceChoice = { sounds: state.sounds, overlays: state.overlays, persona: state.persona }
  const store = (next: ExperienceChoice, personaToken: string | null) =>
    setFailedSlug(saveExperienceChoice(slug, next, personaToken) ? null : slug)
  const save = (next: ExperienceChoice) => store(next, next.persona ? pack.personaToken : null)

  return (
    <div role="group" aria-label={i18nT('pages.settings.displayPanel.theme_experience')}>
      <div className="text-[13px] font-semibold text-text pt-1">
        {i18nT('pages.settings.displayPanel.theme_experience')}
      </div>
      {pack.sounds && (
        <SettingsToggle
          label={i18nT('pages.settings.displayPanel.theme_experience_sounds')}
          checked={state.sounds}
          onChange={(v) => save({ ...current, sounds: v })}
        />
      )}
      {pack.overlays && (
        <SettingsToggle
          label={i18nT('pages.settings.displayPanel.theme_experience_overlays')}
          checked={state.overlays}
          onChange={(v) => save({ ...current, overlays: v })}
        />
      )}
      {pack.persona && (
        <SettingsToggle
          label={i18nT('pages.settings.displayPanel.theme_experience_persona')}
          checked={state.persona}
          onChange={(v) => {
            // On is stored wanted but unbacked, so the consent dialog asks about it.
            if (v) store({ ...current, persona: true }, null)
            else save({ ...current, persona: false })
          }}
        />
      )}
      {/* A toggle applies for this tab even when it cannot be stored, so the hand-off loses nothing. */}
      <ErrorNotice
        askAgent
        message={failedSlug === slug ? i18nT('components.themeExperienceLayer.save_failed_storage') : null}
      />
    </div>
  )
}
