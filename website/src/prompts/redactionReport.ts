import { FEATURE_REQUEST_URL } from './featureRequest'

/**
 * A new issue on the project tracker, prefilled with the rule that fired and
 * nothing else: no removed value, no URL, no message text. The reader sees the
 * whole issue before sending it, and it goes nowhere until they do. The text is
 * addressed to the maintainers, so it stays in the tracker's language.
 */
export function falsePositiveIssueUrl(rule: string, kind: 'credential' | 'link'): string {
  const what = kind === 'credential' ? 'credential redaction' : 'blocked link'
  const title = `Redaction false positive: ${rule}`
  const body = [
    `**Rule:** \`${rule}\``,
    `**Card:** ${what}`,
    '',
    '**What was removed, in your own words** (do not paste the value or the URL):',
    '',
    '',
    '**Why it is not a secret / not suspicious:**',
    '',
  ].join('\n')
  const q = new URLSearchParams({ title, body })
  return `${FEATURE_REQUEST_URL}?${q.toString()}`
}
