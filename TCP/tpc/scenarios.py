"""Cenários de demonstração. Cada função devolve um dict serializável (JSON)."""
import statistics
import time

from .coordinator import Coordinator
from .faults import FaultyParticipant
from .participants import MongoParticipant, PostgresParticipant
from .poller import Poller
from . import config


class Context:
    def __init__(self, log):
        self.log = log
        self.pg = PostgresParticipant(config.PG_DSN).ensure_schema()
        self.mongo = MongoParticipant(config.MONGO_URI, config.MONGO_DB).ensure_schema()

    def reset(self):
        self.pg.reset()
        self.mongo.reset()

    def state(self):
        return {"pg_linhas": self.pg.list(), "pg_prepared": self.pg.count_prepared(),
                "mongo_visiveis": self.mongo.list(), "mongo_pending": self.mongo.count_pending()}


def _events(log, txid, t0):
    return [{"t_ms": round((r["ts"] - t0) * 1000, 1), "evento": r["event"],
             **{k: v for k, v in r.items() if k not in ("ts", "tx", "event", "op", "participants")}}
            for r in log.read() if r["tx"] == txid]


def _run_insert(ctx, titulo, order_names, faulty_name, faulty_kwargs):
    """Insere o mesmo registro nos dois bancos, na ordem dada; um deles falha."""
    ctx.reset()
    real = {"postgres": ctx.pg, "mongodb": ctx.mongo}
    parts = {k: (FaultyParticipant(v, **faulty_kwargs) if k == faulty_name else v)
             for k, v in real.items()}
    coord = Coordinator(ctx.log, list(real.values()))
    op = {"kind": "insert", "id": "P-1", "name": "Notebook", "qty": 10, "version": 1}

    snaps = {"antes": ctx.state()}

    def listener(rec):  # foto do estado no instante da decisão (entre as fases)
        if rec["event"] == "DECISION":
            snaps["na_decisao"] = ctx.state()
    ctx.log.add_listener(listener)
    t0 = time.time()
    res = coord.execute(op, [parts[n] for n in order_names])
    ctx.log.remove_listener(listener)
    snaps["depois"] = ctx.state()
    return {"titulo": titulo, "ordem": order_names, "resultado": res.outcome,
            "erro": res.error, "estados": snaps, "eventos": _events(ctx.log, res.txid, t0)}


def cenario_0(ctx):
    """Controle: caminho feliz (sem falhas)."""
    return _run_insert(ctx, "Controle: inserção sem falhas (Postgres -> MongoDB)",
                       ["postgres", "mongodb"], None, {})


def cenario_1(ctx):
    """Postgres primeiro; MongoDB falha no prepare -> rollback do Postgres."""
    return _run_insert(ctx, "Cenário 1: Postgres -> MongoDB (falha no MongoDB)",
                       ["postgres", "mongodb"], "mongodb",
                       dict(fail_on=("prepare",), message="erro simulado ao gravar"))


def cenario_2(ctx):
    """MongoDB primeiro; Postgres falha (erro REAL: CHECK qty >= 0) -> rollback do MongoDB."""
    ctx.reset()
    coord = Coordinator(ctx.log, [ctx.pg, ctx.mongo])
    op = {"kind": "insert", "id": "P-2", "name": "Monitor", "qty": -5, "version": 1}
    snaps = {"antes": ctx.state()}

    def listener(rec):
        if rec["event"] == "DECISION":
            snaps["na_decisao"] = ctx.state()
    ctx.log.add_listener(listener)
    t0 = time.time()
    res = coord.execute(op, [ctx.mongo, ctx.pg])
    ctx.log.remove_listener(listener)
    snaps["depois"] = ctx.state()
    return {"titulo": "Cenário 2: MongoDB -> Postgres (falha no Postgres)",
            "ordem": ["mongodb", "postgres"], "resultado": res.outcome, "erro": res.error,
            "estados": snaps, "eventos": _events(ctx.log, res.txid, t0)}


def cenario_extra_recuperacao(ctx):
    """Falha na FASE 2 (commit do Postgres indisponível) -> IN_DOUBT -> recover()."""
    ctx.reset()
    flaky_pg = FaultyParticipant(ctx.pg, fail_on=("commit",), times=3, message="commit indisponível")
    coord = Coordinator(ctx.log, [flaky_pg, ctx.mongo], retry_delay=0.01)
    op = {"kind": "insert", "id": "P-3", "name": "Teclado", "qty": 7, "version": 1}
    t0 = time.time()
    res = coord.execute(op, [flaky_pg, ctx.mongo])
    meio = ctx.state()
    recuperadas = coord.recover()     # "reinício" do coordenador: lê o log e conclui
    return {"titulo": "Extra: falha na fase 2 e recuperação pelo log",
            "resultado_inicial": res.outcome, "estado_em_duvida": meio,
            "recuperadas": recuperadas, "estado_final": ctx.state(),
            "eventos": _events(ctx.log, res.txid, t0)}


