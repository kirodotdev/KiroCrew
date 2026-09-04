#!/usr/bin/env bash
# Seam guard: every crew-template parameter without a Default must be passed by
# the driver, and every parameter the driver passes to EITHER stack must be
# declared by that stack's template.
#
# This check exists because its absence cost a deploy. The integration check that
# was run only went one way -- it proved the driver passes nothing the template
# fails to declare -- and the reverse case is the one that actually breaks:
# a parameter the template REQUIRES and the driver never passes. CloudFormation
# reports it as "Parameters: [X] must have values" at CreateChangeSet, five steps
# and one image push into a real deploy.
#
# It also only ever read crew.yaml. base.yaml was invisible in both directions,
# which is how an undeclared BASE parameter (an override for a parameter base.yaml
# does not declare) survived and aborted a real base deploy at CreateChangeSet
# while this guard printed green. Both templates are now covered.
#
# A one-directional agreement check reads as a passing seam check and is not one.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# The crew root: templates/ beside scripts/, this guard two levels down. The
# driver names the same directory CREW_ROOT, and the regex below reads that
# spelling out of the driver, so the two must agree.
ROOT="$(cd "$HERE/../.." && pwd)"

python3 - "$ROOT" <<'PY'
import re, sys, pathlib
root = pathlib.Path(sys.argv[1])
tpl = (root / "templates" / "crew.yaml").read_text()
drv = (root / "scripts" / "smc-deploy.sh").read_text()

blk = re.search(r'(?ms)^Parameters:\n(.*?)(?=\n[A-Z][A-Za-z]*:\n)', tpl).group(1)
params, cur = {}, None
for line in blk.splitlines():
    m = re.match(r'^  ([A-Za-z0-9]+):\s*$', line)
    if m:
        cur = m.group(1); params[cur] = []
    elif cur:
        params[cur].append(line)

required = [p for p, body in params.items()
            if not any(re.match(r'^\s+Default:', l) for l in body)]
passed = set(re.findall(r'"([A-Za-z0-9]+)=', drv))
declared = set(params)

missing = [p for p in required if p not in passed]

# The crew stack's OWN --parameter-overrides block, scoped by its --template-file.
# This used to scan for `ParameterKey=`, the long-form CloudFormation syntax, and
# the driver uses the short `Name=Value` form exclusively: the pattern matched zero
# times, so "driver passes nothing the template fails to declare" was printed
# without ever having been tested. Scoping matters as well as the syntax, because
# the base stack's overrides live in the same file and are not crew parameters.
crew_call = re.search(
    r'(?ms)--template-file\s+"\$CREW_ROOT/templates/crew\.yaml".*?--parameter-overrides(.*?)(?=\n\s*(?:then|fi|;;|\}|if )\b)',
    drv,
)
if crew_call is None:
    print("FAIL  cannot find the crew stack's --parameter-overrides block; this guard "
          "cannot check a call it cannot locate, and a silent pass would be worse")
    sys.exit(1)
crew_passed = re.findall(r'"([A-Za-z0-9]+)=', crew_call.group(1))
if not crew_passed:
    print("FAIL  the crew --parameter-overrides block parsed to zero parameters, so "
          "this guard would pass vacuously")
    sys.exit(1)
undeclared = [p for p in crew_passed if p not in declared]

# A DEFAULTED parameter can still be one the driver must supply per deployment, and
# those are invisible to the check above -- "all required parameters are passed" was
# true while RulePriority defaulted to 100 for every crew, so the second crew on the
# shared listener failed with "Priority '100' is currently in use". The template said
# so in its own Description ("the driver must supply a unique value per crew") and
# nothing read it.
#
# So: a parameter whose description says the driver must supply it counts as required,
# whatever its Default. The default then serves as documentation of the shape, not as
# a value anyone relies on.
def says_driver_must_supply(body):
    text = " ".join(body).lower()
    return "driver must supply" in text or "unique value per crew" in text

per_deploy = [p for p, body in params.items()
              if p not in required and says_driver_must_supply(body)]
per_deploy_missing = [p for p in per_deploy if p not in passed]

