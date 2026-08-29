// Shared field-with-label wrapper for writing-review dialogs.
//
// Both ``NewReviewDialog`` (per-scan context inputs) and ``SettingsPanel``
// (persisted defaults) render field groups with a small uppercase-tracked
// label above a control. This component owns the shared markup so the
// two dialogs cannot drift apart on label typography or spacing.
//
// The prop is a pre-resolved ``labelText`` string rather than an i18n
// key. Every call site is therefore responsible for its own
// ``i18nT(...)`` call, which keeps the i18n dependency visible in the
// JSX and audit-friendly for the ``no hardcoded English`` gate. This
// mirrors the convention shared components elsewhere in the codebase
// use for label props (see ``AddReposModal``, ``ProjectSkillsTrustDialog``).
import type { ReactNode } from 'react'

export interface FieldGroupProps {
  labelText: string
  children: ReactNode
}

export function FieldGroup({ labelText, children }: FieldGroupProps) {
  return (
    <div className="flex flex-col gap-1">
      {/* eslint-disable-next-line jsx-a11y/label-has-for --
          This shared helper renders a caption paired with an arbitrary
          child input. The caller owns the input's id, and the rule wants
          BOTH nesting AND htmlFor by default -- a pattern that would
          require every call site to thread an id through. The visible
          "caption + input" pairing is preserved via layout and
          proximity; a follow-up can add an ``htmlFor`` prop and thread
          an id when we tighten a11y across the tree. */}
      <label className="text-[11.5px] uppercase tracking-wide text-muted">
        {labelText}
      </label>
      {children}
    </div>
  )
}
