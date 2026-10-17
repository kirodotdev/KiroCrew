/**
 * The text a send-to-session hand-off seeds the target composer with. Addressed
 * to the agent, so it names the slug (the handle `artifact_get` takes) rather
 * than relying on the title, which is neither unique nor stable. A `*.prompt.ts`
 * module, so it stays English by design: the agent reads it, and the user can
 * edit it before sending.
 *
 * The name is agent-settable, so it is reduced to its first line with control
 * characters and backticks removed, then quoted as a JSON string: a name cannot
 * add lines or break out of its quotes in a draft the user may send unread.
 */
export function artifactReferencePrompt(name: string, slug: string): string {
  const firstLine = name.split(/\r\n|[\n\r\u2028\u2029]/, 1)[0].replace(/[\u0000-\u001f\u007f-\u009f`]/g, '').trim()
  return `Reference artifact ${JSON.stringify(firstLine)} (slug \`${slug}\`; load it with artifact_get).`
}
