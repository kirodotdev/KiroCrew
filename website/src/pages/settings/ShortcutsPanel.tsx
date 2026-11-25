import { IS_MAC } from '../../hooks/useKeyboardShortcuts'
import { SHORTCUT_GROUPS, ShortcutGroupRows, KeyCapSequence, GlobalHotkeyRow, GlobalHotkeyNotice, PANEL_TOGGLE_LABEL_KEY, groupShortcuts, shortcutGroupLabel, shortcutsHelpText, useShortcutPrefs } from '../../components/ShortcutsModal'
import { SettingsSection, SettingsCard, SettingsToggle, SettingsButtonGroup } from '../../components/settings'
import { useQuickSearchShortcut } from '../../hooks/useQuickSearchShortcut'
import { usePanelToggleShortcuts } from '../../hooks/usePanelToggleShortcuts'
import { canRecordGlobalHotkey, useGlobalHotkey, useGlobalHotkeyRecorder } from '../../hooks/useGlobalHotkey'
import { chordReachable, formatChordKeys, type QuickSearchMode } from '../../lib/quickSearchShortcut'
import { formatAcceleratorKeys, type GlobalHotkeyInfo } from '../../lib/globalHotkey'
import { useKeyboardLayout } from '../../hooks/useNavShortcutHint'
import { PANEL_TOGGLE_IDS, PANEL_TOGGLES_SKIPPING_SHELL, TERMINAL_PANEL_TOGGLE_IDS } from '../../lib/panelToggleShortcuts'
import { useTerminalEnabled } from '../../utils/terminalRegistry'
import { Btn } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'

import { i18nT } from '../../i18n/t'
/**
 * The button every shortcut editor here records with: the bound chord's caps,
 * or the record prompt when nothing is bound and while recording (then with a
 * pulsing dot). Clicking starts recording, or cancels it while recording.
 */
function RecordButton({ recording, caps, onClick }: { recording: boolean; caps: string[]; onClick: () => void }) {
  return (
    <Btn
      onClick={onClick}
      aria-label={i18nT('pages.settings.shortcutsPanel.record_prompt')}
      className={recording ? 'border-accent text-accent' : ''}
    >
      {recording ? (
        <span className="flex items-center gap-1.5">
          <span className="w-2 h-2 rounded-full bg-accent animate-pulse" aria-hidden="true" />
          {i18nT('pages.settings.shortcutsPanel.record_prompt')}
        </span>
      ) : caps.length > 0 ? (
        <span className="flex items-center gap-1"><KeyCapSequence caps={caps} plus /></span>
      ) : (
        i18nT('pages.settings.shortcutsPanel.record_prompt')
      )}
    </Btn>
  )
}

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
  const customCaps = config.mode === 'custom' && config.custom && chordReachable(config.custom) ? formatChordKeys(config.custom) : []

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
        <div className="flex items-center gap-2 flex-wrap">
          <RecordButton recording={recording} caps={customCaps} onClick={recording ? cancelRecording : startRecording} />
          <span className="text-[12px] text-muted">{i18nT('pages.settings.shortcutsPanel.custom_hint')}</span>
        </div>
      )}
    </>
  )
}

/**
 * Editor for the three panel-toggle shortcuts (left sidebar, session list, side
 * panel). Each row records a new chord (recording defers persistence, so the
 * current binding stays live until a valid chord lands; Escape cancels) or
 * clears the binding to unbound. Backed by {@link usePanelToggleShortcuts}.
 */
function PanelToggleConfig() {
  const { bindings, recordingId, startRecording, cancelRecording, clear } = usePanelToggleShortcuts()
  // Reactive, not a one-shot read — see the same call in ShortcutsModal.
  const terminalEnabled = useTerminalEnabled()
  return (
    <>
      {PANEL_TOGGLE_IDS.filter(id => !TERMINAL_PANEL_TOGGLE_IDS.has(id) || terminalEnabled).map(id => {
        const chord = bindings[id]
        const recording = recordingId === id
        const caps = chord && chordReachable(chord) ? formatChordKeys(chord) : []
        return (
          <div key={id}>
            <div className="flex items-center justify-between gap-2">
              {/* A div, not a span: the label and the button caption are separate
                  catalog units, and a block label ends the inline text run so the
                  i18n render gate never sees them joined into one string. */}
              <div className="text-[13px] text-text">{i18nT(PANEL_TOGGLE_LABEL_KEY[id])}</div>
              <span className="flex items-center gap-2">
                <RecordButton recording={recording} caps={caps} onClick={recording ? cancelRecording : () => startRecording(id)} />
                {chord && !recording && (
                  <Btn onClick={() => clear(id)} aria-label={i18nT('pages.settings.shortcutsPanel.clear_shortcut')}>
                    {i18nT('pages.settings.shortcutsPanel.clear_shortcut')}
                  </Btn>
                )}
              </span>
            </div>
            {/* A skip-shell panel's chord is taken from the PTY by design, so the
                cost of the binding lands on the SHELL, not on the dashboard: a
                recorded Ctrl+C or Ctrl+R would stop reaching the shell in every
                session. The recorder cannot refuse those (a shell key is not
                invalid, and which keys matter depends on the user's shell and
                bindings), so the trade is disclosed here, at the moment it is
                made, rather than left to be discovered by a broken Ctrl+C. */}
            {PANEL_TOGGLES_SKIPPING_SHELL.has(id) && (
              <div className="text-[12px] text-muted pt-1">{i18nT(IS_MAC ? 'pages.settings.shortcutsPanel.skips_shell_hint_mac' : 'pages.settings.shortcutsPanel.skips_shell_hint')}</div>
            )}
          </div>
        )
      })}
      <div className="text-[12px] text-muted">{i18nT('pages.settings.shortcutsPanel.custom_hint')}</div>
    </>
  )
}

