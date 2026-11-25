#!/usr/bin/env python3
"""T-045: auto-recompile of the active profile after capability mutations.

Ronny's model („fertige YAML → node.yaml“): every capability mutation
(create / update / delete / publish toggle — dashboard, MCP or CLI)
recomposes the ACTIVE ``node.yaml`` so its ``capabilities`` section is
exactly the published view:

* the operator's local entries ride along untouched;
* every provider entry that IS published (the
  :func:`dashboard.is_published` view: allowlist membership, or the
  legacy ``auto_publish``-on-empty-allowlist state) is baked as a
  routing-complete ``host.*`` capability entry;
* nothing else in the file moves (load → mutate capabilities → dump,
  T-035/T-026 pattern).

The heartbeat wire therefore offers exactly the published set — and the
relay's Full-Replace ``worker-heartbeat`` drops un-published rows.

**Gate layering (the T-045 gate trace):**

* Layer 1 — bridge-level ``bridge._publish_allowed``
  (``bridge.allowlist`` / ``bridge.auto_publish``): decides the RUNTIME
  registry, i.e. which ``host.*`` dicts the merge provider appends for
  claim routing.
* Layer 2 — cap-level ``auto_publish``: since T-045 ``build_bridge_cap``
  stamps ``False``, the merged registry dicts never blanket-ride the
  heartbeat wire anymore.
* The wire carries provider caps ONLY as the recomposed allowlisted
  entries of the active profile — for bridge caps, compose == publish.

Contract: a compose that fails profile validation writes NOTHING (the
file stays byte-identical, callers flash the reason); a successful
compose writes atomically (temp + rename) and SIGHUPs BOTH daemon pid
files best-effort afterwards (the publish t_c214ca25 pattern — a
not-running daemon picks the file up on its next start).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Callable

import yaml

from nodes.common import bridge as bridge_mod
from nodes.common import dashboard, node_config, node_utils

log = logging.getLogger("active-profile")

# The note of the LAST recompile (kept for the dashboard flash). The
# onboarding server renders it right after the store function that
# triggered the recompose returned — same process, same request chain.
_LAST_NOTE = ""


def _note(msg: str) -> None:
    """Remember the last recompile outcome for the dashboard flash."""
    global _LAST_NOTE
    _LAST_NOTE = msg


def last_recompile_note() -> str:
    """The message of the most recent :func:`recompile_active_profile` call.

    Empty when the recompile pipeline has not run in this process yet —
    never a synthetic "unchanged" filler (the flash must only ever echo
    a recompile that actually happened).
    """
    return _LAST_NOTE


def _composed_bridge_cap(raw: dict[str, Any]) -> dict[str, Any]:
    """One providers.yaml entry → one routing-complete active-profile entry.

    The base is ``bridge.build_bridge_cap`` (the runtime merge dict —
    host.-prefixed name, version/description/input_schema/max_parallel/
    cost/timeout/long_run and the ``bridge``/``bridge_name`` routing
    pair). Two T-045 adjustments for the FILE representation:

    * ``auto_publish`` is dropped — the offered-state lives in
      ``bridge.allowlist`` now, not in a per-cap flag (the load-time
      default True is exactly what "this entry IS published" means);
    * ``claimable=False`` and no local ``handler`` — provider caps are
      never executed locally; the ``bridge`` marker routes claimed
      stages to :func:`bridge.execute_bridge_task`.
    """
    name = str(raw.get("name") or "")
    cap = dict(bridge_mod.build_bridge_cap(name, raw))
    cap.pop("auto_publish", None)
    cap.pop("handler", None)
    cap["claimable"] = False
    return cap


# ---------------------------------------------------------------------------
# T-046: single-key active-profile mutations (budget.total, forward_slots)
# ---------------------------------------------------------------------------


def _apply_active_mutation(
    mutate: Callable[[dict[str, Any]], None],
    *,
    what: str,
    path: Path | None = None,
) -> tuple[bool, str]:
    """load → mutate ONE key → validate → atomic dump → SIGHUP (the
    recompile pattern, generalised for T-046).

    A validation failure writes NOTHING (byte-identical file) and the
    error rides the message — the caller flashes it. ``(ok, message)``.
    """
    p = path or node_config.ACTIVE_PATH
    if not p.exists():
        return False, f"{what}: no active node.yaml — nothing written"
    try:
        doc = yaml.safe_load(p.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return False, f"{what}: cannot parse {p}: {exc}"
    if not isinstance(doc, dict):
        return False, f"{what}: {p} root is not a mapping — nothing written"

    before = yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)
    try:
        mutate(doc)
    except (ValueError, LookupError) as exc:
        # mutating closures signal refuse-with-context via these — the
        # POST handler turns the message into the 400 flash.
        return False, f"{what}: {exc}"
    after = yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)
    if after == before:
        return True, f"{what}: unchanged (already in this state)"

    try:
        node_config.validate_profile(doc)
    except node_config.CapabilityValidationError as exc:
        return False, f"{what} failed (node.yaml untouched): {exc}"

    try:
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(
            after,
            encoding="utf-8",
        )
        tmp.replace(p)
    except OSError as exc:
        return False, f"{what}: write to {p} failed: {exc}"

    pids = _sighup_daemons()
    note = f"{what} saved (recompiled active profile)"
    if pids:
        note += f", SIGHUP {', '.join(str(p) for p in pids)}"
    log.info("%s", note)
    return True, note


def _sighup_daemons() -> list[int]:
    """Best-effort SIGHUP to both daemon pid files (the recompile tail)."""
    from nodes.common import node_daemon as _nd
    from nodes.common import poll_daemon as _pd

    return node_utils.sighup_pid_files([_nd.PID_PATH, _pd.PID_PATH])


def write_budget_total(total: int, *, path: Path | None = None) -> tuple[bool, str]:
    """Write ``budget: {total: N}`` (mapping form) into the active profile.

    T-046: the Dashboard-Feld posts here (POST /budget/save). ``total``
    is an int ≥ 0; the mapping form coexists with the legacy T-247 int
    (which keeps its admission-axis meaning AND now serves as the
    accounting ceiling — see ``budget.budget_total``).
    """
    total = int(total)
    if total < 0:
        return False, "budget total must be >= 0"

    def mutate(doc: dict[str, Any]) -> None:
        doc["budget"] = {"total": total}

    return _apply_active_mutation(mutate, what=f"budget total {total}", path=path)


def write_busy_status(busy: bool, *, path: Path | None = None) -> tuple[bool, str]:
    """T-046 busy toggle → the T-081 ``status`` field (busy/idle).

    The active YAML ``status`` field IS the persisted explicit-busy
    store the operator knows from ``node-cli node busy``/``idle``
    (T-084): it survives daemon restarts (fresh read per heartbeat,
    ``node_config.load_active_status``) and the heartbeat already
    forwards it as the relay-honoured explicit status request. The
    brief's „runtime-Block (`busy: true`)“ is thereby implemented via
    the field the relay ACTUALLY honours (the coder-resolved exact form).
    ON → ``status: busy``; OFF → ``status: idle`` (toggle-off reverts
    the node to the idle state, per the T-046 brief).

    When the file already carries the ``status:`` line it is edited as
    text (comments/ordering intact — the T-084 write pattern); otherwise
    the document goes through the load-mutate-dump path with the full
    profile revalidation. The regex sits on :py:data:`_STATUS_LINE_RE`
    (column-0 ``status:`` — capability-level keys are indented, those
    never match).
    """
    p = path or node_config.ACTIVE_PATH
    value = "busy" if busy else "idle"
    if not p.exists():
        return False, "busy toggle: no active node.yaml — nothing written"
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError as exc:
        return False, f"busy toggle: cannot read {p}: {exc}"
    if _STATUS_LINE_RE.search(raw):
        new_text = _STATUS_LINE_RE.sub(lambda m: m.group(1) + value, raw)
        if new_text != raw:
            try:
                parsed = yaml.safe_load(new_text)
                if isinstance(parsed, dict):
                    node_config.validate_profile(parsed)
            except (node_config.CapabilityValidationError, yaml.YAMLError) as exc:
                return False, f"busy toggle failed (node.yaml untouched): {exc}"
            tmp = p.with_suffix(p.suffix + ".tmp")
            tmp.write_text(new_text, encoding="utf-8")
            tmp.replace(p)
            pids = _sighup_daemons()
            note = (
                f"node status set to {value} (text edit)"
                + (f", SIGHUP {pids[0]}" if pids else "")
            )
            log.info("%s", note)
            return True, note

    def mutate(doc: dict[str, Any]) -> None:
        doc["status"] = value

    return _apply_active_mutation(mutate, what=f"node status {value}", path=p)


_STATUS_LINE_RE = re.compile(r"^(status:\s*).*$", re.MULTILINE)


def upsert_forward_slot(
    slot: str, entry: dict[str, Any], *, path: Path | None = None
) -> tuple[bool, str]:
    """T-046 DEFINE: create/replace ONE forward_slots row (node.yaml).

    The dashboard's slot-definition form (label/type/value/ref, T-245
    typed) posts here. Both documented section shapes are supported
    (node/config.md): the mapping form ``slot: {..}`` (keyed mutation)
    and the list form ``- slot: ...`` (the matching row is replaced in
    place, a new row is appended). Legacy description-string rows (no
    ``slot`` key) are preserved on both paths — never destroyed. The
    WHOLE section is re-validated (T-239 fail-fast: the T-235 row
    pipeline + the ref-target crosscheck) BEFORE anything is written.
    """
    slot_name = slot.strip() if isinstance(slot, str) else ""
    if not slot_name:
        return False, "slot name is required"
    if not isinstance(entry, dict):
        return False, "slot entry must be a mapping"
    entry = dict(entry)
    entry.setdefault("slot", slot_name)

    def _mutate_mapping(section: dict[str, Any]) -> None:
        # the mapping form keys the slot OUTSIDE the entry — a stray
        # ``slot`` key inside would be tolerated by the row pipeline but
        # is not the documented shape (node/config.md), so it is stripped.
        section[slot_name] = {k: v for k, v in entry.items() if k != "slot"}

    def _mutate_list(section: list[Any]) -> None:
        # matching rows keep their position, others untouched; a missing
        # slot is appended so first-definition order is stable.
        for idx, row in enumerate(list(section)):
            if isinstance(row, dict) and str(row.get("slot", "")).strip() == slot_name:
                section[idx] = entry
                return
        section.append(entry)

    def mutate(doc: dict[str, Any]) -> None:
        section = doc.get("forward_slots")
        if section is None:
            doc["forward_slots"] = [entry]
            return
        if isinstance(section, dict):
            _mutate_mapping(section)
            return
        if isinstance(section, list):
            _mutate_list(section)
            return
        raise ValueError(
            "forward_slots must be a mapping or a list of objects —"
            " edit node.yaml manually and try again"
        )

    return _apply_active_mutation(mutate, what=f"forward slot '{slot_name}'", path=path)


def remove_forward_slot(slot: str, *, path: Path | None = None) -> tuple[bool, str]:
    """T-046: delete ONE forward_slots row; an emptied section is dropped."""
    slot_name = slot.strip() if isinstance(slot, str) else ""
    if not slot_name:
        return False, "slot name is required"

    def mutate(doc: dict[str, Any]) -> None:
        section = doc.get("forward_slots")
        if isinstance(section, dict):
            if slot_name not in section:
                raise LookupError(f"slot '{slot_name}' is not defined")
            section = dict(section)
            del section[slot_name]
            if section:
                doc["forward_slots"] = section
            else:
                doc.pop("forward_slots", None)
            return
        if isinstance(section, list):
            hits = [
                idx
                for idx, row in enumerate(section)
                if isinstance(row, dict) and str(row.get("slot", "")).strip() == slot_name
            ]
            if not hits:
                raise LookupError(f"slot '{slot_name}' is not defined")
            remaining = [
                row
                for idx, row in enumerate(section)
                if idx not in hits
            ]
            if remaining:
                doc["forward_slots"] = remaining
            else:
                doc.pop("forward_slots", None)
            return
        raise ValueError(
            "forward_slots must be a mapping or a list of objects —"
            " edit node.yaml manually"
        )

    return _apply_active_mutation(mutate, what=f"remove forward slot '{slot_name}'", path=path)


def recompile_active_profile(
    path: Path | None = None,
    *,
    providers_path: Path | None = None,
) -> tuple[bool, str]:
    """Recompose the active profile's capabilities from the published set.

    Call after EVERY capability mutation (dashboard create/update/delete,
    ``set_capability_published`` add/remove, MCP and CLI variants — they
    all drive the dashboard store functions). Returns ``(ok, message)``:
    the message is the flash/reason text; ``ok=False`` means NOTHING was
    written (validation failed on the composed profile).

    Non-capability blocks of the active file survive: the document is
    loaded, ONLY ``capabilities`` is replaced, everything else dumps back
    exactly as it was loaded (T-035/T-026 load-mutate-dump).
    """
    global _LAST_NOTE
    p = path or node_config.ACTIVE_PATH
    if not p.exists():
        _note("no active node.yaml — nothing recomposed")
        return True, _LAST_NOTE
    try:
        doc = yaml.safe_load(p.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        _note(f"recompile failed: cannot parse {p}: {exc}")
        return False, f"recompile failed: cannot parse {p}: {exc}"
    if doc is None:
        doc = {}
    if not isinstance(doc, dict):
        _note(f"recompile failed: {p} root is not a mapping — nothing written")
        return False, f"recompile failed: {p} root is not a mapping"

    existing = doc.get("capabilities")
    if existing is None:
        locals_: list[dict[str, Any]] = []
        baked_old: list[dict[str, Any]] = []
    elif isinstance(existing, list):
        # true locals (operator/profile entries) ride along untouched;
        # PREVIOUSLY baked entries (bridge-marked) are regenerated — an
        # update_provider version bump or a pulled-back allowlist name
        # must be reflected, they are compose OUTPUT, not operator state.
        locals_ = [c for c in existing if isinstance(c, dict) and not c.get("bridge")]
        baked_old = [c for c in existing if isinstance(c, dict) and c.get("bridge")]
    else:
        # a broken section shape: pass it through untouched, the
        # validation below fails loudly — never silently rewrite garbage
        locals_ = None  # type: ignore[assignment]
        baked_old = []
    local_names: set[Any] = {c.get("name") for c in locals_} if locals_ is not None else set()

    bridge_cfg = bridge_mod.load_bridge_config()
    providers = [entry for entry in dashboard.load_providers(providers_path) if isinstance(entry, dict)]

    composed: list[dict[str, Any]] = []
    skipped: list[str] = []
    if locals_ is not None:
        for entry in providers:
            name = entry.get("name")
            if not isinstance(name, str) or not name.strip():
                log.warning("recompile: provider entry without a usable name skipped: %r", entry)
                continue
            # T-045 publish filter: EXACTLY the dashboard's published view
            # (allowlist membership / wildcard, or the legacy
            # auto_publish-on-empty-allowlist state). For a bridged node
            # this IS the allowlist — the checkbox render and the compose
            # share one definition of "published".
            if not dashboard.is_published(name, bridge_cfg):
                continue
            cap = _composed_bridge_cap(entry)
            if cap["name"] in local_names:
                # the operator pinned this name locally (profiles rule);
                # the local entry keeps the name (same namespace rule the
                # bridge manifest merge applies)
                skipped.append(str(cap["name"]))
                continue
            composed.append(cap)

    if locals_ is not None:
        doc["capabilities"] = [*locals_, *composed]

    # Validate BEFORE writing — a compose that does not validate as an
    # active profile must never reach disk (byte-identical file).
    try:
        node_config.validate_profile(doc)
    except node_config.CapabilityValidationError as exc:
        _note(f"recompile failed (node.yaml untouched): {exc}")
        return False, f"recompile failed (node.yaml untouched): {exc}"

    try:
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(
            yaml.safe_dump(doc, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        tmp.replace(p)
    except OSError as exc:
        _note(f"recompile failed: write to {p} failed: {exc}")
        return False, f"recompile failed: write to {p} failed: {exc}"

    # SIGHUP both daemon pid files best-effort (t_c214ca25 pair) — a
    # not-running daemon picks the file up on its next start.
    from nodes.common import node_daemon as _nd
    from nodes.common import poll_daemon as _pd

    pids = node_utils.sighup_pid_files([_nd.PID_PATH, _pd.PID_PATH])

    _note(
        f"recompiled active profile: {len(composed)} published bridge cap(s) baked"
        + (f", skipped (local collision): {', '.join(skipped)}" if skipped else "")
        + (f", SIGHUP {', '.join(str(p) for p in pids)}" if pids else "")
    )
    log.info(
        "active profile recomposed: %d local + %d published bridge entries"
        + (", skipped: %s" if skipped else ""),
        len(locals_ or []),
        len(composed),
        *([", ".join(skipped)] if skipped else []),
    )
    return True, _LAST_NOTE
