"""Run the documented Node.js v2 proxy verifier against the gateway's own signer.

The verifier is copied out of ``docs/app-kit/api-reference.md`` at test time, so
the snippet third-party backends copy is the code under test.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from kiro_crew.apps.proxy_auth import sign_proxy_request_v2

_DOC = Path(__file__).resolve().parent.parent / "docs" / "app-kit" / "api-reference.md"
_SECRET = "s3cret-app-key"
_NONCE = "0123456789abcdef" * 2
_OTHER_NONCE = "f" * 32
_NOW = 1_000_000
_TARGET = "/api/write?page=2&q=a%20b"
_BODY = '{"text":"hi"}'

_DRIVER = """
import { createProxyV2Verifier } from './verifier.mjs';
const cases = JSON.parse(process.argv[2]);
const results = [];
let verify = null;
for (const c of cases) {
  if (c.fresh || verify === null) verify = createProxyV2Verifier(c.secret, { maxEntries: c.maxEntries ?? 4096 });
  results.push(verify({ ...c.args, body: Buffer.from(c.args.body, 'utf8') }));
}
process.stdout.write(JSON.stringify(results));
"""


def _reference_verifier() -> str:
    blocks = re.findall(r"```js\n(.*?)```", _DOC.read_text(encoding="utf-8"), flags=re.S)
    matches = [block for block in blocks if "export function createProxyV2Verifier" in block]
    assert len(matches) == 1, "the api reference must carry exactly one Node v2 verifier"
    return matches[0]


def _sig(*, ts: int = _NOW, nonce: str = _NONCE, method: str = "POST", body: str = _BODY) -> str:
    return sign_proxy_request_v2(
        secret=_SECRET, ts=ts, nonce=nonce, method=method, target=_TARGET, body=body.encode()
    )


def _case(*, fresh: bool = False, max_entries: int = 4096, secret: str = _SECRET, **args):
    request = {
        "method": "POST",
        "target": _TARGET,
        "body": _BODY,
        "signature": _sig(),
        "nonce": _NONCE,
        "now": _NOW,
    }
    request.update(args)
    return {"fresh": fresh, "maxEntries": max_entries, "secret": secret, "args": request}


def test_the_reference_verifier_is_present_in_the_docs():
    source = _reference_verifier()
    assert "timingSafeEqual" in source
    assert "new Map()" in source


@pytest.mark.skipif(shutil.which("node") is None, reason="the reference verifier runs under node")
def test_the_documented_node_verifier_agrees_with_the_gateway(tmp_path: Path):
    ts, _, mac = _sig().partition(":")
    cases = [
        (_case(fresh=True), True),
        (_case(), False),  # identical replay
        (_case(signature=_sig(nonce=_OTHER_NONCE), nonce=_OTHER_NONCE), True),
        (_case(fresh=True, now=_NOW + 60), True),
        (_case(fresh=True, now=_NOW - 61), False),  # future timestamp
        (_case(fresh=True, now=_NOW + 61), False),  # stale timestamp
        (_case(fresh=True, method="PUT"), False),
        (_case(fresh=True, target="/api/write?page=3"), False),
        (_case(fresh=True, body='{"text":"bye"}'), False),
        (_case(fresh=True, nonce=_OTHER_NONCE), False),  # swapped nonce
        (_case(fresh=True, nonce=_NONCE.upper()), False),
        (_case(fresh=True, signature=f"{ts}:{mac[:-1]}\u00e9"), False),
        (_case(fresh=True, signature="no-colon"), False),
        (_case(fresh=True, signature="9" * 13 + ":" + mac), False),
        (_case(fresh=True, secret=""), False),
        (_case(fresh=True, secret="other-secret"), False),
        # A rejected request takes no entry, so the genuine one still passes after it.
        (_case(fresh=True, method="PUT"), False),
        (_case(), True),
        # A full cache refuses instead of evicting, then frees room once a window closes.
        (_case(fresh=True, max_entries=1), True),
        (_case(signature=_sig(nonce=_OTHER_NONCE), nonce=_OTHER_NONCE), False),
        (_case(), False),
        (
            _case(
                signature=_sig(ts=_NOW + 61, nonce=_OTHER_NONCE),
                nonce=_OTHER_NONCE,
                now=_NOW + 61,
            ),
            True,
        ),
    ]
    (tmp_path / "verifier.mjs").write_text(_reference_verifier(), encoding="utf-8")
    (tmp_path / "driver.mjs").write_text(_DRIVER, encoding="utf-8")
    done = subprocess.run(
        [shutil.which("node") or "node", "driver.mjs", json.dumps([c for c, _ in cases])],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == [expected for _, expected in cases]
