/**
 * Evidence for the shipped Settings → Display → Terminal "Reuse the current
 * terminal" toggle (issue #11641) — the surface the UX review needs to verify
 * against the shipped help text.
 *
 * THE STATE: the toggle's description must agree with the always-copy behavior.
 * Earlier evidence captured a prior revision whose description ended "it opens a
 * new terminal and runs the command there"; the shipped behavior (ChatPage:
 * "Reuse-on ALWAYS copies — never runs") contradicts that, and the frontend
 * `terminal_reuse_current_desc` string was corrected to "it still copies the
 * command for you to paste — it is never run for you."
 *
 * This mounts the REAL `SettingsToggle` with the SAME label/description props
 * the shipped DisplayPanel passes — read live from the i18n catalog
 * (pages.settings.displayPanel.terminal_reuse_current[/_desc]) — against the
 * real stylesheet and theme tokens. Nothing here re-implements the row or its
 * copy, so the frame proves exactly the description the diff ships. The capture
 * script asserts the rendered description text before writing, so a run can
 * never emit a stale frame.
 *
 * Query string: ?theme=dark|light&on=1  (on=1 renders the checked state)
 */
import { createRoot } from 'react-dom/client'
import { SettingsToggle } from '../src/components/settings'
import { initI18n } from '../src/i18n/all'
import { i18nT } from '../src/i18n/t'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
const checked = params.get('on') === '1'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

await initI18n()

createRoot(document.getElementById('root')!).render(
  <div
    data-capture-root
    style={{
      width: 640,
      minHeight: 140,
      padding: 24,
      background: 'var(--bg)',
      color: 'var(--text)',
      fontSize: 14,
    }}
  >
    <SettingsToggle
      label={i18nT('pages.settings.displayPanel.terminal_reuse_current')}
      description={i18nT('pages.settings.displayPanel.terminal_reuse_current_desc')}
      checked={checked}
      onChange={() => {}}
      configKey="dashboard.terminal.reuse_current"
    />
  </div>,
)
