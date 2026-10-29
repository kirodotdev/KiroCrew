import { useEffect, useMemo, useState } from 'react'

import { SettingsToggle } from '../../components/settings'
import { Btn } from '../../components/ui'
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
 * Overlays apply as soon as they are toggled. Turning Persona on shows the
 * persona text first and writes the grant only on Save, so what the backend
 * injects is the text the user read here; turning it off revokes the grant.
 * Stays in sync with ThemeExperienceLayer through the change and storage events.
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
  const [confirmingPersona, setConfirmingPersona] = useState(false)

  useEffect(() => {
    setConfirmingPersona(false)
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
  const save = (next: ExperienceChoice) => saveExperienceChoice(slug, next, next.persona ? pack.personaToken : null)

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
          checked={state.persona || confirmingPersona}
          onChange={(v) => {
            if (v) {
              setConfirmingPersona(true)
              return
            }
            setConfirmingPersona(false)
            // Also when the persona is wanted but unbacked (a changed persona), so
            // the layer stops asking about it.
            if (state.persona || state.ask.includes('persona')) save({ ...current, persona: false })
          }}
        />
      )}
      {pack.persona && confirmingPersona && !state.persona && (
        <div className="space-y-2 pb-1.5">
          {pack.personaText && (
            <pre
              className="max-h-40 overflow-auto whitespace-pre-wrap break-words rounded-md border border-border bg-bg-elevated p-2 text-[12px] text-text"
              aria-label={i18nT('pages.settings.displayPanel.theme_experience_persona_text')}
            >
              {pack.personaText}
            </pre>
          )}
          <Btn
            type="button"
            primary
            onClick={() => {
              setConfirmingPersona(false)
              save({ ...current, persona: true })
            }}
          >
            {i18nT('pages.settings.displayPanel.theme_experience_persona_save')}
          </Btn>
        </div>
      )}
    </div>
  )
}
