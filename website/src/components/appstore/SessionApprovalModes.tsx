import { i18nT } from '../../i18n/t'

/** The three per-chat approval modes a `permissions.sessionApproval` app may
 *  set, each with its one-line gloss from the approval-mode picker. YOLO is a
 *  process-global override and stays dashboard-only, so it is not listed.
 *
 *  Bare pills read as buttons and carried no meaning cold -- a reader could not
 *  tell mode-"Trust" from the consent dialog's own "Trust this app" verb, and
 *  Badge pills afford a click that does nothing. Each row is the mode name in
 *  plain bold text plus the same description the chat footer picker uses, so the
 *  two surfaces teach the same vocabulary and nothing here looks selectable.
 *  Static literal keys keep the dead-key and dynamic-key i18n guards happy. */
export default function SessionApprovalModes({ label, className = '' }: { label: string; className?: string }) {
  const rows = [
    { label: i18nT('components.approvalModePicker.normal_label'), desc: i18nT('components.approvalModePicker.normal_desc') },
    { label: i18nT('components.approvalModePicker.reads_label'), desc: i18nT('components.approvalModePicker.reads_desc') },
    { label: i18nT('components.approvalModePicker.trust_label'), modeHint: i18nT('components.approvalModePicker.chat_mode_hint'), desc: i18nT('components.approvalModePicker.trust_desc') },
  ]
  return (
    <div className={className}>
      <div className="text-muted">{label}</div>
      <ul className="mt-1 flex flex-col gap-1">
        {rows.map(row => (
          <li key={row.label} className="flex items-baseline gap-2">
            <span className="font-semibold text-text shrink-0">{row.label}{row.modeHint ? <> ({row.modeHint})</> : null}</span>
            <span className="text-muted leading-relaxed">{row.desc}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}
