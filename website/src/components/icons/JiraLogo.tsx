import { BrandGlyph } from '../BrandIcon'
import jiraLogoUrl from './jira-logo.svg'

/** Official Jira mark, rendered as a theme-aware CSS mask via BrandGlyph. */
export default function JiraLogo({ size = 13, className = '' }: { size?: number; className?: string }) {
  return <BrandGlyph url={jiraLogoUrl} size={size} className={`inline-block ${className}`} testId="jira-provider-mark" />
}
