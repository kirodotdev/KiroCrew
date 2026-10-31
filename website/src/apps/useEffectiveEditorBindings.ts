/**
 * The editor bindings enabled apps contribute, as declared and as in force after
 * the reader's overrides, with each app's display name. Settings → Shortcuts and
 * the shortcuts reference both read this.
 */
import { useMemo } from 'react'
import { appDisplayName } from '../components/appstore/appManifest'
import { useInstalledApps } from '../hooks/panelTabRegistry'
import { i18nT } from '../i18n/t'
import { isMac } from '../utils/platform'
import { contributedEditorBindings, type ContributedEditorBinding, type EditorBindingAppRecord } from './editorBindingContributions'
import { applyEditorBindingOverrides, type EditorBindingOverrides } from './editorBindingOverrides'
import { triggerCaps, type EditorBindingTrigger } from './editorBindingTriggers'
import { useEditorBindingOverrides } from './useEditorBindingOverrides'

export interface EffectiveEditorBindings {
  declared: ContributedEditorBinding[]
  effective: ContributedEditorBinding[]
  overrides: EditorBindingOverrides
  appTitle: (app: string) => string
}

export function useEffectiveEditorBindings(): EffectiveEditorBindings {
  const { apps } = useInstalledApps()
  const overrides = useEditorBindingOverrides()
  const declared = useMemo(() => contributedEditorBindings(apps as unknown as EditorBindingAppRecord[]), [apps])
  const effective = useMemo(() => applyEditorBindingOverrides(declared, overrides), [declared, overrides])
  const appTitle = useMemo(() => {
    const names = new Map<string, string>()
    for (const app of apps) names.set(app.name, appDisplayName(app))
    return (app: string) => names.get(app) ?? app
  }, [apps])
  return { declared, effective, overrides, appTitle }
}

/** A trigger's key caps for this platform, the way the dashboard draws its other shortcuts. */
export function editorBindingCaps(trigger: EditorBindingTrigger): string[] {
  return triggerCaps(trigger, isMac, i18nT('pages.settings.shortcutsPanel.click_cap'))
}
