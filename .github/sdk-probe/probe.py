"""Run the PR's sdk_extras installer on a shipped desktop backend interpreter.

Usage: <bundled python> probe.py <data home>
The bundle's own lark_oapi / Crypto / requests_toolbelt must already be removed,
so every import below can only come from the data-home install.
"""
import importlib.util
import os
import sys
from pathlib import Path

home = Path(sys.argv[1])
os.environ["KIROCREW_HOME"] = str(home)
here = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("sdk_extras", here / "sdk_extras.py")
sdk = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sdk)

from kiro_crew import platform_compat  # noqa: E402

print("interpreter:", sys.executable)
print("is_bundled_interpreter:", platform_compat.is_bundled_interpreter())
print("python_tag:", sdk.python_tag())
for name in ("lark_oapi", "Crypto", "requests_toolbelt"):
    assert importlib.util.find_spec(name) is None, f"{name} still importable from the bundle"
print("bundled SDK removed: ok")

target = sdk.install("feishu", home)
print("installed into:", target)

import lark_oapi  # noqa: E402
import lark_oapi.api.im.v1  # noqa: E402,F401
import lark_oapi.ws  # noqa: E402,F401
import requests_toolbelt  # noqa: E402
from Crypto.Cipher import AES  # noqa: E402

for mod in (lark_oapi, requests_toolbelt, sys.modules["Crypto"]):
    assert str(Path(mod.__file__).resolve()).startswith(str(target.resolve())), mod.__file__
# Exercises pycryptodome's native extension, the file library validation would refuse.
ct = AES.new(b"k" * 16, AES.MODE_ECB).encrypt(b"x" * 16)
native = [p for p in target.rglob("*") if p.suffix in (".so", ".pyd", ".dylib")]
print("native files loaded from data home:", len(native), "AES ok:", len(ct) == 16)
print("PROBE PASS")
