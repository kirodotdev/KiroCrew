/**
 * The text a send-to-session hand-off seeds the target composer with. Addressed
 * to the agent, so it names the slug (the handle `artifact_get` takes) rather
 * than relying on the title, which is neither unique nor stable. A `*.prompt.ts`
 * module, so it stays English by design: the agent reads it, and the user can
 * edit it before sending.
 */
export function artifactReferencePrompt(name: string, slug: string): string {
  return `Reference artifact "${name}" (slug \`${slug}\`; load it with artifact_get).`
}
