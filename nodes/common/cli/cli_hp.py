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
from pathlib import Path
from typing import Any, TextIO

from nodes.common.cli.cli_file import _load_capability_modes  # noqa: F401
from nodes.common.envelope import (  # noqa: F401 — genutzt ab Phase 3
    EnvelopeError,
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
    raise NotImplementedError("Phase 3 (T-179 Task 2/2b): implement decide_mode")


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
    raise NotImplementedError("Phase 3 (T-179 Task 2): implement cmd_put")


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
    raise NotImplementedError("Phase 3 (T-179 Task 3): implement cmd_get")


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
    raise NotImplementedError("Phase 3 (T-179 Task 2): implement dispatch_put")


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
    raise NotImplementedError("Phase 3 (T-179 Task 3): implement dispatch_get")