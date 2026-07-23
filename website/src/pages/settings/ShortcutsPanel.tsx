import { formatShortcut, IS_MAC } from '../../hooks/useKeyboardShortcuts'
import { SHORTCUT_GROUPS, ShortcutRow, SearchEverywhereRow, groupShortcuts, useShortcutPrefs } from '../../components/ShortcutsModal'
import { SettingsSection, SettingsCard, SettingsToggle } from '../../components/settings'

/**
 * Settings → Shortcuts. Same data + preference state as the Alt+K
 * `ShortcutsModal` (shared primitives from ShortcutsModal.tsx), presented in
 * the standard Settings layout: a `SettingsSection` header per shortcut group
 * with the rows in a `SettingsCard` container. Gives keyboard shortcuts a
 * discoverable, permanent home now that the left-nav Shortcuts row was
 * removed in the nav-IA restructure.
 */
export function ShortcutsPanel() {
  const { enabled, macCtrl, toggle, toggleMacCtrl } = useShortcutPrefs()

  return (
    <div className="max-w-2xl">
      <SettingsSection title="Preferences" />
      <SettingsCard>
        <SettingsToggle
          label="Enable shortcuts"
          description={`Turn keyboard shortcuts on or off globally — ${IS_MAC ? '⌥' : 'Alt'} + K (this reference) always works`}
          checked={enabled}
          onChange={toggle}
        />
        {IS_MAC && (
          <SettingsToggle
            label="Use ⌃ Ctrl (not ⌥ Option) for chat 1–9"
            description="Bind chat-tab switching to Ctrl+digit instead of Option+digit"
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
            <SettingsSection title={group} />
            <SettingsCard>
              {entries.map(s => (
                <ShortcutRow key={s.id} label={s.label} keys={formatShortcut(s).split(' + ')} />
              ))}
            </SettingsCard>
          </div>
        )
      })}
      <SettingsSection title="Search" />
      <SettingsCard>
        <SearchEverywhereRow />
      </SettingsCard>
    </div>
  )
}
