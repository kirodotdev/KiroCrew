import { BrandGlyph } from '../BrandIcon'
import azureDevopsLogoUrl from './azure-devops-logo.svg'

/** Official Azure DevOps mark, rendered through the shared `BrandGlyph`.
 *
 * `BrandGlyph` owns the theme-aware CSS mask (including the quoting that keeps a
 * Vite-inlined `data:` URI from breaking the `url(...)` token), so this component
 * does not restate that styling. It only wraps the glyph in a span carrying
 * `data-provider-mark`, which is how the provider badge identifies which mark
 * rendered and which `BrandGlyph` itself does not expose.
 */
export default function AzureDevopsLogo({ size = 13, className = '' }: { size?: number; className?: string }) {
  return (
    <span aria-hidden="true" className="inline-flex shrink-0" data-provider-mark="azure">
      <BrandGlyph url={azureDevopsLogoUrl} size={size} className={`inline-block ${className}`} />
    </span>
  )
}
