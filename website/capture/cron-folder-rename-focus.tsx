/**
 * Isolated capture entry for the Schedule page's cron folder header rename.
 *
 * Mounts the REAL CronFolderHeader in a table, against the real stylesheet,
 * theme tokens and i18n catalog. The harness opens the rename through the real
 * folder-actions menu and closes it with real key presses, so each frame shows
 * where focus lands after the editor closes.
 *   &theme=dark|light
 */
import { useState } from 'react'
import { createRoot } from 'react-dom/client'

import CronFolderHeader from '../src/components/CronFolderHeader'
import { Table, TableBody } from '../src/components/ui/table'
import { initI18n } from '../src/i18n/all'
import '../src/index.css'

const theme = new URLSearchParams(location.search).get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

function Scene() {
  const [name, setName] = useState('Nightly jobs')
  const [collapsed, setCollapsed] = useState(false)
  return (
    <div data-testid="scene" className="bg-bg p-6" style={{ width: 640 }}>
      <Table>
        <TableBody>
          <CronFolderHeader
            folder={{ id: 'nightly', name, order: 0 }}
            jobCount={3}
            collapsed={collapsed}
            onToggleCollapse={() => setCollapsed(c => !c)}
            onRename={setName}
            onDelete={() => {}}
            colSpan={4}
          />
        </TableBody>
      </Table>
    </div>
  )
}

initI18n('en')
createRoot(document.getElementById('root')!).render(<Scene />)
