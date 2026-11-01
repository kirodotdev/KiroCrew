"""Client UI latency remains independent of Provider and server Turn stats."""
from __future__ import annotations

import subprocess
from pathlib import Path

from kiro_byo import crew11_model_labels, crew11_ui_timing


def test_script_is_source_integrated_and_does_not_intercept_network_or_persist():
    script = crew11_ui_timing.script()
    html = crew11_model_labels.block()
    assert 'Kiro BYO client latency probe v1' in html
    assert 'type="submit"' not in script  # no synthetic submissions
    assert 'window.fetch =' not in script
    assert 'localStorage' not in script and 'sessionStorage' not in script
    assert 'window.location.reload' not in script
    assert 'performance.now()' in script
    assert 'requestAnimationFrame' in script
    assert "'/api/chat/slots'" in script
    assert 'pending.everBusy' in script
    assert 'new Set(messageBubbles' in script
    assert 'zh-TW' in script and 'ja' in script and 'ko' in script


def test_client_latency_browser_contract_fixture(tmp_path: Path):
    js = tmp_path / 'ui-latency.js'
    js.write_text(crew11_ui_timing.script(), encoding='utf-8')
    syntax = subprocess.run(['node','--check',str(js)],capture_output=True,text=True)
    assert syntax.returncode == 0, syntax.stderr
    fixture = Path(__file__).with_name('fixtures')/'crew11_client_latency_fixture.cjs'
    ran = subprocess.run(['node',str(fixture),str(js)],capture_output=True,text=True,timeout=10)
    assert ran.returncode == 0, ran.stderr
    assert 'ambiguous' in ran.stdout


def test_explicit_app_bundle_asset_is_authoritative_even_when_runtime_reader_is_stale(tmp_path, monkeypatch):
    app_asset = Path(__file__).resolve().parents[1] / 'src/kiro_byo/assets/crew11_ui_timing.js'
    expected = app_asset.read_text(encoding='utf-8')
    # A stale or missing site-packages resource cannot break App-source preflight.
    def stale_reader():
        raise FileNotFoundError('old runtime missing asset')
    monkeypatch.setattr(crew11_ui_timing, 'files', lambda _: stale_reader())
    assert crew11_ui_timing.script(app_asset=app_asset) == expected
    assert 'Kiro BYO client latency probe v1' in crew11_model_labels.block(timing_asset=app_asset)
    try:
        crew11_ui_timing.script()
    except FileNotFoundError:
        pass
    else:
        raise AssertionError('the test must exercise the stale-runtime failure')
