import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { render, screen } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import { HowItWorks } from './SectionsBottom';

// The landing page quotes the install floors by hand. The real floors live in
// the repo root: pyproject.toml decides which Python can install the package,
// and ensure-node.sh carries the Node floor the installers and `kirocrew
// doctor` enforce. Read both so a floor bump that forgets this page fails here.
// Resolved through node:path, not `new URL(..., import.meta.url)`: Vite reads
// that form as an asset glob and would try to serve the whole repo root.
const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');

function repoFile(relative: string): string {
  return readFileSync(resolve(REPO_ROOT, relative), 'utf8');
}

function declaredPythonFloor(): string {
  const match = /^requires-python\s*=\s*"\s*>=\s*([\d.]+)"/m.exec(repoFile('pyproject.toml'));
  if (!match) throw new Error('pyproject.toml declares no requires-python floor');
  return match[1];
}

function declaredNodeFloor(): string {
  const match = /^MIN_VERSION="([\d.]+)"/m.exec(repoFile('ensure-node.sh'));
  if (!match) throw new Error('ensure-node.sh declares no MIN_VERSION floor');
  return match[1];
}

describe('HowItWorks prerequisites', () => {
  it('names the Python and Node floors the installers enforce', () => {
    render(<HowItWorks />);
    const line = screen.getByText(/^Prerequisites:/).textContent ?? '';
    expect.soft(line).toContain(`Python ${declaredPythonFloor()}+`);
    expect.soft(line).toContain(`Node.js ${declaredNodeFloor()}+`);
  });
});
