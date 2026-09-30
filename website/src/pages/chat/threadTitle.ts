/**
 * The stored thread title as it should read BESIDE a label that already says
 * "Thread".
 *
 * `_default_title` stores `"Thread: <first words of the anchor>"`, and both the
 * footer chip and the drawer header draw the word "Thread" themselves — so the
 * stored prefix renders a second time and the reader gets "Thread  Thread:
 * Amazon" for one thing.
 *
 * Stripped at the READER rather than at the writer on purpose. Every thread
 * already opened carries the prefix in its stored title, and a change to
 * `_default_title` alone would leave all of them reading double; there is no
 * migration that could reach them, because a title is also a field a person may
 * have edited. The sidebar keeps the stored prefix, which is currently the only
 * thing distinguishing a thread's card from a session's.
 *
 * Only the exact default shape is stripped — `Thread:` with its colon, or the
 * bare word alone. A person's own title of "Thread safety in the sandbox" has no
 * colon and survives intact, which a looser prefix match would have eaten.
 */
export function threadTitleBeside(title: string | null | undefined): string {
  const text = (title ?? '').trim()
  if (/^thread$/i.test(text)) return ''
  return text.replace(/^thread:\s*/i, '').trim()
}