# --------------------------------------------------------------------------- #
def cenario_3(ctx, updates=80, interval=0.010, phase_gap=0.020, pause=0.010,
              fail_mongo_at=12, fail_pg_at=25):
    """Atualizações 2PC concorrentes com um serviço lendo ambos os bancos a cada 10 ms."""
    ctx.reset()
    mongo_f = FaultyParticipant(ctx.mongo, fail_on=("prepare",),
                                when=lambda op: op["version"] == fail_mongo_at,
                                message="erro simulado ao gravar")
    coord = Coordinator(ctx.log, [ctx.pg, ctx.mongo], phase_gap=phase_gap)
    for i, nome in enumerate(("A", "B")):
        r = coord.execute({"kind": "insert", "id": nome, "name": f"item-{nome}",
                           "qty": 100, "version": 1}, [ctx.pg, ctx.mongo])
        assert r.outcome == "COMMITTED"

    poller = Poller(config.PG_DSN, config.MONGO_URI, config.MONGO_DB, interval)
    poller.start_and_wait()
    time.sleep(0.15)
    t_ini = time.time()
    txs = []
    for v in range(2, updates + 2):
        qty = -1 if v == fail_pg_at else v * 10        # -1 viola CHECK do Postgres
        op = {"kind": "update", "id": "A", "name": "item-A", "qty": qty, "version": v}
        # alterna a ordem: pares PG->Mongo, ímpares Mongo->PG
        order = [ctx.pg, mongo_f] if v % 2 == 0 else [mongo_f, ctx.pg]
        r = coord.execute(op, order)
        txs.append({"version": v, "qty": qty, "tx": r.txid, "ordem": [p.name for p in order],
                    "outcome": r.outcome, "erro": r.error})
        time.sleep(pause)
    t_fim = time.time()
    time.sleep(0.15)
    poller.stop()

    return _analyze(ctx, poller, txs, t_ini, t_fim, interval, phase_gap,
                    abortadas={fail_mongo_at, fail_pg_at})


def _analyze(ctx, poller, txs, t_ini, t_fim, interval, phase_gap, abortadas):
    S = poller.samples
    ts = [s[0] for s in S]
    gaps = [(b - a) * 1000 for a, b in zip(ts, ts[1:])]
    vA = lambda state: next((v for (i, q, v) in state if i == "A"), None)

    estaveis = [s for s in S if s[1] == s[3]]
    diverg = [s for s in estaveis if s[1] != s[2]]
    pg_ahead = sum(1 for s in diverg if (vA(s[1]) or 0) > (vA(s[2]) or 0))
    mongo_ahead = len(diverg) - pg_ahead

    # episódios de divergência = sequências de amostras divergentes consecutivas
    episodios, atual = [], []
    for s in S:
        if s in diverg:
            atual.append(s)
        elif atual:
            episodios.append(atual); atual = []
    if atual:
        episodios.append(atual)
    spans = [(e[-1][0] - e[0][0]) * 1000 for e in episodios]

    # leituras sujas: alguma amostra viu versão/valor de transação abortada?
    sujas = 0
    for s in S:
        for estado in (s[1], s[2], s[3]):
            for (i, q, v) in estado:
                if i == "A" and (v in abortadas or q < 0):
                    sujas += 1
    # janelas de commit extraídas do log
    log = [r for r in ctx.log.read() if r["tx"] in {t["tx"] for t in txs}]
    por_tx = {}
    for r in log:
        por_tx.setdefault(r["tx"], []).append(r)
    janela_commit, janela_prepared = [], []
    for tx, evs in por_tx.items():
        oks = [e["ts"] for e in evs if e["event"] == "COMMIT_OK"]
        dec = [e["ts"] for e in evs if e["event"] == "DECISION"]
        if len(oks) == 2 and dec:
            janela_commit.append((max(oks) - min(oks)) * 1000)
            janela_prepared.append((min(oks) - dec[0]) * 1000)

    commit_ok = [t for t in txs if t["outcome"] == "COMMITTED"]
    obs_pg = {vA(s[1]) for s in S} - {None}
    obs_mg = {vA(s[2]) for s in S} - {None}
    final_pg, final_mg = ctx.pg.list(), ctx.mongo.list()
    q = lambda xs, p: sorted(xs)[int(len(xs) * p) - 1] if xs else None
    return {
        "titulo": "Cenário 3: atualizações 2PC com leitura a cada 10 ms",
        "config": {"updates": len(txs), "intervalo_leitura_ms": interval * 1000,
                   "pausa_entre_fases_ms": phase_gap * 1000,
                   "versoes_abortadas": sorted(abortadas)},
        "duracao_updates_s": round(t_fim - t_ini, 2),
        "transacoes": {"total": len(txs), "commit": len(commit_ok),
                       "abortadas": [t for t in txs if t["outcome"] == "ABORTED"],
                       "em_duvida": [t for t in txs if t["outcome"] == "IN_DOUBT"]},
        "leitor": {"amostras": len(S), "intervalo_medio_ms": round(statistics.mean(gaps), 2),
                   "intervalo_p95_ms": round(q(gaps, .95), 2), "intervalo_max_ms": round(max(gaps), 2),
                   "duracao_media_leitura_ms": round(statistics.mean(s[4] for s in S) * 1000, 2),
                   "estouros_de_10ms": poller.overruns,
                   "amostras_estaveis": len(estaveis)},
        "leituras_sujas": sujas,
        "divergencia": {"amostras": len(diverg),
                        "pct_das_estaveis": round(100 * len(diverg) / max(1, len(estaveis)), 2),
                        "postgres_a_frente": pg_ahead, "mongodb_a_frente": mongo_ahead,
                        "episodios": len(episodios),
                        "maior_episodio_ms": round(max(spans), 1) if spans else 0},
        "janelas": {"commit_entre_bancos_ms_medio": round(statistics.mean(janela_commit), 2) if janela_commit else None,
                    "commit_entre_bancos_ms_max": round(max(janela_commit), 2) if janela_commit else None,
                    "prepared_ms_medio": round(statistics.mean(janela_prepared), 2) if janela_prepared else None},
        "versoes_observadas": {"postgres": len(obs_pg), "mongodb": len(obs_mg),
                               "commitadas": len(commit_ok) + 1},
        "estado_final": {"postgres": final_pg, "mongodb": final_mg,
                         "identicos": final_pg == final_mg,
                         "pg_prepared": ctx.pg.count_prepared(), "mongo_pending": ctx.mongo.count_pending()},
    }
