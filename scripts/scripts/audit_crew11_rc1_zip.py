#!/usr/bin/env python3
"""Read-only validation of an official KiroCrew 0.11.0-rc.1 *source ZIP*.

Extracts only explicitly required Python source files into a temporary directory,
never to the installed Crew tree. Does not run the uploaded code. The hashes are
pinned in the BYO 0.2.5 source-specific adapter. No runtime or Windows claims.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import sys
import tempfile
from pathlib import Path
from zipfile import ZipFile, BadZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from kiro_byo.integration_manager import _adapter_matches

ADAPTER = ROOT / 'integration/kiro_crew/v0.11-rc1'
PREFIX = 'src/kiro_crew/'


def _archive_hash(archive: Path) -> str:
    with archive.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()

def audit(archive: Path) -> dict:
    compat = json.loads((ADAPTER / 'compatibility.json').read_text(encoding='utf-8'))
    manifest = json.loads((ADAPTER / 'manifest.json').read_text(encoding='utf-8'))
    requested = set(str(n) for n in manifest['files'] if n not in compat['created_files'])
    for name in ('required_contract_symbols', 'required_contract_markers', 'required_contract_exports'):
        requested.update(compat.get(name, {}))
    with ZipFile(archive) as z, tempfile.TemporaryDirectory(prefix='kirocrew-rc1-check-') as temp:
        all_names = z.namelist()
        roots = {n[:-len(PREFIX+'agent_sdk/backends.py')] for n in all_names if n.endswith(PREFIX+'agent_sdk/backends.py')}
        if len(roots) != 1:
            raise ValueError('Expected exactly one Crew source root')
        root = roots.pop()
        crew = Path(temp) / 'kiro_crew'
        extracted = 0
        for rel in sorted(requested):
            if not rel.startswith(PREFIX) or '..' in Path(rel).parts:
                raise ValueError('Unsafe contract source path')
            member = root + rel
            try:
                info = z.getinfo(member)
            except KeyError:
                raise ValueError(f'Expected source is missing: {rel}') from None
            if info.is_dir() or info.file_size > 4_000_000 or extracted + info.file_size > 40_000_000:
                raise ValueError(f'Refusing oversized/non-file source: {rel}')
            data = z.read(member)
            extracted += len(data)
            target = crew / rel[len(PREFIX):]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        ok, issues, _, _ = _adapter_matches(ADAPTER, '0.11.0-rc.1', crew)
        return {
            'archive_name': archive.name,
            'archive_sha256': _archive_hash(archive),
            'target_version': '0.11.0-rc.1',
            'exact_source_contract_matches': ok,
            'checked_python_source_files': len(requested),
            'generated_seams': len(compat['generated_files']),
            'issues': issues,
            'windows_desktop_e2e_verified': False,
            'mate2_worker_kit_verified': not issues,
            'mate2_worker_kit_scope': 'source_contract_only' if not issues else 'unverified',
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    try:
        result = audit(args.archive)
    except (OSError, BadZipFile, ValueError, RuntimeError) as exc:
        print(f'ERROR: {exc}', file=sys.stderr)
        return 2
    out = json.dumps(result, ensure_ascii=False, indent=2)+'\n'
    print(out)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(out, encoding='utf-8')
    return 0 if result['exact_source_contract_matches'] else 2

if __name__ == '__main__':
    raise SystemExit(main())