/**
 * Editor for the desktop shell's system-wide summon hotkey: record a chord or
 * clear it to turn the hotkey off, like the panel toggles above. The shell
 * binds the change at once (no restart) and the button shows what is ACTUALLY
 * bound, with {@link GlobalHotkeyNotice} when that is not what was recorded.
 * A window that cannot set the hotkey (connected to a remote host, or an older
 * shell) keeps the read-only row and the config-file hint.
 */
function SummonHotkeyConfig({ hotkey }: { hotkey: GlobalHotkeyInfo }) {
  const { recording, startRecording, cancelRecording, clear, failed } = useGlobalHotkeyRecorder()
  if (!canRecordGlobalHotkey()) {
    return (
      <>
        <GlobalHotkeyRow />
        <div className="text-[12px] text-muted">{i18nT(hotkey.accelerator ? 'components.shortcutsModal.global_hotkey_hint' : 'components.shortcutsModal.global_hotkey_hint_off')}</div>
      </>
    )
  }
  const { accelerator, requested } = hotkey
  return (
    <>
      {/* One card row for the controls and their notices, so the card draws no divider between them. */}
      <div>
      <div className="flex items-center justify-between gap-2">
        {/* Block elements for the label and the tag: separate catalog units, as in PanelToggleConfig. */}
        <div className="text-[13px] text-text">{i18nT('components.shortcutsModal.show_or_focus_the_kiro_crew_window')}</div>
        <span className="flex items-center gap-2">
          {/* The shell holds this chord, so "Enable shortcuts" above does not turn it off. */}
          {accelerator && !recording && (
            <div className="text-muted text-[10px] uppercase tracking-wider">{i18nT('components.shortcutsModal.always_works')}</div>
          )}
          <RecordButton recording={recording} caps={formatAcceleratorKeys(accelerator, IS_MAC)} onClick={recording ? cancelRecording : startRecording} />
          {/* `requested` too: a chord that could not be bound is still stored, and Clear is how to drop it. */}
          {(accelerator || requested) && !recording && (
            <Btn onClick={clear} aria-label={i18nT('pages.settings.shortcutsPanel.clear_shortcut')}>
              {i18nT('pages.settings.shortcutsPanel.clear_shortcut')}
            </Btn>
          )}
        </span>
      </div>
      <GlobalHotkeyNotice hotkey={hotkey} className="pt-1" />
      {failed && (
        <ErrorNotice variant="inline" message={i18nT('components.shortcutsModal.global_hotkey_save_failed')} askAgent testId="global-hotkey-save-error" className="pt-1" />
      )}
      </div>
      <div className="text-[12px] text-muted">{i18nT(recording ? 'pages.settings.shortcutsPanel.custom_hint' : 'components.shortcutsModal.global_hotkey_hint_record')}</div>
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
  useKeyboardLayout()
  const { enabled, macCtrl, toggle, toggleMacCtrl, saveError } = useShortcutPrefs()
  const globalHotkey = useGlobalHotkey()

  return (
    <div>
      <SettingsSection title={i18nT('pages.settings.shortcutsPanel.preferences')} />
      <SettingsCard>
        <SettingsToggle
          label={i18nT('pages.settings.shortcutsPanel.enable_shortcuts')}
          description={i18nT('pages.settings.shortcutsPanel.turn_keyboard_shortcuts_on_or_off_globally', { chord: shortcutsHelpText() })}
          checked={enabled}
          onChange={toggle}
        />
        {IS_MAC && (
          <SettingsToggle
            label={i18nT('pages.settings.shortcutsPanel.use_ctrl_not_option_for_chat_1_9')}
            hint={i18nT('pages.settings.shortcutsPanel.bind_chat_tab_switching_to_ctrl_digit_instead_of')}
            checked={macCtrl}
            onChange={toggleMacCtrl}
          />
        )}
        {saveError && (
          <ErrorNotice variant="inline" message={saveError} askAgent testId="shortcut-pref-save-error" className="pb-2" />
        )}
      </SettingsCard>
      {SHORTCUT_GROUPS.map((group, gi) => {
        const entries = groupShortcuts(group, macCtrl)
        if (entries.length === 0) return null
        return (
          <div key={group}>
            <SettingsSection title={shortcutGroupLabel(group)} />
            {/* Ordinal from the full group list, not the rendered subset: a gap
                where a group rendered null only stretches the stagger, while a
                compacted ordinal would shift every later card's delay whenever a
                group toggles. */}
            <SettingsCard index={gi + 1}>
              <ShortcutGroupRows entries={entries} hintClass="text-[12px] text-muted" />
            </SettingsCard>
          </div>
        )
      })}
      <SettingsSection title={i18nT('pages.settings.shortcutsPanel.search')} />
      <SettingsCard index={SHORTCUT_GROUPS.length + 1}>
        <SearchEverywhereConfig />
      </SettingsCard>
      <SettingsSection title={i18nT('pages.settings.shortcutsPanel.panel_toggles')} />
      <SettingsCard index={SHORTCUT_GROUPS.length + 2}>
        <PanelToggleConfig />
      </SettingsCard>
      {globalHotkey && (
        <div>
          <SettingsSection title={i18nT('components.shortcutsModal.desktop_app')} />
          <SettingsCard>
            <SummonHotkeyConfig hotkey={globalHotkey} />
          </SettingsCard>
        </div>
      )}
    </div>
  )
}
