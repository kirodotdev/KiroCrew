// A single guide step: short bold title (`t`), body (`do`, rich), a dark cmd
// block, and a collapsible "what to expect".
import { useTranslation } from 'react-i18next'
import { Eye } from 'lucide-react'
import CodeBlock from './CodeBlock'
import MdInline from './MdInline'
import { type Step, pickL, pickStepDo, resolveVariant } from './api'

export default function StepBlock({
  step,
  ids,
  onSelect,
  lang,
}: {
  step: Step
  ids: Set<string>
  onSelect: (id: string) => void
  lang: string
}) {
  const { t } = useTranslation()
  const title = pickL(step.t, step.t_zh, lang)
  const body = pickStepDo(step, lang)
  const cmd = resolveVariant(step.cmd)
  const expect = resolveVariant(step.expect)
  return (
    <li
      className="rounded-lg p-3"
      style={{ border: '1px solid var(--border)', background: 'var(--card)' }}
    >
      {title && <div className="text-sm font-medium">{title}</div>}
      {body && (
        <div className={title ? 'mt-1' : ''}>
          <MdInline text={body} ids={ids} onSelect={onSelect} />
        </div>
      )}
      {cmd && <CodeBlock code={cmd} />}
      {expect && (
        <details className="mt-1.5">
          <summary
            className="flex items-center gap-1.5 text-xs cursor-pointer focus-visible:ring-1 focus-visible:ring-[var(--accent)]"
            style={{ color: 'var(--muted)' }}
          >
            <Eye size={13} />
            <span className="font-semibold">{t('apps.guide.expectLabel')}</span>
          </summary>
          <div className="mt-1 text-sm" style={{ color: 'var(--muted)' }}>
            <MdInline text={expect} ids={ids} onSelect={onSelect} />
          </div>
        </details>
      )}
    </li>
  )
}
