"""T-241: docs hygiene — docstrings must match the phase-3 reality.

Audit F5/F6: cli_hp.py's contract docstrings still describe the phase-2
MVP boundary ("bridge → NotImplementedError", "hp get: bridge resolution
not supported by this node-cli version", "FROZEN CONTRACT (Phase 1)") —
the bridge rungs have been fully implemented since phase 3
(cmd_put :265ff with serve probe + stage_file + register_temp_route,
cmd_get :377ff with the proxy stream). Stale contract documentation on
the handler-facing primitives actively misleads script authors.

Audit F6: docstrings reference files that do not exist in this repo —
``NODE_CLI_SPEC.md §4`, ``docs/node/capabilities.md`` (handler_runner
module docstring) and the bare ``concepts/forward-routing.md`` slug in
the 'forward' capability refusal message (node_config). The docs are
served live by every relay at ``/relay/v2/docs/<slug>`` (llms.txt,
CONTRIBUTING) — docstrings link there now.

Plus the README forward-routing section the release series
2.3.29-2.3.31 promised publicly (T-Forward-Node, T-233, T-235) but never
documented.

These are CONTRACT LOCKS on documentation (not behavior RED); they fail
against the current text and guard the fixed state.
"""

from __future__ import annotations

import pytest

from nodes.common import handler_runner, node_config
from nodes.common.cli import cli_hp

DOCS_LIVE_PREFIX = "/relay/v2/docs"


def test_module_docstring_states_phase3_completion():
    doc = cli_hp.__doc__ or ""
    assert "fully implemented" in doc
    assert "NotImplementedError" not in doc
    assert "FROZEN CONTRACT (Phase 1, T-179)" not in doc


def test_cmd_put_docstring_describes_real_bridge():
    doc = cli_hp.cmd_put.__doc__ or ""
    assert "NotImplementedError" not in doc
    assert "bridge-put follows in Task 3" not in doc
    # The real mechanics — serve probe, staging, route registration.
    assert "stage_file" in doc
    assert "register_temp_route" in doc


def test_cmd_get_docstring_describes_real_bridge():
    doc = cli_hp.cmd_get.__doc__ or ""
    assert "bridge resolution not supported" not in doc
    assert "not implemented in phase 2" not in doc
    # The real mechanics — proxy pull via node_serve storage_ref.
    assert "proxy" in doc


def test_handler_runner_docstring_links_real_docs():
    doc = handler_runner.__doc__ or ""
    assert "NODE_CLI_SPEC.md" not in doc
    assert "docs/node/capabilities.md" not in doc
    assert f"{DOCS_LIVE_PREFIX}/node/handlers/contract" in doc
    assert f"{DOCS_LIVE_PREFIX}/node/capabilities" in doc


def test_forward_capability_refusal_links_live_docs():
    """Name a capability 'forward' → refusal cites the relay-served docs."""
    with pytest.raises(node_config.CapabilityValidationError) as excinfo:
        node_config.validate_profile(
            {"capabilities": [{"name": "forward", "handler": "/usr/bin/env"}]}
        )
    message = str(excinfo.value)
    assert f"{DOCS_LIVE_PREFIX}/concepts/forward-routing" in message
    # The bare file-slug pointer is replaced by the live docs path.
    assert "concepts/forward-routing.md" not in message


def test_readme_documents_forward_routing():
    from pathlib import Path

    readme_path = (
        Path(node_config.__file__).resolve().parent.parent.parent / "README.md"
    )
    text = readme_path.read_text(encoding="utf-8")
    assert "## Forward Routing" in text
    assert "forward_slots" in text
    assert "--forward-slot" in text
    assert f"{DOCS_LIVE_PREFIX}/concepts/forward-routing" in text
    # The GitHub-rendered page for the same content.
    assert "https://github.com/iowap-org/iowap-docs/blob/main/concepts/forward-routing.md" in text