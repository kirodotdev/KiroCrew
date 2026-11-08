# kc_native

A PyO3 module with one function, `might_contain_credential`: the native version of the pre-filter in `kiro_crew.security.redaction_patterns`. When the module is importable, redaction uses it by default. When it is missing, out of date (its `GATE_FINGERPRINT` does not match the Python pre-filter), or turned off with `KIROCREW_NATIVE_REDACTION=0`, the pure-Python pre-filter runs. Both give the same answer for every string.

It reads each string from the str object's own storage and never asks CPython for UTF-8, because CPython keeps that UTF-8 copy inside the string until the string dies. That is also why the module is built for one CPython version (no abi3): the limited API cannot reach a string's storage.

## Build

Into the gateway's own venv:

```bash
cd native/kc_native
VIRTUAL_ENV=/path/to/venv PATH=/path/to/venv/bin:$PATH \
  pip install maturin==1.15.0 && maturin develop --release --locked
```

For a wheel: `maturin build --release --locked`, then `pip install target/wheels/kc_native-*.whl`.

## Tests

```bash
.venv/bin/python -m pytest test/test_native_redaction_gate.py      # needs the module built
.venv/bin/python -m pytest test/test_native_redaction_fallback.py  # runs without it
```
