/**
 * Evidence for the reuse-path COPY-VARIANT confirm dialog (RunInTerminalConfirm).
 *
 * THE STATE: when the reuse-current-terminal setting is on, confirming a
 * Run-in-terminal action COPIES the command to the clipboard for the user to
 * paste rather than running it in a new tab. To avoid promising an action it
 * will not take, RunInTerminalConfirm switches its title, body and primary
 * button to Copy semantics when `willCopy` is true:
 *   - title  -> components.runInTerminalConfirm.title_copy  ("Copy to terminal")
 *   - body   -> components.runInTerminalConfirm.body_copy    ("The command is
 *               copied for you to paste into the terminal.")
 *   - button -> components.runInTerminalConfirm.copy         ("Copy")
 *
 * This mounts the REAL RunInTerminalConfirm with `open` and `willCopy` true
 * against the real stylesheet, theme tokens and live i18n catalog. Nothing here
 * re-implements the dialog copy or its classes, so the frame proves exactly the
 * copy-variant the diff ships. The capture script asserts the rendered button
 * text and title before writing, so a run can never emit a misleading frame.
 *
 * Query string: ?theme=dark|light
 */
import { createRoot } from 'react-dom/client'
import RunInTerminalConfirm from '../src/components/RunInTerminalConfirm'
import { initI18n } from '../src/i18n/all'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') === 'light' ? 'light' : 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

await initI18n()

createRoot(document.getElementById('root')!).render(
  <div
    data-capture-root
    style={{
      width: 620,
      minHeight: 260,
      padding: 24,
      display: 'flex',
      alignItems: 'flex-start',
      justifyContent: 'center',
      background: 'var(--bg)',
      color: 'var(--text)',
      fontSize: 14,
    }}
  >
    <RunInTerminalConfirm
      open
      willCopy
      command="npm run build"
      onConfirm={() => {}}
      onCancel={() => {}}
    />
  </div>,
)
