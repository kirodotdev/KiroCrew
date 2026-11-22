import type { ReactNode } from 'react'

import { cn } from '../../lib/utils'
import { IconButtonGroup } from '../ui'
import { Field, FieldContent, FieldGroup, FieldTitle } from './field'

/**
 * Read-only rows on shadcn's Field: a label column, the value, and actions
 * that appear on hover or keyboard focus (always on touch). `FieldRows` is
 * the field group the rows' responsive layout measures: label beside value
 * from its `@md` width, label above value below it.
 */
export function FieldRows({ children }: { children: ReactNode }) {
  return <FieldGroup className="gap-0">{children}</FieldGroup>
}

/** One row. `label` is optional so a run of rows can continue a group (a
 *  second website) without repeating it. `mono` sets the value in the code
 *  face, for names, references and commands. */
export function FieldRow({ label, children, actions, mono = false }: { label?: string; children: ReactNode; actions?: ReactNode; mono?: boolean }) {
  return (
    <Field
      orientation="responsive"
      className="group border-t border-border py-2.5 first:border-t-0 first:pt-0 last:pb-0 @md/field-group:[&>[data-slot=field-label]]:w-[140px] @md/field-group:[&>[data-slot=field-label]]:flex-none @md/field-group:has-[>[data-slot=field-content]]:items-center"
    >
      <FieldTitle className="text-[13px] font-normal text-muted">{label}</FieldTitle>
      <FieldContent className={cn('min-w-0 flex-row items-center gap-2.5 text-[13.5px] text-text-strong', mono && 'font-mono')}>
        {children}
        {actions && <IconButtonGroup reveal className="ml-auto shrink-0">{actions}</IconButtonGroup>}
      </FieldContent>
    </Field>
  )
}
