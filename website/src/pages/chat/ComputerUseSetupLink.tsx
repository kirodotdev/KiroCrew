import { useNavigate } from 'react-router-dom'
import { SquareMousePointer } from 'lucide-react'
import { i18nT } from '../../i18n/t'

/** The MCP server the `computer_*` tools are served from. A wire value. */
const COMPUTER_SERVER = /^kirocrew-computer$/

/** A `computer_*` refusal that only setup can clear names the Settings page it
 *  needs, the way the gateway's own refusal text does ("… under Settings ->
 *  Computer Use …"). A regex, not a string constant, because it matches wire
 *  text byte for byte and must never be translated. */
const SETUP_NEEDED_MARKER = /Settings -> Computer Use/

/** True when a finished computer-use tool row is a refusal the setup dialog can
 *  fix (the feature is off, a permission is missing, the driver is not set up).
 *
 *  Identity comes from the row's trusted transport fields (`mcpServer`,
 *  `trustedToolName`), never from the display title, which the model can write.
 *  The output must also carry the gateway's `Error:` prefix: a successful call
 *  that merely mentions the page does not get a button. */
export function computerUseSetupNeeded(row: { mcpServer: string; trustedToolName: string; output: string }): boolean {
  const fromComputer = COMPUTER_SERVER.test(row.mcpServer) || /^computer_[a-z_]+$/.test(row.trustedToolName)
  if (!fromComputer) return false
  const out = row.output.trimStart()
  return out.startsWith('Error:') && SETUP_NEEDED_MARKER.test(out)
}

/** "Set up Computer Use" under a refused computer-use tool row: opens
 *  Settings -> Computer Use with the setup dialog already open. */
export function ComputerUseSetupLink() {
  const navigate = useNavigate()
  return (
    <button
      type="button"
      onClick={() => navigate('/settings/computer-use?setup=1')}
      data-testid="computer-use-setup-link"
      className="mt-1.5 flex items-center gap-2 px-1.5 py-0.5 -ml-1.5 rounded text-[12px] leading-5 text-muted hover:text-text hover:bg-bg-hover bg-transparent border-none cursor-pointer transition-colors focus-ring-accent"
    >
      <SquareMousePointer size={13} aria-hidden />
      <span>{i18nT('pages.settings.computerUseSetup.set_up_action')}</span>
    </button>
  )
}