fail = False
if missing:
    print(f"FAIL  template requires but driver never passes: {missing}")
    fail = True
else:
    print(f"ok    all {len(required)} required crew parameters are passed")

if per_deploy_missing:
    print("FAIL  defaulted but must be supplied per deployment, and the driver does "
          f"not pass it: {per_deploy_missing}")
    fail = True
elif per_deploy:
    print(f"ok    {len(per_deploy)} defaulted parameter(s) that must still be supplied "
          f"per deployment are passed: {sorted(per_deploy)}")

if undeclared:
    print(f"FAIL  driver passes parameters the template does not declare: {undeclared}")
    fail = True
else:
    print("ok    driver passes nothing the template fails to declare")

# --- base stack -------------------------------------------------------------
# The gate above only ever read crew.yaml, which is how an undeclared BASE
# parameter (TranscriptRetentionDays, passed to a base.yaml that declares only
# LogRetentionDays) survived: it aborted a real base deploy at CreateChangeSet
# while this seam check reported a clean pass. Cover base.yaml with the same two
# directions, and with the same Name=Value short-form parsing -- the driver uses
# short form for the base call too, so a ParameterKey= scan would match zero
# times here as well and pass vacuously.
base_tpl = (root / "templates" / "base.yaml").read_text()

base_blk = re.search(r'(?ms)^Parameters:\n(.*?)(?=\n[A-Z][A-Za-z]*:\n)', base_tpl).group(1)
base_params, cur = {}, None
for line in base_blk.splitlines():
    m = re.match(r'^  ([A-Za-z0-9]+):\s*$', line)
    if m:
        cur = m.group(1); base_params[cur] = []
    elif cur:
        base_params[cur].append(line)
base_declared = set(base_params)

# The BASE stack's OWN --parameter-overrides block, scoped by its --template-file,
# so the crew stack's overrides in the same file are not mistaken for base ones.
base_call = re.search(
    r'(?ms)--template-file\s+"\$CREW_ROOT/templates/base\.yaml".*?--parameter-overrides(.*?)(?=\n\s*(?:then|fi|;;|\}|if )\b)',
    drv,
)
if base_call is None:
    print("FAIL  cannot find the base stack's --parameter-overrides block; this guard "
          "cannot check a call it cannot locate, and a silent pass would be worse")
    sys.exit(1)
base_passed = re.findall(r'"([A-Za-z0-9]+)=', base_call.group(1))
if not base_passed:
    print("FAIL  the base --parameter-overrides block parsed to zero parameters, so "
          "this guard would pass vacuously")
    sys.exit(1)

# Forward: every parameter the driver passes to the base stack must be declared.
base_undeclared = [p for p in base_passed if p not in base_declared]
if base_undeclared:
    print(f"FAIL  driver passes base parameters base.yaml does not declare: {base_undeclared}")
    fail = True
else:
    print("ok    driver passes nothing base.yaml fails to declare")

# Reverse: a base parameter the driver never passes is reported unless it is
# deliberately left at its default. A defaulted base parameter (VpcCidr,
# StageName) IS the deliberate case -- CloudFormation supplies the default, so the
# driver need not. A base parameter with NO Default that the driver never passes
# would fail a real base deploy ("Parameters: [X] must have values"), the same
# way the crew required-direction catches it.
base_required = [p for p, body in base_params.items()
                 if not any(re.match(r'^\s+Default:', l) for l in body)]
base_required_missing = [p for p in base_required if p not in base_passed]
if base_required_missing:
    print("FAIL  base.yaml requires (no Default) but driver never passes: "
          f"{base_required_missing}")
    fail = True
else:
    print(f"ok    all {len(base_required)} required base parameters are passed")

base_defaulted_unpassed = sorted(p for p in base_declared
                                 if p not in base_passed and p not in base_required_missing)
print(f"ok    {len(base_defaulted_unpassed)} base parameter(s) left at their declared "
      f"default (deliberate, not passed by the driver): {base_defaulted_unpassed}")

sys.exit(1 if fail else 0)
PY
