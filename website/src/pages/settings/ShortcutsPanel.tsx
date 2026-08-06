import { formatShortcut, IS_MAC, shortcutLabel } from '../../hooks/useKeyboardShortcuts'
import { SHORTCUT_GROUPS, ShortcutRow, KeyCapSequence, groupShortcuts, shortcutGroupLabel, useShortcutPrefs } from '../../components/ShortcutsModal'
import { SettingsSection, SettingsCard, SettingsToggle, SettingsButtonGroup } from '../../components/settings'
import { useQuickSearchShortcut } from '../../hooks/useQuickSearchShortcut'
import { formatChordKeys, type QuickSearchMode } from '../../lib/quickSearchShortcut'
import { Btn } from '../../components/ui'

import { i18nT } from '../../i18n/t'
/**
 * Editor for the Search Everywhere activation shortcut. A preset picker
 * (double-Shift / ⌘K·Ctrl+K / custom) backed by {@link useQuickSearchShortcut},
 * with an inline recorder revealed for the `custom` preset. Selecting `custom`
 * enters recording without persisting, so the previous binding stays live until
 * a valid chord is captured (Escape cancels).
 */
function SearchEverywhereConfig() {
  const { config, recording, selectMode, startRecording, cancelRecording } = useQuickSearchShortcut()
  // While recording, surface `custom` as the active preset even though nothing
  // is persisted yet, so the picker reflects the pending choice.
  const activeMode = recording ? 'custom' : config.mode
  const customCaps = config.mode === 'custom' && config.custom ? formatChordKeys(config.custom) : []

  return (
    <>
      <SettingsButtonGroup
        label={i18nT('components.shortcutsModal.search_everywhere')}
        value={activeMode}
        options={[
          { value: 'double-shift', label: i18nT('pages.settings.shortcutsPanel.preset_double_shift') },
          { value: 'mod-k', label: i18nT('pages.settings.shortcutsPanel.preset_mod_k') },
          { value: 'custom', label: i18nT('pages.settings.shortcutsPanel.preset_custom') },
        ]}
        onChange={(v) => selectMode(v as QuickSearchMode)}
      />
      {activeMode === 'custom' && (
        <div className="flex items-center gap-2 flex-wrap pl-1 pb-1">
          <Btn
            onClick={recording ? cancelRecording : startRecording}
            aria-label={i18nT('pages.settings.shortcutsPanel.record_prompt')}
            className={recording ? 'border-accent text-accent' : ''}
          >
            {recording ? (
              <span className="flex items-center gap-1.5">
                <span className="w-2 h-2 rounded-full bg-accent animate-pulse" aria-hidden="true" />
                {i18nT('pages.settings.shortcutsPanel.record_prompt')}
              </span>
            ) : customCaps.length > 0 ? (
              <span className="flex items-center gap-1"><KeyCapSequence caps={customCaps} plus /></span>
            ) : (
              i18nT('pages.settings.shortcutsPanel.record_prompt')
            )}
          </Btn>
          <span className="text-[12px] text-muted">{i18nT('pages.settings.shortcutsPanel.custom_hint')}</span>
        </div>
      )}
    </>
  )
}

/**
 * Settings → Shortcuts. Same data + preference state as the Alt+K
 * `ShortcutsModal` (shared primitives from ShortcutsModal.tsx), presented in
 * the standard Settings layout: a `SettingsSection` header per shortcut group
 * with the rows in a `SettingsCard` container. Gives keyboard shortcuts a
 * discoverable, permanent home in Settings.
 */
export function ShortcutsPanel() {
  const { enabled, macCtrl, toggle, toggleMacCtrl } = useShortcutPrefs()

  return (
    <div className="max-w-2xl">
      <SettingsSection title={i18nT('pages.settings.shortcutsPanel.preferences')} />
      <SettingsCard>
        <SettingsToggle
          label={i18nT('pages.settings.shortcutsPanel.enable_shortcuts')}
          description={i18nT('pages.settings.shortcutsPanel.turn_keyboard_shortcuts_on_or_off_globally', { mod: IS_MAC ? '⌥' : 'Alt' })}
          checked={enabled}
          onChange={toggle}
        />
        {IS_MAC && (
          <SettingsToggle
            label={i18nT('pages.settings.shortcutsPanel.use_ctrl_not_option_for_chat_1_9')}
            description={i18nT('pages.settings.shortcutsPanel.bind_chat_tab_switching_to_ctrl_digit_instead_of')}
            checked={macCtrl}
            onChange={toggleMacCtrl}
          />
        )}
      </SettingsCard>
      {SHORTCUT_GROUPS.map(group => {
        const entries = groupShortcuts(group, macCtrl)
        if (entries.length === 0) return null
        return (
          <div key={group}>
            <SettingsSection title={shortcutGroupLabel(group)} />
            <SettingsCard>
              {entries.map(s => (
                <ShortcutRow key={s.id} label={shortcutLabel(s)} keys={formatShortcut(s).split(' + ')} />
              ))}
            </SettingsCard>
          </div>
        )
      })}
      <SettingsSection title={i18nT('pages.settings.shortcutsPanel.search')} />
      <SettingsCard>
        <SearchEverywhereConfig />
      </SettingsCard>
    </div>
  )
}
