/**
 * A message from a caught value.
 *
 * `catch` binds `unknown`, because anything can be thrown. Narrowing here keeps the
 * call sites free of casts and guarantees the UI shows a string rather than
 * "[object Object]".
 */
export function errorText(err: unknown): string {
  if (err instanceof Error) return err.message
  if (typeof err === 'string') return err
  return ''
}
