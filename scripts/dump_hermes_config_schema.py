#!/usr/bin/env python3
"""Regenerate backend/drivers/hermes_config_schema.yaml from a local
hermes-agent checkout.

The snapshot captures exactly what `hermes config set` validates against:

- DEFAULT_CONFIG (hermes_cli/config_defaults.py), collapsed to a key tree —
  populated dicts recurse, everything else (scalars, lists, EMPTY dicts)
  becomes null. An empty dict in DEFAULT_CONFIG is a free-form section whose
  keys are user-chosen (model.aliases.<role>, terminal.docker_env.<VAR>),
  so null means "leaf or open mapping: anything below is accepted".
- _OPEN_SUBKEY_TOP_LEVEL_KEYS — top-level sections validated first-segment-
  only (platform configs, providers, mcp_servers, ...).
- _EXTRA_KNOWN_ROOT_KEYS — roots the runtime reads but DEFAULT_CONFIG omits.
- _PLATFORM_CONTAINER_KEYS — containers whose child is a user-chosen name.

Usage:

    python3 scripts/dump_hermes_config_schema.py [path/to/hermes-agent]

Defaults to ~/.hermes/hermes-agent. The hermes_cli package is imported from
that checkout (sys.path insert), so run this with a Python that can import
the checkout's hermes_cli package.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import date
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "backend" / "drivers" / "hermes_config_schema.yaml"

# Hand-maintained overlays for sections DEFAULT_CONFIG seeds as a scalar or
# empty value even though the runtime reads a FIXED set of subkeys under
# them — without this, the collapsed tree accepts any typo below (`model`
# is seeded as the scalar shorthand form, so DEFAULT_CONFIG alone would wave
# `model.primary` through). Values follow the same collapse convention:
# null = leaf or free-form mapping. Extend when a Hermes version adds a
# runtime-read subkey; this is the one place the snapshot isn't mechanical.
OVERLAYS = {
    # Verified against hermes-agent runtime readers (model_tools.py,
    # hermes_cli/config.py, route_identity.py, auth.py, model_switch.py):
    "model": {
        "default": None,
        "model": None,          # legacy alias for default
        "name": None,           # legacy alias for default
        "provider": None,
        "base_url": None,
        "api_base": None,       # alias, normalized to base_url at set time
        "api_mode": None,
        "aliases": None,        # free-form: role name -> provider/model route
        "context_length": None,
        "key_env": None,
        "api_key_env": None,
        "auth_mode": None,
        "entra": None,          # dict, azure/entra auth — left open
    },
}


def collapse(node):
    """DEFAULT_CONFIG -> key tree. Populated dicts recurse; every other
    value (scalar, list, empty dict) collapses to null = 'leaf or free-form
    mapping' — both accept anything below them at validation time."""
    if isinstance(node, dict) and node:
        return {key: collapse(value) for key, value in sorted(node.items())}
    return None


def main() -> None:
    checkout = Path(sys.argv[1] if len(sys.argv) > 1 else Path.home() / ".hermes" / "hermes-agent").resolve()
    if not (checkout / "hermes_cli" / "config.py").exists():
        raise SystemExit(f"no hermes_cli package under {checkout} — pass the hermes-agent checkout path")

    sys.path.insert(0, str(checkout))
    from hermes_cli.config import (  # noqa: E402
        _EXTRA_KNOWN_ROOT_KEYS,
        _OPEN_SUBKEY_TOP_LEVEL_KEYS,
        _PLATFORM_CONTAINER_KEYS,
    )
    from hermes_cli.config_defaults import DEFAULT_CONFIG  # noqa: E402

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(checkout), *args], capture_output=True, text=True, check=True
        ).stdout.strip()

    tree = collapse(DEFAULT_CONFIG)
    overlaid = []
    for section, overlay in OVERLAYS.items():
        if not tree.get(section):  # only overlays a degenerate (null) section
            tree[section] = overlay
            overlaid.append(section)

    body = {
        "source": {
            "checkout": "hermes-agent",
            "commit": git("rev-parse", "HEAD"),
            "describe": git("describe", "--tags", "--always"),
            "generated": date.today().isoformat(),
        },
        "overlaid_sections": overlaid,
        "open_roots": sorted(_OPEN_SUBKEY_TOP_LEVEL_KEYS),
        "extra_roots": sorted(_EXTRA_KNOWN_ROOT_KEYS),
        "platform_containers": sorted(_PLATFORM_CONTAINER_KEYS),
        "tree": tree,
    }

    header = """\
# Known Hermes config-key schema — what Beacon validates desired.config and
# template config paths against before diffing/pushing. REGENERATED, not
# hand-edited: scripts/dump_hermes_config_schema.py [hermes-agent checkout].
#
# Semantics (mirroring `hermes config set`'s own rules, one notch stricter —
# see backend/drivers/hermes.py validate_config_key):
#   - a path under `tree` must follow the populated-dict structure exactly;
#     an unknown subkey of a POPULATED section is a likely typo -> error
#   - null = leaf or free-form mapping: anything at/below it is accepted
#   - open_roots / extra_roots / platform_containers: accepted, no deep check
#   - unknown TOP-LEVEL keys are legal in Hermes (custom keys bridge to the
#     environment for skills) -> warning, not an error
#   - overlaid_sections were degenerate (scalar/empty) in DEFAULT_CONFIG but
#     have a fixed runtime-read key set; their tree comes from the OVERLAYS
#     table in the generator script, not from DEFAULT_CONFIG
"""
    OUT_PATH.write_text(header + yaml.safe_dump(body, sort_keys=False, width=120))
    print(f"wrote {OUT_PATH} ({len(body['tree'])} top-level sections, from {body['source']['describe']})")


if __name__ == "__main__":
    main()
