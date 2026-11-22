/**
 * Up to two upper-case letters naming an account: the first letters of the
 * first two words of the email's local part (`zezhen.xu@…` -> `ZX`), or its
 * first two letters when it is one word. `?` when there is nothing to read.
 */
export function emailInitials(email: string): string {
  const localPart = email.split('@', 1)[0] ?? ''
  const parts = localPart.split(/[^A-Za-z0-9]+/).filter(Boolean)
  if (parts.length >= 2) return `${parts[0][0]}${parts[1][0]}`.toUpperCase()
  return (parts[0] ?? '?').slice(0, 2).toUpperCase()
}
