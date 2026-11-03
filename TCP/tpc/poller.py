"""Serviço de consulta: lê a lista completa dos dois bancos a cada 10 ms.

Cada tick faz  PG -> Mongo -> PG.  Como as versões são monotônicas, se as duas
leituras do PG são iguais o PG não mudou durante o tick; assim uma diferença
PG != Mongo nesse tick é uma divergência REAL (e não um artefato de a leitura
dos dois bancos não ser simultânea).
"""
import threading
import time

from .participants import MongoParticipant, PostgresParticipant


def _compact(rows):
    return tuple((r["id"], r["qty"], r["version"]) for r in rows)


class Poller(threading.Thread):
    def __init__(self, pg_dsn, mongo_uri, mongo_db, interval=0.010):
        super().__init__(daemon=True)
        self.args = (pg_dsn, mongo_uri, mongo_db)
        self.interval = interval
        self.samples = []          # (t, pg_antes, mongo, pg_depois, duração_leitura)
        self.overruns = 0
        self._halt = threading.Event()
        self._ready = threading.Event()

    def run(self):
        pg = PostgresParticipant(self.args[0])
        mg = MongoParticipant(self.args[1], self.args[2])
        pg.list(); mg.list()       # aquece conexões
        self._ready.set()
        nxt = time.perf_counter()
        while not self._halt.is_set():
            t = time.time()
            t0 = time.perf_counter()
            a = _compact(pg.list())
            m = _compact(mg.list())
            b = _compact(pg.list())
            dur = time.perf_counter() - t0
            self.samples.append((t, a, m, b, dur))
            nxt += self.interval
            wait = nxt - time.perf_counter()
            if wait > 0:
                time.sleep(wait)
            else:                   # leitura demorou mais que 10 ms
                self.overruns += 1
                nxt = time.perf_counter()
        pg.close(); mg.close()

    def start_and_wait(self):
        self.start()
        self._ready.wait(10)

    def stop(self):
        self._halt.set()
        self.join()
