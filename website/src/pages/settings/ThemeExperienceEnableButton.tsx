import { useEffect, useId, useState } from 'react'

import { Btn } from '../../components/ui'
import type { CustomThemeData } from '../../hooks/useTheme'
import { i18nT } from '../../i18n/t'
import {
  DECLINE_PREFIX,
  THEME_EXPERIENCE_CHANGED_EVENT,
  clearExperienceDecline,
  hasDecline,
} from '../../utils/themeConsent'

/**
 * The way back after "Keep colors only". The declined pack stays selected, so
 * re-picking it in the theme select is a same-value no-op; this button clears
 * the stored decline instead, and ThemeExperienceLayer then shows its consent
 * dialog again. It never writes a grant: the user still confirms in that dialog.
 *
 * Shown only while the ACTIVE theme is a Level-2 pack with a stored decline.
 */
export function ThemeExperienceEnableButton({
  colorTheme,
  customThemeDataMap,
}: {
  colorTheme: string
  customThemeDataMap: Map<string, CustomThemeData>
}) {
  const hintId = useId()
  const slug = colorTheme.startsWith('custom-') ? colorTheme.slice('custom-'.length) : ''
  const isL2 = !!slug && (customThemeDataMap.get(slug)?.level ?? 0) >= 2
  const [declined, setDeclined] = useState(() => isL2 && hasDecline(slug))

  useEffect(() => {
    if (!isL2) {
      setDeclined(false)
      return
    }
    const reread = () => setDeclined(hasDecline(slug))
    reread()
    const onChanged = (e: Event) => {
      if ((e as CustomEvent<{ slug?: string }>).detail?.slug === slug) reread()
    }
    const onStorage = (e: StorageEvent) => {
      if (e.key === DECLINE_PREFIX + slug) reread()
    }
    window.addEventListener(THEME_EXPERIENCE_CHANGED_EVENT, onChanged)
    window.addEventListener('storage', onStorage)
    return () => {
      window.removeEventListener(THEME_EXPERIENCE_CHANGED_EVENT, onChanged)
      window.removeEventListener('storage', onStorage)
    }
  }, [slug, isL2])

  if (!isL2 || !declined) return null
  return (
    <div className="flex items-center gap-3">
      <Btn type="button" aria-describedby={hintId} onClick={() => clearExperienceDecline(slug)}>
        {i18nT('pages.settings.displayPanel.enable_theme_experience')}
      </Btn>
      <span id={hintId} className="text-[12px] text-muted leading-relaxed">
        {i18nT('pages.settings.displayPanel.enable_theme_experience_hint')}
      </span>
    </div>
  )
}
