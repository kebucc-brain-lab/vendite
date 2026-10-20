"""labserver -- controllo di un Qdrant di LABORATORIO per i tool di engine/rag/tools (mai un cluster vero).

Estratto da `kit/rag/netfail_test.py` (3.0) e condiviso da rbac_test.py e netfail_test.py:
avvio/arresto del server (pid file) e pulizia delle CARTELLE ORFANE. Artefatto osservato su
Windows con Qdrant 1.19.2: `delete_collection` subito dopo scritture/ricerche risponde
500 "failed to rename ... to .deleted"; la collection sparisce dal registro ma la cartella resta
e RISORGE al riavvio, e una `create_collection` omonima risponde 400 "data already exists".
L'unica pulizia certa e' a server fermo: stop, rmtree della cartella, start.

CLI minima (istanza di laboratorio usa-e-getta, chiave MAI stampata a schermo):
    python labserver.py start  --exe <qdrant.exe> --port 6353 --grpc 6354 --storage <dir> [--api-key-env NAME]
    python labserver.py status --port 6353 --storage <dir>
    python labserver.py stop   --port 6353 --storage <dir>
Layout di <dir>: config.yaml, api-key.txt (la chiave: da NAME se definita, altrimenti riusata dal
file precedente, altrimenti casuale), qdrant.pid, qdrant.log, storage/, snapshots/, temp/.
`jwt_rbac: true`, host 127.0.0.1.

Le funzioni di modulo si attivano SOLO con il controllo esplicito del server
(`--qdrant-exe --config --pid-file` nei tool): senza, i tool riportano l'artefatto e non tentano nulla.
"""
from __future__ import annotations

import argparse
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx

SERVER: dict = {}


def configure(url: str, key: str, exe: str | None = None, config: str | None = None, pid_file: str | None = None) -> None:
    SERVER.update(url=url, key=key)
    if exe and config and pid_file:
        SERVER.update(exe=exe, config=config, pid_file=pid_file,
                      log=str(Path(config).parent / "qdrant-tools.log"))


def has_control() -> bool:
    return bool(SERVER.get("exe"))


def server_up(url: str | None = None) -> bool:
    try:
        return httpx.get((url or SERVER["url"]) + "/readyz", timeout=2).status_code == 200
    except Exception:  # noqa: BLE001
        return False


def storage_path() -> Path:
    cfg = Path(SERVER["config"])
    m = re.search(r"^\s*storage_path:\s*(.+?)\s*$", cfg.read_text(encoding="utf-8"), re.M)
    return Path(m.group(1).strip().strip("'\"")) if m else cfg.parent / "storage"


def server_stop() -> None:
    pid = int(Path(SERVER["pid_file"]).read_text().strip())
    try:
        os.kill(pid, signal.SIGTERM)          # su Windows = TerminateProcess
    except OSError as e:
        print(f"  (kill {pid}: {e})")
    for _ in range(40):
        if not server_up():
            break
        time.sleep(0.25)
    assert not server_up(), "il server non si e' fermato"
    print(f"  [server fermato, pid {pid}]")


def server_start() -> None:
    log = open(SERVER["log"], "ab")
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    p = subprocess.Popen([SERVER["exe"], "--config-path", SERVER["config"]], cwd=str(Path(SERVER["config"]).parent),
                         stdout=log, stderr=subprocess.STDOUT, creationflags=flags, stdin=subprocess.DEVNULL)
    Path(SERVER["pid_file"]).write_text(str(p.pid))
    for _ in range(120):
        if server_up():
            break
        if p.poll() is not None:
            break
        time.sleep(0.25)
    assert server_up(), f"il server non e' ripartito (log: {SERVER['log']})"
    time.sleep(2)   # Windows: subito dopo il riavvio i file delle collection sono ancora in apertura
    print(f"  [server riacceso, pid {p.pid}]")


def purge_orphans(names) -> list[str]:
    """Server giu', via le cartelle delle collection indicate (e `.deleted`), server su.
    Ritorna le cartelle effettivamente rimosse."""
    if not has_control():
        raise RuntimeError("pulizia delle cartelle orfane: servono --qdrant-exe/--config/--pid-file")
    storage = storage_path()
    if server_up():
        server_stop()
    removed = []
    for name in names:
        d = storage / "collections" / name
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
            removed.append(name)
    shutil.rmtree(storage / ".deleted", ignore_errors=True)
    server_start()
    print(f"  [cartelle orfane rimosse a server fermo: {removed or 'nessuna'}]")
    return removed


# ----------------------------------------------------------------------------
# CLI minima: start | stop | status
# ----------------------------------------------------------------------------
def _layout(storage: Path) -> dict:
    return {"config": storage / "config.yaml", "key": storage / "api-key.txt", "pid": storage / "qdrant.pid",
            "log": storage / "qdrant.log"}


