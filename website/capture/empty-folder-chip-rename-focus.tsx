/**
 * Isolated capture entry for the Schedule page's empty-folder chip rename.
 *
 * Mounts the REAL EmptyFolderChip (the chip the Schedule page shows for a
 * folder with no jobs) against the real stylesheet, theme tokens and i18n
 * catalog. The harness opens the rename through the real folder-actions menu
 * and closes it with real key presses, so each frame shows where focus lands
 * after the editor closes.
 *   &theme=dark|light
 */
import { useState } from 'react'
import { createRoot } from 'react-dom/client'

import { EmptyFolderChip } from '../src/pages/SchedulePage'
import { initI18n } from '../src/i18n/all'
import '../src/index.css'

const theme = new URLSearchParams(location.search).get('theme') || 'dark'
document.documentElement.setAttribute('data-theme', theme === 'light' ? 'kiro-light' : 'kiro-dark')

function Scene() {
  const [name, setName] = useState('Nightly jobs')
  return (
    <div data-testid="scene" className="bg-bg p-6" style={{ width: 480 }}>
      <EmptyFolderChip folder={{ id: 'nightly', name, order: 0 }} onRename={setName} onDelete={() => {}} error={null} />
    </div>
  )
}

initI18n('en')
createRoot(document.getElementById('root')!).render(<Scene />)
