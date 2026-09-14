/**
 * Isolated capture entry for the artifact comment popover's Copy affordance.
 *
 * WHY ISOLATED: on the real artifact page the popover opens off a mouseup over
 * rendered prose, which needs the app shell, a live gateway and a saved
 * artifact. The popover is a self-contained component, so mounting it over a
 * block of artifact-styled prose -- with the real theme CSS and the real i18n
 * catalog -- is faithful to what the page renders.
 *
 * The clipboard is the one seam stubbed: `navigator.clipboard.writeText` is a
 * recorder so the capture script can assert EXACTLY what a click put on the
 * clipboard, and can be switched to refuse (`?clipboard=refuse`) so the
 * copy-failed notice is photographed from a real refusal path.
 *
 * Query string: ?theme=light&clipboard=refuse
 */
import { createRoot } from 'react-dom/client'

// Initialise i18next exactly as main.tsx does. Importing the module only DEFINES
// initI18n -- without calling it every label in the frame is blank.
import { initI18n } from '../src/i18n'
import { CommentPopover } from '../src/components/CommentOverlay'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const theme = params.get('theme') || 'dark'
const refuse = params.get('clipboard') === 'refuse'

document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

declare global {
  interface Window { __copied: string[]; __submitted: string[] }
}
window.__copied = []
window.__submitted = []
Object.defineProperty(navigator, 'clipboard', {
  configurable: true,
  value: {
    writeText: async (text: string) => {
      if (refuse) throw new Error('denied')
      window.__copied.push(text)
    },
  },
})
// The execCommand fallback must refuse too, or copyToClipboard() still succeeds.
if (refuse) document.execCommand = () => false

/** The passage the user swept. Leading/trailing whitespace is deliberate: the
 *  comment anchor is trimmed for matching, the clipboard must get it verbatim. */
const SELECTED = ' the popover opens the moment the mouse button comes up '

initI18n('en')

createRoot(document.getElementById('root')!).render(
  <div data-capture-root className="bg-bg text-text relative" style={{ width: 560, height: 260, padding: 24 }}>
    <div className="font-body text-[15px] leading-7">
      <p>
        Selecting text on an artifact is how you leave a comment for the agent:
        <mark className="bg-accent-subtle text-text rounded-sm px-0.5">{SELECTED}</mark>
        and the box is already focused, so you can start typing.
      </p>
    </div>
    <CommentPopover
      x={40}
      y={76}
      copyText={SELECTED}
      onSubmit={text => { window.__submitted.push(text) }}
      onCancel={() => undefined}
    />
  </div>,
)