def _write_config(storage: Path, port: int, grpc: int, key: str) -> Path:
    for d in ("storage", "snapshots", "temp"):
        (storage / d).mkdir(parents=True, exist_ok=True)
    cfg = _layout(storage)["config"]
    text = ("log_level: INFO\ntelemetry_disabled: true\nstorage:\n"
            f"  storage_path: {(storage / 'storage').as_posix()}\n"
            f"  snapshots_path: {(storage / 'snapshots').as_posix()}\n"
            f"  temp_path: {(storage / 'temp').as_posix()}\n"
            f"service:\n  host: 127.0.0.1\n  http_port: {port}\n  grpc_port: {grpc}\n  api_key: {key}\n  jwt_rbac: true\n")
    with open(cfg, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return cfg


def cli_start(a) -> int:
    storage = Path(a.storage).resolve()
    storage.mkdir(parents=True, exist_ok=True)
    lay = _layout(storage)
    key = os.environ.get(a.api_key_env, "").strip() if a.api_key_env else ""
    origin = f"variabile {a.api_key_env}" if key else ""
    if not key and lay["key"].is_file():
        key, origin = lay["key"].read_text(encoding="ascii").strip(), "file precedente"
    if not key:
        key, origin = secrets.token_hex(24), "generata casualmente"
    lay["key"].write_text(key, encoding="ascii")
    try:
        os.chmod(lay["key"], 0o600)
    except OSError:
        pass
    cfg = _write_config(storage, a.port, a.grpc, key)
    url = f"http://127.0.0.1:{a.port}"
    configure(url, key, a.exe, str(cfg), str(lay["pid"]))
    SERVER["log"] = str(lay["log"])
    if server_up(url):
        print(f"gia' in ascolto: {url} (pid file {lay['pid']}); chiave in {lay['key']} ({origin}, non stampata)")
        return 0
    if not Path(a.exe).is_file():
        print(f"ERRORE: eseguibile non trovato: {a.exe}")
        return 2
    server_start()
    print(f"URL: {url} (grpc {a.grpc})\nconfig: {cfg}\nchiave: {lay['key']} ({origin}, non stampata)\n"
          f"pid: {lay['pid'].read_text().strip()} | log: {lay['log']} | dati: {storage / 'storage'}")
    return 0


def cli_stop(a) -> int:
    storage = Path(a.storage).resolve()
    lay = _layout(storage)
    url = f"http://127.0.0.1:{a.port}"
    if not lay["pid"].is_file():
        print(f"nessun pid file in {storage}: {'il server risponde ma non e\' stato avviato da qui' if server_up(url) else 'server fermo'}")
        return 1 if server_up(url) else 0
    # per fermare bastano url e pid file (niente exe): si imposta SERVER direttamente
    SERVER.update(url=url, key="", pid_file=str(lay["pid"]), config=str(lay["config"]), log=str(lay["log"]))
    if not server_up(url):
        print(f"server gia' fermo ({url})")
        return 0
    server_stop()
    print(f"fermato: {url}")
    return 0


def cli_status(a) -> int:
    storage = Path(a.storage).resolve()
    lay = _layout(storage)
    url = f"http://127.0.0.1:{a.port}"
    up = server_up(url)
    pid = lay["pid"].read_text().strip() if lay["pid"].is_file() else "-"
    print(f"{url}: {'raggiungibile' if up else 'non raggiungibile'} | pid file: {pid} | chiave: "
          f"{lay['key'] if lay['key'].is_file() else 'assente'} (non stampata)")
    if up and lay["key"].is_file():
        try:
            r = httpx.get(url + "/collections", headers={"api-key": lay["key"].read_text(encoding="ascii").strip()}, timeout=5)
            if r.status_code == 200:
                names = sorted(c["name"] for c in r.json().get("result", {}).get("collections", []))
                print(f"collection: {', '.join(names) or '(nessuna)'}")
            else:
                print(f"collection: non leggibili (HTTP {r.status_code})")
        except Exception as e:  # noqa: BLE001
            print(f"collection: non leggibili ({e.__class__.__name__})")
    return 0 if up else 2


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Qdrant di LABORATORIO usa-e-getta (mai un cluster vero).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("start", "stop", "status"):
        s = sub.add_parser(name)
        s.add_argument("--port", type=int, required=True, help="porta HTTP")
        s.add_argument("--storage", required=True, help="cartella di lavoro: config, chiave, pid, log, dati")
        s.add_argument("--exe", default=None, help="qdrant.exe (obbligatorio per start)")
        if name == "start":
            s.add_argument("--grpc", type=int, required=True, help="porta gRPC")
            s.add_argument("--api-key-env", default=None,
                           help="NOME della variabile con la chiave da usare (mai il valore); assente = file precedente o casuale")
    a = ap.parse_args(argv)
    if a.cmd == "start":
        if not a.exe:
            ap.error("start richiede --exe")
        return cli_start(a)
    if a.cmd == "stop":
        return cli_stop(a)
    return cli_status(a)


if __name__ == "__main__":
    sys.exit(main())
