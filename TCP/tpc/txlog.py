"""Log durável do coordenador (write-ahead log do 2PC).

Cada evento é uma linha JSON com fsync. A decisão (COMMIT/ABORT) é gravada
ANTES de ser comunicada aos participantes: é isso que permite recuperar
transações "em dúvida" após uma falha do coordenador.
"""
import json
import os
import threading
import time


class TxLog:
    def __init__(self, path, fsync=True):
        self.path = path
        self.fsync = fsync
        self._lock = threading.Lock()
        self._listeners = []
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def add_listener(self, fn):
        self._listeners.append(fn)

    def remove_listener(self, fn):
        self._listeners.remove(fn)

    def append(self, tx, event, **detail):
        rec = {"ts": time.time(), "tx": tx, "event": event, **detail}
        with self._lock:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                f.flush()
                if self.fsync:
                    os.fsync(f.fileno())
        for fn in list(self._listeners):
            fn(rec)
        return rec

    def read(self):
        if not os.path.exists(self.path):
            return []
        with open(self.path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def in_doubt(self):
        """Transações com BEGIN mas sem END -> precisam ser concluídas."""
        txs = {}
        for r in self.read():
            t = txs.setdefault(r["tx"], {"participants": [], "decision": None, "ended": False})
            if r["event"] == "BEGIN":
                t["participants"] = r["participants"]
            elif r["event"] == "DECISION":
                t["decision"] = r["decision"]
            elif r["event"] == "END":
                t["ended"] = True
        return {k: v for k, v in txs.items() if not v["ended"]}
