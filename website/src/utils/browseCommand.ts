/**
 * Whether a shell command preview is the START of a browse.
 *
 * Matches the INVOCATION, not a mention: `playwright-cli` has to be the first
 * word of a command, so `grep playwright-cli .` and `echo playwright-cli` are
 * not browses while `cd /tmp && playwright-cli open …` is. Unit-tested rather
 * than asserted in a comment -- a regex that merely required leading WHITESPACE
 * matched every mention.
 *
 * Its own module rather than a member of `pages/ChatPage.tsx` so that test can
 * reach it without importing the page: ChatPage pulls ~700 eager modules
 * (framer-motion, react-markdown, katex, highlight.js, the store, the router)
 * into the importing fork, which a predicate over a string has no reason to pay
 * for.
 */
export function isBrowseCommand(preview: string | undefined | null): boolean {
  if (!preview) return false
  // A real shell preview is the tool INPUT, which is JSON:
  // `{"command":"playwright-cli open https://x"}`. Testing the raw string never
  // matched, because `playwright-cli` sits behind a quote rather than at a
  // command boundary -- so the panel never opened. Pull the command field out
  // first, mirroring the backend's own `_extract_bash_command`, and fall back to
  // the raw text for a preview that is already a bare command.
  let cmd = preview
  try {
    const parsed: unknown = JSON.parse(preview)
    if (parsed && typeof parsed === 'object' && typeof (parsed as { command?: unknown }).command === 'string') {
      cmd = (parsed as { command: string }).command
    }
  } catch {
    // Not JSON: use the preview verbatim.
  }
  return /(^|[;&|(]\s*)playwright-cli(\s|$)/.test(cmd)
}
