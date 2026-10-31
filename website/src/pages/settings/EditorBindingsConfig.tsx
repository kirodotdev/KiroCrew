/**
 * Settings → Shortcuts → Editor shortcuts: one row per binding an enabled app
 * contributes to the side panel's code editor, with the chord in force, a
 * recorder, Clear (unbind) and Reset (back to the app's own chord). Backed by
 * {@link useEditorBindingRecorder}; the editor reads the same overrides, so a
 * change applies to an open file at once.
 */
import { useCallback, useMemo } from 'react'
import type { ContributedEditorBinding } from '../../apps/editorBindingContributions'
import { overrideKey } from '../../apps/editorBindingOverrides'
import { dashboardShortcutFor, parseCanonicalTrigger } from '../../apps/editorBindingTriggers'
import { DEFAULT_SHORTCUTS, shortcutLabel } from '../../hooks/useKeyboardShortcuts'
import { isMac } from '../../utils/platform'
import { useEditorBindingRecorder, type RecordRefusal } from '../../apps/useEditorBindingOverrides'
import { editorBindingCaps as caps, useEffectiveEditorBindings } from '../../apps/useEffectiveEditorBindings'
import { KeyCapSequence } from '../../components/ShortcutsModal'
import { Btn } from '../../components/ui'
import { SettingsCard, SettingsSection } from '../../components/settings'
import { i18nT } from '../../i18n/t'

/** Whether two bindings can fire on the same file: no filter means every file. */
function extensionsOverlap(a: ContributedEditorBinding, b: ContributedEditorBinding): boolean {
  const ea = a.when.extensions
  const eb = b.when.extensions
  if (!ea.length || !eb.length) return true
  return ea.some(e => eb.includes(e))
}

function refusalText(refusal: RecordRefusal): string {
  const trigger = parseCanonicalTrigger(refusal.canonical)
  const chord = trigger ? caps(trigger).join('+') : refusal.canonical
  return refusal.kind === 'reserved'
    ? i18nT('pages.settings.shortcutsPanel.chord_reserved', { chord })
    : i18nT('pages.settings.shortcutsPanel.chord_in_use', { chord, label: refusal.by })
}

/** The section, or nothing when no enabled app contributes a binding. `index` staggers the card like its siblings. */
export function EditorBindingsConfig({ index }: { index?: number }) {
  const { declared, effective, overrides, appTitle } = useEffectiveEditorBindings()
  const inUse = useCallback((canonical: string, except: string) => {
    // A dashboard shortcut: the editor would take it away while it has focus.
    const shortcut = dashboardShortcutFor(canonical, isMac)
    if (shortcut !== null) {
      const def = DEFAULT_SHORTCUTS.find(d => d.id === shortcut)
      return def ? shortcutLabel(def) : shortcut
    }
    const self = declared.find(b => overrideKey(b.app, b.id) === except)
    const other = effective.find(b =>
      overrideKey(b.app, b.id) !== except
      && b.trigger.canonical === canonical
      && (!self || extensionsOverlap(self, b)))
    return other ? other.label : null
  }, [declared, effective])
  const recorder = useEditorBindingRecorder(inUse)

  const byApp = useMemo(() => {
    const groups: { app: string; title: string; bindings: ContributedEditorBinding[] }[] = []
    for (const binding of declared) {
      let group = groups.find(g => g.app === binding.app)
      if (!group) {
        group = { app: binding.app, title: appTitle(binding.app), bindings: [] }
        groups.push(group)
      }
      group.bindings.push(binding)
    }
    return groups
  }, [appTitle, declared])

  if (byApp.length === 0) return null
  return (
    <div>
      <SettingsSection title={i18nT('pages.settings.shortcutsPanel.editor_bindings')} />
      <SettingsCard index={index}>
      {byApp.map(group => (
        <div key={group.app}>
          <div className="text-[12px] text-muted px-2 pt-1">{group.title}</div>
          {group.bindings.map(binding => {
            const key = overrideKey(binding.app, binding.id)
            const current = effective.find(b => overrideKey(b.app, b.id) === key)
            const overridden = Object.prototype.hasOwnProperty.call(overrides, key)
            const recording = recorder.recording === key
            return (
              <div key={key}>
                <div className="flex items-center justify-between gap-2 py-1.5 px-2 rounded-md">
                  <div className="text-[13px] text-text">{binding.label}</div>
                  <span className="flex items-center gap-2">
                    <Btn
                      onClick={recording ? recorder.cancel : () => recorder.start(binding.app, binding.id)}
                      aria-label={i18nT('pages.settings.shortcutsPanel.record_prompt')}
                      className={recording ? 'border-accent text-accent' : ''}
                    >
                      {recording ? (
                        <span className="flex items-center gap-1.5">
                          <span className="w-2 h-2 rounded-full bg-accent animate-pulse" aria-hidden="true" />
                          {i18nT('pages.settings.shortcutsPanel.record_prompt')}
                        </span>
                      ) : current ? (
                        <span className="flex items-center gap-1"><KeyCapSequence caps={caps(current.trigger)} plus /></span>
                      ) : (
                        i18nT('pages.settings.shortcutsPanel.unbound')
                      )}
                    </Btn>
                    {current && !recording && (
                      <Btn onClick={() => recorder.clear(binding.app, binding.id)}>
                        {i18nT('pages.settings.shortcutsPanel.clear_shortcut')}
                      </Btn>
                    )}
                    {overridden && !recording && (
                      <Btn onClick={() => recorder.reset(binding.app, binding.id)}>
                        {i18nT('pages.settings.shortcutsPanel.reset_shortcut')}
                      </Btn>
                    )}
                  </span>
                </div>
                {recording && recorder.refusal && (
                  <div role="status" className="text-[12px] text-danger px-2 pb-1">{refusalText(recorder.refusal)}</div>
                )}
              </div>
            )
          })}
        </div>
      ))}
      <div className="text-[12px] text-muted px-2 pt-1">{i18nT('pages.settings.shortcutsPanel.editor_bindings_hint')}</div>
      </SettingsCard>
    </div>
  )
}
