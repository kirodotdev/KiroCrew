/**
 * Compile-time pins for `ErrorNotice`'s hand-off states. No product code
 * imports this file (only `ErrorNotice.noHandoffReason.test.tsx` renders it, for
 * coverage): `tsc -p tsconfig.app.json` checks it because it sits under `src/`
 * (test files are excluded from that program, so a pin in a test would never be
 * type-checked).
 *
 * Each `@ts-expect-error` line is a combination that must NOT compile. If the
 * `ErrorNoticeNoHandoff` union is loosened so one of them compiles, the
 * directive goes unused and the typecheck fails (TS2578).
 */
import ErrorNotice from './ErrorNotice'

// Values, not JSX string literals: nothing here is user-facing copy.
const message = 'm'
const reason = 'r'
const noop = () => {}

export function ErrorNoticeHandoffTypePins() {
  return (
    <>
      <ErrorNotice message={message} askAgent />
      <ErrorNotice message={message} askAgent={false} />
      <ErrorNotice message={message} noHandoffReason={reason} />
      {/* @ts-expect-error a stated no-hand-off reason cannot come with the hand-off */}
      <ErrorNotice message={message} noHandoffReason={reason} askAgent />
      {/* @ts-expect-error not even a hand-off that is switched off: one or the other */}
      <ErrorNotice message={message} noHandoffReason={reason} askAgent={false} />
      {/* @ts-expect-error a hand-off label means a hand-off */}
      <ErrorNotice message={message} noHandoffReason={reason} askAgentLabel={message} />
      {/* @ts-expect-error onHandoff means a hand-off */}
      <ErrorNotice message={message} noHandoffReason={reason} onHandoff={noop} />
    </>
  )
}
