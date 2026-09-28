/**
 * Verification entry for shared ShellAside layout changes: renders the aside
 * with each consumer flow's own footnote copy so a change to the shell is
 * checked against every screen that ships it, not just the KAS gate. `?key=<i18n key>&lang=<locale>` selects the copy.
 */
import { createRoot } from 'react-dom/client'

import { ShellAside } from '../src/components/OnboardingChapterShell'
import { initI18n } from '../src/i18n/all'
import { i18nT } from '../src/i18n/t'
import '../src/index.css'

const params = new URLSearchParams(location.search)
const lang = params.get('lang') || 'en'
const key = params.get('key') || 'components.kasLogin.aside_footnote'
const shellW = Number(params.get('w') || 1150)

document.documentElement.setAttribute('data-theme', 'kiro-dark')
initI18n(lang)

createRoot(document.getElementById('root')!).render(
  <div className="flex h-screen bg-bg">
    {/* Inline styles: capture/ sits outside Tailwind's content globs, so
        arbitrary-value classes written here never compile. 1150px wide makes
        the aside's own sm:w-[36%] resolve to its production ~414px. */}
    <div className="flex m-auto overflow-hidden rounded-2xl" style={{ width: shellW, height: 760 }}>
      <ShellAside
        copy={{
          ariaLabel: 'aside',
          panelHeadline: i18nT('components.kasLogin.aside_headline'),
          panelBody: i18nT('components.kasLogin.aside_body'),
          panelFootnote: i18nT(key),
        }}
      />
      <div className="flex-1 bg-surface" />
    </div>
  </div>,
)
