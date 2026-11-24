"""T-030: node dashboard — capability capacity fields + script upload +
Forward-Slots section.

Contract:

* Capability form (``<workspace>/bridge/providers.yaml``): optional
  ``max_parallel`` (int ≥ 1, default 1) and ``cost`` (int ≥ 0, default 1)
  land in the providers entry — empty fields mean the defaults, garbage
  is a validation error that writes NOTHING. ``artifacts_glob`` stays
  optional: present in the entry when submitted, absent when not.
* ``bridge.build_bridge_cap`` forwards ``max_parallel`` / ``cost`` from
  the manifest entry (default 1 each) instead of hardcoding — the values
  flow into the heartbeat → relay scheduler. Bools/garbage fall back.
* Script upload (POST /caps/script-upload, multipart): extension
  allowlist (.bat .cmd .ps1 .py .sh .exe), 2 MB cap, numeric collision
  suffix, atomic write into ``<workspace>/bridge/scripts/``; the JSON
  response carries the slugified capability name and the default argv
  per extension (workspace-relative — the Go daemon execs with
  ``cmd.Dir = workspaceDir``, process.go).
* Forward-Slots section: GET ``/relay/v2/forward-mappings[/slots]``
  filtered by the own node id (via RelayClient); 403 → quiet note
  ("nur für Admin-Nodes konfigurierbar"); success renders slot rows with
  editable ``target_capability`` (PUT upsert) and a delete button.
  Slots are NEVER created here — they come from node.yaml
  (``forward_slots``, T-053).
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nodes.common import bridge, dashboard, node_config, node_utils, onboarding


@pytest.fixture(autouse=True)
def _fresh_forward_latch(monkeypatch):
    """T-032: the 403 latch is module state — reset per test so a 403 case
    never silences the error cases that follow it."""
    monkeypatch.setattr(dashboard, "_FORBIDDEN_LATCH", {})


@pytest.fixture()
def dash_env(tmp_path, monkeypatch):
    """Isolate every state path the dashboard touches (see test_t021)."""
    for name in (
        "META_PATH", "TOKEN_PATH", "CONFIG_PATH",
        "LEGACY_META_PATH", "LEGACY_TOKEN_PATH", "STATUS_PATH",
    ):
        monkeypatch.setattr(node_utils, name, tmp_path / f"{name.lower()}.json")
    monkeypatch.setattr(node_config, "ACTIVE_PATH", tmp_path / "node.yaml")
    monkeypatch.setattr(node_config, "BASE_DIR", tmp_path / "relay")
    monkeypatch.setattr(dashboard, "mcp_bearer_path", lambda: tmp_path / "mcp_bearer")
    monkeypatch.setattr(dashboard, "mcp_pid_path", lambda: tmp_path / "mcp.pid")
    monkeypatch.setattr(dashboard, "mcp_log_path", lambda: tmp_path / "mcp.log")
    monkeypatch.setenv("IOWAP_WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.delenv("RELAY_BASE_URL", raising=False)
    monkeypatch.delenv("IOWAP_MCP_BEARER", raising=False)
    monkeypatch.setattr(onboarding, "_discover_relay_mdns", lambda timeout=2.0: None)
    return tmp_path


def _configure(dash_env):
    """Make the node look configured (meta + token + relay config)."""
    node_utils.META_PATH.write_text(json.dumps(
        {"node_id": "N1", "node_name": "win-node", "base_url": "http://r:8788"}))
    node_utils.TOKEN_PATH.write_text(json.dumps({"token": "tok-1"}))
    node_utils.CONFIG_PATH.write_text(json.dumps({"base_url": "http://r:8788"}))


class _Resp:
    def __init__(self, status_code: int, payload=None):
        self.status_code = status_code
        self._payload = payload
        self.text = "" if payload is None else json.dumps(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("no body")
        return self._payload


class FakeForwardClient:
    """Records _get/_put/_delete calls; answers from a status map."""

    def __init__(self, responses: dict[str, _Resp]):
        self.responses = responses
        self.calls: list[tuple[str, str, object]] = []

    def _get_with_retry(self, path, params=None, timeout=None):
        self.calls.append(("GET", path, params))
        return self.responses.get(path, _Resp(200, {}))

    def _put(self, path, body=None, timeout=None):
        self.calls.append(("PUT", path, body))
        return self.responses.get(("PUT", path), _Resp(200, {}))

    def _delete(self, path, timeout=None):
        self.calls.append(("DELETE", path, None))
        return self.responses.get(("DELETE", path), _Resp(200, {}))


# ---------------------------------------------------------------------------
# Capability form: max_parallel / cost (T-030)
# ---------------------------------------------------------------------------


def test_create_provider_writes_max_parallel_and_cost(dash_env):
    ok, msg = dashboard.create_provider({
        "name": "a.cap", "type": "process", "command": "run",
        "max_parallel": "3", "cost": "5",
    })
    assert ok, msg
    entry = yaml.safe_load(dashboard.providers_path().read_text())["providers"][0]
    assert entry["max_parallel"] == 3
    assert entry["cost"] == 5


def test_create_provider_capacity_defaults(dash_env):
    ok, msg = dashboard.create_provider({
        "name": "a.cap", "type": "http", "url_template": "u",
        "max_parallel": "", "cost": "",
    })
    assert ok, msg
    entry = yaml.safe_load(dashboard.providers_path().read_text())["providers"][0]
    assert entry["max_parallel"] == 1
    assert entry["cost"] == 1


def test_create_provider_rejects_invalid_capacity(dash_env):
    for fields in (
        {"max_parallel": "abc"}, {"max_parallel": "2.5"},
        {"max_parallel": "0"}, {"max_parallel": "-1"},
        {"cost": "x"}, {"cost": "-1"},
    ):
        payload = {"name": "a.cap", "type": "process", "command": "run"}
        payload.update(fields)
        ok, _ = dashboard.create_provider(payload)
        assert not ok, f"expected rejection for {fields}"
    assert not dashboard.providers_path().exists(), "failed validation must write nothing"


def test_artifacts_glob_optional_in_entry(dash_env):
    # without the field → no key
    ok, msg = dashboard.create_provider({"name": "a.cap", "type": "process", "command": "run"})
    assert ok, msg
    entry = yaml.safe_load(dashboard.providers_path().read_text())["providers"][0]
    assert "artifacts_glob" not in entry
    # with the field → present
    ok, msg = dashboard.create_provider({
        "name": "b.cap", "type": "process", "command": "run",
        "artifacts_glob": "artifacts/host.sysinfo/*",
    })
    assert ok, msg
    entry = yaml.safe_load(dashboard.providers_path().read_text())["providers"][1]
    assert entry["artifacts_glob"] == "artifacts/host.sysinfo/*"


# ---------------------------------------------------------------------------
# bridge.build_bridge_cap: capacity pass-through (T-030)
# ---------------------------------------------------------------------------


def test_build_bridge_cap_defaults_capacity():
    cap = bridge.build_bridge_cap("sysinfo", {"summary": "s"})
    assert cap["max_parallel"] == 1
    assert cap["cost"] == 1
    assert cap["name"] == "host.sysinfo"


def test_build_bridge_cap_forwards_capacity():
    cap = bridge.build_bridge_cap("sysinfo", {"max_parallel": 4, "cost": 7})
    assert cap["max_parallel"] == 4
    assert cap["cost"] == 7


def test_build_bridge_cap_coerces_garbage_capacity():
    # bools, strings and below-minimum values fall back to the defaults
    cap = bridge.build_bridge_cap(
        "sysinfo", {"max_parallel": True, "cost": "3", "x": 1})
    assert cap["max_parallel"] == 1  # bool → default
    assert cap["cost"] == 1          # string → default
    cap = bridge.build_bridge_cap("sysinfo", {"max_parallel": 0, "cost": -2})
    assert cap["max_parallel"] == 1
    assert cap["cost"] == 1


# ---------------------------------------------------------------------------
# Forward panel fetch (T-030)
# ---------------------------------------------------------------------------


SLOTS_BODY = {"slots": [
    {"node_id": "N1", "slot": "1", "label": "Wartungsslot",
     "description": "", "slot_type": "forward", "value": None,
     "ref_slot": None, "ref_field": None, "input_schema": None,
     "updated_at": "2026-11-24T00:00:00Z"},
    {"node_id": "N1", "slot": "plan", "label": "Plan",
     "description": "model slot", "slot_type": "forward", "value": None,
     "ref_slot": None, "ref_field": None, "input_schema": None,
     "updated_at": "2026-11-24T00:00:00Z"},
]}
MAPPINGS_BODY = {"mappings": [
    {"node_id": "N1", "slot": "plan", "target_capability": "model.specialist",
     "target_node_id": None, "binding": None,
     "created_at": "2026-11-24T00:00:00Z", "updated_at": "2026-11-24T00:00:00Z"},
]}


def _fake_client(status_slots=200, status_maps=200):
    return FakeForwardClient({
        "/relay/v2/forward-mappings/slots": _Resp(status_slots, SLOTS_BODY),
        "/relay/v2/forward-mappings": _Resp(status_maps, MAPPINGS_BODY),
    })


def test_fetch_forward_panel_success(dash_env):
    _configure(dash_env)
    client = _fake_client()
    panel = dashboard.fetch_forward_panel(client)
    assert panel["ok"] is True
    assert panel["forbidden"] is False
    assert panel["node_id"] == "N1"
    assert [s["slot"] for s in panel["slots"]] == ["1", "plan"]
    assert panel["mappings"]["plan"]["target_capability"] == "model.specialist"
    # both requests filtered by the own node id
    assert any("slots" in p for m, p, _ in client.calls if m == "GET")
    for method, path, params in client.calls:
        if method == "GET":
            assert params == {"node": "N1"}


def test_fetch_forward_panel_403_is_forbidden(dash_env):
    _configure(dash_env)
    client = FakeForwardClient({
        "/relay/v2/forward-mappings/slots": _Resp(403, {"detail": "Admin role required"}),
    })
    panel = dashboard.fetch_forward_panel(client)
    assert panel["ok"] is False
    assert panel["forbidden"] is True


def test_fetch_forward_panel_http_error(dash_env):
    _configure(dash_env)
    client = _fake_client(status_slots=500)
    panel = dashboard.fetch_forward_panel(client)
    assert panel["ok"] is False
    assert "500" in (panel["error"] or "")
    # 404 unknown node → error, not forbidden
    client = _fake_client(status_slots=404)
    panel = dashboard.fetch_forward_panel(client)
    assert panel["forbidden"] is False
    assert "404" in (panel["error"] or "")


def test_fetch_forward_panel_never_raises(dash_env):
    class ExplodingClient:
        def _get_with_retry(self, *a, **k):
            raise ConnectionError("relay down")

    panel = dashboard.fetch_forward_panel(ExplodingClient(), node_id="N1")
    assert panel["ok"] is False
    assert "relay down" in (panel["error"] or "")


# ---------------------------------------------------------------------------
# Forward mapping save/delete (PUT / DELETE contract)
# ---------------------------------------------------------------------------


def test_save_forward_mapping_put_contract():
    client = FakeForwardClient({})
    ok, msg = dashboard.save_forward_mapping(client, "N1", "plan", "model.specialist")
    assert ok, msg
    method, path, body = client.calls[-1]
    assert method == "PUT"
    assert path == "/relay/v2/forward-mappings/N1/plan"
    assert body == {"target_capability": "model.specialist"}


def test_save_forward_mapping_optional_pin():
    client = FakeForwardClient({})
    ok, _ = dashboard.save_forward_mapping(client, "N1", "1", "a.cap", "N2")
    assert ok
    assert client.calls[-1][2] == {"target_capability": "a.cap", "target_node_id": "N2"}
    # empty pin is omitted
    client2 = FakeForwardClient({})
    dashboard.save_forward_mapping(client2, "N1", "1", "a.cap", "  ")
    assert client2.calls[-1][2] == {"target_capability": "a.cap"}


def test_save_forward_mapping_validation_and_errors():
    client = FakeForwardClient({})
    ok, _ = dashboard.save_forward_mapping(client, "N1", "", "a.cap")
    assert not ok
    ok, _ = dashboard.save_forward_mapping(client, "N1", "1", "  ")
    assert not ok
    assert client.calls == []
    client = FakeForwardClient({
        ("PUT", "/relay/v2/forward-mappings/N1/1"): _Resp(403, {"detail": "Admin role required"}),
    })
    ok, msg = dashboard.save_forward_mapping(client, "N1", "1", "a.cap")
    assert not ok and "forbidden" in msg.lower()


def test_delete_forward_mapping_statuses():
    client = FakeForwardClient({})
    ok, msg = dashboard.delete_forward_mapping(client, "N1", "plan")
    assert ok, msg
    assert client.calls[-1] == ("DELETE", "/relay/v2/forward-mappings/N1/plan", None)

    client = FakeForwardClient({
        ("DELETE", "/relay/v2/forward-mappings/N1/plan"): _Resp(404, {"detail": "x"}),
    })
    ok, msg = dashboard.delete_forward_mapping(client, "N1", "plan")
    assert not ok and "no mapping" in msg


# ---------------------------------------------------------------------------
# Forward section rendering
# ---------------------------------------------------------------------------


def test_forward_section_forbidden_note():
    html = onboarding._forward_section({"forbidden": True, "ok": False})
    assert "nur f&uuml;r Admin-Nodes konfigurierbar" in html
    assert "/forward/save" not in html


def test_forward_section_error_note():
    html = onboarding._forward_section({"ok": False, "error": "relay down"})
    assert "relay down" in html
    assert "/forward/save" not in html


def test_forward_section_table_and_editor():
    panel = dashboard.fetch_forward_panel(_fake_client())
    html = onboarding._forward_section(panel)
    assert "Wartungsslot" in html
    assert "model.specialist" in html
    assert "action='/forward/save'" in html
    assert "action='/forward/delete'" in html
    assert "value='plan'" in html


def test_forward_section_no_slots_note():
    client = FakeForwardClient({
        "/relay/v2/forward-mappings/slots": _Resp(200, {"slots": []}),
        "/relay/v2/forward-mappings": _Resp(200, {"mappings": []}),
    })
    panel = dashboard.fetch_forward_panel(client)
    html = onboarding._forward_section(panel)
    assert "Keine Forward-Slots deklariert" in html


# ---------------------------------------------------------------------------
# Script upload (T-030)
# ---------------------------------------------------------------------------


def test_save_script_extension_allowlist(dash_env):
    ok, msg, stored = dashboard.save_script("tool.txt", b"x")
    assert not ok and "not allowed" in msg
    ok, msg, stored = dashboard.save_script("noext", b"x")
    assert not ok
    ok, msg, stored = dashboard.save_script("tool.BAT", b"echo hi")
    assert ok, msg
    assert stored == "tool.BAT"


def test_save_script_traversal_sanitized(dash_env):
    ok, msg, stored = dashboard.save_script("../../evil.bat", b"x", dash_env)
    assert ok, msg
    assert stored == "evil.bat"
    assert (dashboard.scripts_dir(dash_env) / "evil.bat").read_bytes() == b"x"


def test_save_script_size_cap(dash_env):
    ok, msg, _ = dashboard.save_script("big.bat", b"x" * (dashboard.MAX_SCRIPT_BYTES + 1))
    assert not ok and "too large" in msg
    ok, msg, _ = dashboard.save_script("exact.bat", b"x" * dashboard.MAX_SCRIPT_BYTES)
    assert ok, msg


def test_save_script_collision_numeric_suffix(dash_env):
    for _ in range(3):
        ok, msg, stored = dashboard.save_script("cleanup.py", b"same", dash_env)
        assert ok, msg
    names = sorted(p.name for p in dashboard.scripts_dir(dash_env).iterdir())
    assert names == ["cleanup.1.py", "cleanup.2.py", "cleanup.py"]


@pytest.mark.parametrize("filename,expected", [
    ("run.bat", ["cmd", "/c", "scripts\\run.bat"]),
    ("run.cmd", ["cmd", "/c", "scripts\\run.cmd"]),
    ("run.ps1", ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-File", "scripts\\run.ps1"]),
    ("run.py", ["python", "scripts\\run.py"]),
    ("run.sh", ["bash", "scripts/run.sh"]),
    ("run.exe", ["scripts\\run.exe"]),
])
def test_script_command_argv_per_extension(filename, expected):
    assert dashboard.script_command_argv(filename) == expected


@pytest.mark.parametrize("filename,expected", [
    ("My Script.bat", "my_script"),
    ("1tool.bat", "cap-1tool"),
    ("data_export.PY", "data_export"),
    ("wurstwasser-v2.cmd", "wurstwasser-v2"),
])
def test_slugify_capability_name(filename, expected):
    slug = dashboard.slugify_capability_name(filename)
    assert dashboard.CAP_NAME_RE.match(slug), slug
    assert slug == expected


def _multipart(filename: str, content: bytes, boundary: str = "iowaptest") -> tuple[str, bytes]:
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + content + f"\r\n--{boundary}--\r\n".encode()
    return f"multipart/form-data; boundary={boundary}", body


def _post_raw(server, path: str, content_type: str, body: bytes):
    req = urllib.request.Request(
        f"http://127.0.0.1:{server.server_address[1]}{path}", data=body, method="POST",
        headers={"Content-Type": content_type},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def test_script_upload_endpoint(dash_env):
    _configure(dash_env)
    srv = onboarding.start_dashboard(port=0)
    try:
        ct, body = _multipart("cleanup.bat", b"echo hi")
        status, text = _post_raw(srv, "/caps/script-upload", ct, body)
        assert status == 200, text
        data = json.loads(text)
        assert data["ok"] is True
        assert data["stored"] == "cleanup.bat"
        assert data["name"] == "cleanup"
        assert data["command"] == ["cmd", "/c", "scripts\\cleanup.bat"]
        assert (dashboard.scripts_dir(dash_env / "workspace") / "cleanup.bat").read_bytes() == b"echo hi"

        # allowlisted extension enforced at the endpoint too
        ct, body = _multipart("evil.txt", b"x")
        status, text = _post_raw(srv, "/caps/script-upload", ct, body)
        assert status == 400
        assert json.loads(text)["ok"] is False
    finally:
        onboarding.shutdown(srv)


def test_multipart_parser_extracts_file():
    ct, body = _multipart("tool.ps1", b"Write-Output 1")
    part = onboarding._parse_multipart_file(ct, body)
    assert part is not None
    filename, content = part
    assert filename == "tool.ps1"
    assert content == b"Write-Output 1"
    assert onboarding._parse_multipart_file("text/plain", body) is None


# ---------------------------------------------------------------------------
# Dashboard HTML: form fields + forward section wired in
# ---------------------------------------------------------------------------


def test_dashboard_renders_capacity_fields_and_forward_section(dash_env, monkeypatch):
    _configure(dash_env)
    monkeypatch.setattr(dashboard, "fetch_forward_panel", lambda *a, **k: {
        "ok": True, "forbidden": False, "error": None, "node_id": "N1",
        "slots": SLOTS_BODY["slots"], "mappings": {"plan": MAPPINGS_BODY["mappings"][0]},
    })
    srv = onboarding.start_dashboard(port=0)
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{srv.server_address[1]}/", timeout=10
        ) as resp:
            html = resp.read().decode()
    finally:
        onboarding.shutdown(srv)
    # capability form: capacity + artifacts glob + script picker
    assert "name='max_parallel'" in html
    assert "name='cost'" in html
    assert "name='artifacts_glob'" in html
    assert "cap_script" in html
    assert "/caps/script-upload" in html
    # forward section with editor
    assert "Forward-Slots" in html
    assert "action='/forward/save'" in html
    assert "value='plan'" in html


def test_dashboard_renders_forbidden_forward_note(dash_env, monkeypatch):
    _configure(dash_env)
    monkeypatch.setattr(dashboard, "fetch_forward_panel", lambda *a, **k: {
        "ok": False, "forbidden": True, "error": None, "node_id": "N1",
        "slots": [], "mappings": {},
    })
    srv = onboarding.start_dashboard(port=0)
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{srv.server_address[1]}/", timeout=10
        ) as resp:
            html = resp.read().decode()
    finally:
        onboarding.shutdown(srv)
    assert "nur f&uuml;r Admin-Nodes konfigurierbar" in html
    assert "action='/forward/save'" not in html
