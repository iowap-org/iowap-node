"""T-179: handler primitives — ``node-cli hp put/get``.

Scripts reden ausschließlich mit dem lokalen node-cli (Subprocess-Contract,
Board-Entscheid ①): Token, Ladder-Verhandlung und Transfer-Formate bleiben
im CLI-Prozess. Scripts sehen nur Pfad rein / Pfad raus.

FROZEN CONTRACT (Phase 1, T-179)
================================
Jede öffentliche Signatur in diesem Modul ist unveränderlich für Phase 2
(scaffold: Test-Stubs + Parser-Boilerplate) und Phase 3 (integrate:
Implementierung). Insbesondere FROZEN:

* Parameternamen + -reihenfolge (Tests der Phasen 2/3 rufen positional
  und keyword auf — beides muss stabil bleiben),
* Rückgabe-Typen (``int`` exit codes; ``0`` ok / ``1`` Fehler / ``2`` usage),
* stdout-Verträge (je EINE compacte JSON-Zeile, siehe cmd_put/cmd_get),
* Fehler-Meldungs-Strings von ``decide_mode`` (Task 2b benötigt sie
  byte-identisch zu ``cli_file._choose_mode``).

Haus-Konvention (wie cli_file/cli_task, T-117 split): CLI-Handler sind
``(client, args) -> int`` und werden bei der Parser-Registrierung in
``node_cli.py`` mit ``with_client(...)`` dekoriert. Dieses Modul
importiert ``node_cli`` NICHT (kein circular import, RelayClient-
Monkeypatch bleibt funktionsfähig).

Phase 1 liefert nur Signaturen + Vertrags-Dokumentation; die Körper
werden in Phase 3 implementiert (Phase 2 erzeugt ``NotImplementedError``-
Test-Stubs, die hier bereits vorbereitet sind).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, TextIO

import httpx

from nodes.common.cli.cli_file import _load_capability_modes  # noqa: F401
from nodes.common.envelope import (  # noqa: F401
    ENVELOPE_KEY,
    EnvelopeError,
    dump,
    is_envelope,
    make_envelope,
    parse_envelope,
)
from nodes.common.relay_client import RelayClient

# Reihenfolge = Präferenz; erste erlaubte Stufe, deren Schwellwert passt.
# FROZEN: Reihenfolge ist Vertragsbestandteil (inline < artifact < bridge).
_LADDER = ("inline", "artifact", "bridge")


def decide_mode(
    size_bytes: int,
    upload_modes: list[str],
    thresholds: dict[str, int] | None = None,
    force: str | None = None,
) -> str:
    """Wähle die Ladder-Stufe für eine Datei — der geteilte Entscheid-Punkt.

    FROZEN SIGNATUR (Phase 1) — Task 2b lässt ``cli_file._choose_mode``
    über diese Funktion laufen; deshalb sind auch die Fehlermeldungen
    FROZEN (siehe unten).

    Semantik
    --------
    * Ladder-Präferenz: erste Stufe aus ``_LADDER``, die (a) in
      ``upload_modes`` erlaubt ist und (b) deren Schwellwert die Größe
      trägt.
    * ``thresholds``: Keys ``max_inline_bytes`` / ``max_artifact_bytes``
      (vom Server, ``RelayClient.get_transfer_config()``).
      - Key fehlt oder ist ``None`` → Stufe gilt als **unbegrenzt**
        (Safety-Net ist der Server-Payload-Limit, kein stiller Fehler).
      - Explicit ``0`` → Stufe **verboten** (cli_file-Semantik bei
        fehlender Server-Antwort).
      - ``thresholds=None`` → alle Stufen unbegrenzt → reine
        Präferenz-Wahl nach ``upload_modes``.
    * ``bridge`` hat keinen Threshold-Key (Server definiert keinen) →
      unbegrenzt, wird gewählt, sobald inline/artifact nicht passen.
    * ``force``: überschreibt die Wahl, muss aber in ``upload_modes``
      sein. Wird VOR der Größen-Logik geprüft (wie cli_file).

    Fehler (FROZEN, byte-identisch zu ``cli_file._choose_mode``)
    ------------------------------------------------------------
    Raise ``ValueError`` (Library-Level; CLI-Adapter wandeln in
    SystemExit/exit-code um):

    * force nicht erlaubt::

        --force {force!r} not supported by capability (upload_modes={upload_modes})

    * keine Stufe passt::

        file too big: {size_bytes} bytes, capability supports only {upload_modes} (server ladder: inline<={max_inline}, artifact<={max_artifact})

      mit ``max_inline = (thresholds or {}).get("max_inline_bytes", 0)`` usw.

    Task-2b-Mapping (FROZEN, verhaltensidentisch) in ``cli_file.py``::

        thresholds = {
            "max_inline_bytes": int(transfer_cfg.get("max_inline_bytes", 0)),
            "max_artifact_bytes": int(transfer_cfg.get("max_artifact_bytes", 0)),
        }
        try:
            return decide_mode(size, upload_modes, thresholds, force=force)
        except ValueError as exc:
            raise SystemExit(str(exc))

    (cli_file materialisiert die 0-Defaults explizit — damit bleibt das
    "fehlender Key = unbegrenzt"-Verhalten von decide_mode ohne Wirkung
    auf den cli_file-Pfad.)
    """
    if force is not None:
        if force not in upload_modes:
            raise ValueError(
                f"--force {force!r} not supported by capability "
                f"(upload_modes={upload_modes})"
            )
        return force

    th = thresholds or {}
    max_inline = th.get("max_inline_bytes")
    max_artifact = th.get("max_artifact_bytes")

    if "inline" in upload_modes and (max_inline is None or size_bytes <= max_inline):
        return "inline"
    if "artifact" in upload_modes and (
        max_artifact is None or size_bytes <= max_artifact
    ):
        return "artifact"
    if "bridge" in upload_modes:
        return "bridge"
    raise ValueError(
        f"file too big: {size_bytes} bytes, capability supports only {upload_modes} "
        f"(server ladder: inline<={(th or {}).get('max_inline_bytes', 0)}, "
        f"artifact<={(th or {}).get('max_artifact_bytes', 0)})"
    )


def cmd_put(
    client: RelayClient,
    *,
    cap: str,
    path: Path,
    name: str | None = None,
    out: TextIO | None = None,
) -> int:
    """``node-cli hp put <path> --cap <cap>`` → Umschlag-JSON auf stdout.

    FROZEN SIGNATUR (Phase 1).

    Vertrag
    -------
    * Lädt Capability-Details via ``_load_capability_modes(client, cap)``
      (``upload_modes``; dessen ``SystemExit`` bei HTTP-/Lookup-Fehlern
      propagiert — ``main()`` wandelt es in den exit code).
    * Thresholds via ``client.get_transfer_config()``; defensiv extrahieren
      (nur echte ints übernehmen, alles andere = Key weglassen → Stufe
      unbegrenzt). HTTP-Fehler hier → ``return 1`` mit klarer Meldung.
    * ``mode = decide_mode(size, upload_modes, thresholds)`` mit
      ``size = path.stat().st_size`` (NICHT erst ganz einlesen — artifact/
      bridge sollen große Dateien ohne RAM-Peak handhaben; inline liest
      die Bytes erst nach der Entscheidung). ``ValueError`` aus
      decide_mode → stderr + ``return 1``.
    * inline  → ``make_envelope(src="inline", filename=name or path.name,
      data=<bytes>)``.
    * artifact → ``client.upload_artifact(path, name=name or path.name)``;
      fehlende ``artifact_id`` in der Antwort → stderr + ``return 1``;
      sonst ``make_envelope(src="artifact", ..., artifact_id=aid)``.
    * bridge → ``raise NotImplementedError("bridge-put folgt in Task 3")``
      (MVP-Grenze, bewusst — Envelope kann bridge, der Kanal-Open-Flow
      nicht; siehe Plan T-179).
    * stdout: GENAU EINE Zeile — compactes JSON des Umschlags
      (``envelope.dump``) + ``"\\n"``. ``out=None`` → ``sys.stdout`` zur
      CALL-ZEIT (nicht Import-Zeit!) auflösen, damit pytest-capsys
      funktioniert. Keine anderen stdout-Ausgaben; Fehler → stderr.
    * Exit codes: ``0`` ok, ``1`` Transfer-/Protokollfehler,
      ``2`` usage (Datei fehlt — geprüft im dispatch_put).

    ``name`` überschreibt den Dateinamen im Umschlag (Default: path.name).
    """
    if not path.is_file():
        print(f"file not found: {path}", file=sys.stderr)
        return 2

    # 1. Capability-Details (upload_modes); SystemExit bei Lookup-Fehlern
    #    propagiert — main() macht daraus den exit code.
    cap_detail = _load_capability_modes(client, cap)
    upload_modes = list(
        cap_detail.get("upload_modes") or ["inline", "artifact", "bridge"]
    )

    # 2. Server-Treppen-Konfig laden; defensiv: nur echte ints übernehmen,
    #    alles andere = Key weglassen → Stufe unbegrenzt (F2.5-Semantik).
    try:
        transfer_cfg = client.get_transfer_config()
    except httpx.HTTPError as exc:
        print(f"failed to load transfer config: {exc}", file=sys.stderr)
        return 1
    thresholds: dict[str, int] = {}
    if isinstance(transfer_cfg, dict):
        for key in ("max_inline_bytes", "max_artifact_bytes"):
            value = transfer_cfg.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                thresholds[key] = value

    # 3. Größe via stat (F2.9) — Bytes erst nach der Entscheidung lesen.
    size = path.stat().st_size
    try:
        mode = decide_mode(size, upload_modes, thresholds)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    # 4. Umschlag je Modus bauen.
    if mode == "inline":
        envelope = make_envelope(
            src="inline", filename=name or path.name, data=path.read_bytes()
        )
    elif mode == "artifact":
        try:
            resp = client.upload_artifact(path, name=name or path.name)
        except httpx.HTTPError as exc:
            print(f"artifact upload failed: {exc}", file=sys.stderr)
            return 1
        artifact_id = resp.get("artifact_id") if isinstance(resp, dict) else None
        if not artifact_id:
            print(
                "artifact upload response without artifact_id", file=sys.stderr
            )
            return 1
        envelope = make_envelope(
            src="artifact",
            filename=name or path.name,
            artifact_id=str(artifact_id),
        )
    else:  # bridge — MVP-Grenze (F2.10), bewusst.
        raise NotImplementedError("bridge-put folgt in Task 3")

    # 5. stdout: GENAU EINE compacte JSON-Zeile; out=None → sys.stdout zur
    #    CALL-Zeit (capsys-kompatibel).
    stream = out if out is not None else sys.stdout
    stream.write(dump(envelope) + "\n")
    return 0


def cmd_get(
    client: RelayClient,
    *,
    envelope: dict[str, Any],
    out_dir: Path,
    output: Path | None = None,
    out: TextIO | None = None,
) -> int:
    """``node-cli hp get`` → Umschlag auflösen, Datei lokal ablegen.

    FROZEN SIGNATUR (Phase 1).

    Vertrag
    -------
    * ``parse_envelope(envelope)`` → ``(src, ref)``; ``EnvelopeError`` →
      ``"hp get: {exc}"`` auf stderr + ``return 1``.
    * Dateiname: ``envelope["__iowap_ref__"].get("filename") or
      "download"`` (Filename aus dem Umschlag ist NICHT trustbar als
      Pfad — keine ``..``/absoluten Pfade akzeptieren, sanitizen).
    * ``out_dir`` wird bei Bedarf angelegt (``mkdir(parents=True,
      exist_ok=True)``) — auch bei ``output``-Override, damit der
      Default-Pfad-Zweig konsistent bleibt.
    * inline  → ``target = output or out_dir / filename``;
      ``target.write_bytes(ref)``.
    * artifact → ``target = client.download_artifact(ref, output or
      out_dir / filename)`` (RelayClient kümmert sich ums Streaming).
    * bridge  → stderr ``"hp get: bridge resolution not supported by
      this node-cli version"`` + ``return 1`` (MVP-Grenze, exakter
      Meldungsstring ist FROZEN — Plan T-179 Task 3).
    * sha256-Verifikation, wenn der Umschlag einen trägt (inline trägt
      immer einen): mismatch → stderr
      ``"hp get: sha256 mismatch ({digest} != {expected})"`` + ``return 1``.
    * stdout: GENAU EINE Zeile — compactes JSON
      ``{"path": str(target), "size_bytes": target.stat().st_size,
      "src": <mode>}`` + ``"\\n"`` (Keys FROZEN). ``out=None`` →
      ``sys.stdout`` zur CALL-ZEIT (capsys-kompatibel).
    * Exit codes: ``0`` ok, ``1`` Auflöse-/Verifikationsfehler,
      ``2`` usage.
    """
    try:
        src, ref = parse_envelope(envelope)
    except EnvelopeError as exc:
        print(f"hp get: {exc}", file=sys.stderr)
        return 1

    # Dateiname aus dem Umschlag ist NICHT trustbar (keine ../absoluten
    # Pfade) — sanitizen.
    raw_name = (envelope.get(ENVELOPE_KEY) or {}).get("filename")
    if not isinstance(raw_name, str) or not raw_name:
        raw_name = "download"
    safe_name = Path(raw_name).name
    if safe_name in ("", ".", ".."):
        safe_name = "download"

    out_dir.mkdir(parents=True, exist_ok=True)

    if src == "bridge":
        print(
            "hp get: bridge resolution not supported by this node-cli version",
            file=sys.stderr,
        )
        return 1

    if src == "inline":
        target = output or out_dir / safe_name
        target.write_bytes(ref)
    else:  # artifact — RelayClient kümmert sich ums Streaming.
        try:
            target = client.download_artifact(ref, output or out_dir / safe_name)
        except httpx.HTTPError as exc:
            print(f"hp get: artifact download failed: {exc}", file=sys.stderr)
            return 1

    # sha256-Verifikation, wenn der Umschlag einen trägt (inline immer).
    expected = (envelope.get(ENVELOPE_KEY) or {}).get("sha256")
    if expected:
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        if digest != expected:
            print(
                f"hp get: sha256 mismatch ({digest} != {expected})",
                file=sys.stderr,
            )
            try:
                target.unlink()
            except OSError:
                pass
            return 1

    stream = out if out is not None else sys.stdout
    stream.write(
        json.dumps(
            {
                "path": str(target),
                "size_bytes": target.stat().st_size,
                "src": src,
            },
            separators=(",", ":"),
        )
        + "\n"
    )
    return 0


def default_out_dir() -> Path:
    """Default-Ablageverzeichnis für ``hp get``: ``~/.relay/tmp/<task_id>/``.

    FROZEN SIGNATUR (Phase 1). Task-ID aus ``RELAY_TASK_ID`` (vom
    handler_runner an jeden Handler vergeben, siehe
    ``handler_runner.HANDLER_ENV_KEYS``); ohne Handler-Kontext →
    ``adhoc``. Analog zu ``node_config.BASE_DIR`` (``~/.relay``) —
    derselbe Stamm wie Profile/Meta, kein neuer Wurzelordner.
    """
    task_id = __import__("os").environ.get("RELAY_TASK_ID") or "adhoc"
    return Path.home() / ".relay" / "tmp" / task_id


def dispatch_put(client: RelayClient, args: argparse.Namespace) -> int:
    """Argparse-Adapter: ``node-cli hp put <path> --cap <cap> [--name N]``.

    FROZEN SIGNATUR (Phase 1). Haus-Konvention ``(client, args) -> int``;
    Registrierung in ``node_cli.py`` (Phase 2)::

        p_hp_put.set_defaults(func=with_client(cli_hp.dispatch_put))

    Namespace-Attribute (FROZEN, Phase 2 registriert genau diese):

    * ``args.path``   (str, positional)   — Datei, existenzgeprüft hier:
      fehlt → stderr + ``return 2`` (wie cli_file._cmd_file_send).
    * ``args.cap``    (str, --cap, required)
    * ``args.name``   (str | None, --name, default None)

    Delegiert an ``cmd_put(client, cap=args.cap, path=Path(args.path),
    name=args.name)``.
    """
    path = Path(args.path)
    if not path.is_file():
        print(f"file not found: {path}", file=sys.stderr)
        return 2
    return cmd_put(client, cap=args.cap, path=path, name=args.name)


def dispatch_get(client: RelayClient, args: argparse.Namespace) -> int:
    """Argparse-Adapter: ``node-cli hp get [--file F] [--output P]``.

    FROZEN SIGNATUR (Phase 1). Haus-Konvention ``(client, args) -> int``;
    Registrierung in ``node_cli.py`` (Phase 2)::

        p_hp_get.set_defaults(func=with_client(cli_hp.dispatch_get))

    Namespace-Attribute (FROZEN, Phase 2 registriert genau diese):

    * ``args.file``    (str | None, --file, default None) — Umschlag-JSON
      als Datei; ohne --file wird stdin gelesen (``sys.stdin``, Call-Zeit).
    * ``args.output``  (Path | None, --output/-o, default None)
    * ``args.out_dir`` (Path | None, --out-dir, default None) — Override,
      sonst ``default_out_dir()``.

    Eingabe muss ein JSON-Objekt mit ``__iowap_ref__`` sein; ungültiges
    JSON → stderr + ``return 2`` (usage). Delegiert an ``cmd_get``.
    """
    if args.file:
        try:
            raw = Path(args.file).read_text(encoding="utf-8")
        except OSError as exc:
            print(f"hp get: cannot read {args.file}: {exc}", file=sys.stderr)
            return 2
    else:
        raw = sys.stdin.read()

    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"hp get: invalid JSON: {exc}", file=sys.stderr)
        return 2
    if not isinstance(envelope, dict) or ENVELOPE_KEY not in envelope:
        print(
            f"hp get: no {ENVELOPE_KEY} key — not an envelope",
            file=sys.stderr,
        )
        return 2

    out_dir = args.out_dir if args.out_dir is not None else default_out_dir()
    return cmd_get(
        client, envelope=envelope, out_dir=out_dir, output=args.output
    )
