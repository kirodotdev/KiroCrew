import { BrandGlyph } from '../../components/BrandIcon'
import notionLogoUrl from './logos/notion.svg'
import githubLogoUrl from './logos/github.svg'
import linearLogoUrl from './logos/linear.svg'
import atlassianLogoUrl from './logos/atlassian.svg'
import stripeLogoUrl from './logos/stripe.svg'
import vercelLogoUrl from './logos/vercel.svg'
import gitlabLogoUrl from './logos/gitlab.svg'
import sentryLogoUrl from './logos/sentry.svg'
import supabaseLogoUrl from './logos/supabase.svg'
import airtableLogoUrl from './logos/airtable.svg'
import paypalLogoUrl from './logos/paypal.svg'
import asanaLogoUrl from './logos/asana.svg'
import figmaLogoUrl from './logos/figma.svg'
import canvaLogoUrl from './logos/canva.svg'
import dropboxLogoUrl from './logos/dropbox.svg'
import miroLogoUrl from './logos/miro.svg'
import webflowLogoUrl from './logos/webflow.svg'
import netlifyLogoUrl from './logos/netlify.svg'
import mixpanelLogoUrl from './logos/mixpanel.svg'
import cloudflareLogoUrl from './logos/cloudflare.svg'
import huggingfaceLogoUrl from './logos/huggingface.svg'
import zapierLogoUrl from './logos/zapier.svg'
import squareLogoUrl from './logos/square.svg'
import postmanLogoUrl from './logos/postman.svg'
import neonLogoUrl from './logos/neon.svg'
import prismaLogoUrl from './logos/prisma.svg'

/** Official provider brand marks for the Connections cards.
 *
 *  Art lives in its own `.svg` beside this component and is consumed through a
 *  plain URL import — no `<svg>` element or path data in this file (AUTOSDE
 *  `use-lucide-icons` blocks that unconditionally; lucide-react ships no brand
 *  marks, so these qualify under the rule's brand-mark exception).
 *
 *  Render follows the mark, per the exception's condition 2:
 *   - MONOCHROME marks (Notion, GitHub, Vercel, and the industry-baseline
 *     set) sit inline among lucide glyphs and are painted as a CSS mask
 *     over `currentColor` via the shared `BrandGlyph` helper, so they inherit
 *     the card's text colour and stay legible in every theme (their brand art
 *     is near-black, which would disappear on a dark card). The
 *     industry-baseline marks are the single-path 24×24 glyphs from the CC0
 *     simple-icons set, the same source as notion.svg. Amplitude ships no mark
 *     and takes the card's lettered tile: simple-icons does not carry it, and
 *     Amplitude's brand guidelines ask for a check-in before its logomark is
 *     used on a product, so the tile stands until that confirmation.
 *     The marks themselves remain each vendor's trademark and are used only to
 *     indicate compatibility.
 *   - FULL-COLOUR marks (Linear, Atlassian, Stripe) carry their identity in
 *     their own hues, so they render as a plain `<img>` and are never flattened
 *     to `currentColor`.
 */

/** Monochrome marks — CSS mask over `currentColor`. */
const MASKED: Record<string, string> = {
  notion: notionLogoUrl,
  github: githubLogoUrl,
  vercel: vercelLogoUrl,
  sentry: sentryLogoUrl,
  supabase: supabaseLogoUrl,
  airtable: airtableLogoUrl,
  paypal: paypalLogoUrl,
  asana: asanaLogoUrl,
  figma: figmaLogoUrl,
  canva: canvaLogoUrl,
  dropbox: dropboxLogoUrl,
  miro: miroLogoUrl,
  webflow: webflowLogoUrl,
  netlify: netlifyLogoUrl,
  mixpanel: mixpanelLogoUrl,
  cloudflare: cloudflareLogoUrl,
  huggingface: huggingfaceLogoUrl,
  zapier: zapierLogoUrl,
  square: squareLogoUrl,
  postman: postmanLogoUrl,
  neon: neonLogoUrl,
  prisma: prismaLogoUrl,
}

/** Full-colour marks — plain `<img>`, colours preserved. */
const COLOURED: Record<string, string> = {
  linear: linearLogoUrl,
  atlassian: atlassianLogoUrl,
  stripe: stripeLogoUrl,
  gitlab: gitlabLogoUrl,
}

/** The provider's brand mark, or null for a slug we ship no mark for (the card
 *  then falls back to its lettered tile). */
export default function ProviderLogo({ slug, size = 20 }: { slug: string; size?: number }) {
  const masked = MASKED[slug]
  if (masked) {
    return <BrandGlyph url={masked} size={size} testId={`provider-logo-${slug}`} />
  }
  const coloured = COLOURED[slug]
  if (coloured) {
    return (
      <img
        src={coloured}
        alt=""
        aria-hidden="true"
        width={size}
        height={size}
        data-testid={`provider-logo-${slug}`}
        className="inline-block shrink-0"
      />
    )
  }
  return null
}

/** Slugs this module can render — the card's lettered fallback covers the rest. */
export const PROVIDER_LOGO_SLUGS = [...Object.keys(MASKED), ...Object.keys(COLOURED)]
