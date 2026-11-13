/**
 * The "Always" answers remembered for one app's link opening, each with Forget.
 *
 * Shown on the app's detail page. Renders nothing when nothing is remembered.
 *
 * @module linkOpen/LinkOpenChoices
 */
import { useState } from 'react'
import { Btn } from '../components/ui'
import { i18nT } from '../i18n/t'
import { forgetChoice, rememberedChoices } from './linkOpenRegistry'

export default function LinkOpenChoices({ appName, label }: { appName: string; label: string }) {
  const [choices, setChoices] = useState(() => rememberedChoices(appName))
  if (!choices.length) return null
  return (
    <div className="mb-4 rounded-lg border border-border bg-bg-elevated p-3" data-testid="link-open-choices">
      <div className="mb-2 text-[13px] font-medium text-text">{i18nT('pages.appDetailPage.link_open_remembered')}</div>
      <ul className="flex flex-col gap-1.5">
        {choices.map((c) => (
          <li key={`${c.key}|${c.host}`} className="flex items-center justify-between gap-3 text-[13px]">
            <span className="min-w-0">
              <code className="mr-2 rounded border border-border px-1.5 py-0.5 text-[11px] text-text">{c.host}</code>
              <span className="text-muted">
                {c.choice === 'app'
                  ? i18nT('pages.appDetailPage.link_open_choice_app', { name: label })
                  : i18nT('pages.appDetailPage.link_open_choice_browser')}
              </span>
            </span>
            <Btn
              onClick={() => {
                forgetChoice(c.key, c.host)
                setChoices(rememberedChoices(appName))
              }}
            >
              {i18nT('pages.appDetailPage.link_open_forget')}
            </Btn>
          </li>
        ))}
      </ul>
    </div>
  )
}
