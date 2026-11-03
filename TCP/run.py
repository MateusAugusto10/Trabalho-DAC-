#!/usr/bin/env python3
"""Executa os cenários e gera reports/resultados.json + RELATORIO.md.

Uso:  python run.py [all|s0|s1|s2|s3|extra] [--updates N]
"""
import argparse
import json
import os
import sys
import time

from tpc import config, scenarios
from tpc.report import build_report
from tpc.txlog import TxLog


def wait_dbs(retries=30):
    for i in range(retries):
        try:
            return scenarios.Context(TxLog(config.TX_LOG_PATH))
        except Exception as e:
            print(f"aguardando bancos... ({type(e).__name__})", file=sys.stderr)
            time.sleep(1)
    sys.exit("bancos indisponíveis; rode `docker compose up -d --wait`")


def ambiente(ctx):
    import psycopg2
    c = psycopg2.connect(config.PG_DSN); cur = c.cursor()
    cur.execute("SHOW server_version"); pg = cur.fetchone()[0]; c.close()
    info = ctx.mongo.client.server_info()
    mongo = info.get("version", "?") + (" (FerretDB)" if "ferretdbVersion" in info else "")
    return {"postgres": pg, "mongo": mongo}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("which", nargs="?", default="all", choices=["all", "s0", "s1", "s2", "s3", "extra"])
    ap.add_argument("--updates", type=int, default=80)
    a = ap.parse_args()

    if os.path.exists(config.TX_LOG_PATH):
        os.remove(config.TX_LOG_PATH)
    ctx = wait_dbs()
    plan = {"s0": scenarios.cenario_0, "s1": scenarios.cenario_1, "s2": scenarios.cenario_2,
            "extra": scenarios.cenario_extra_recuperacao,
            "s3": lambda c: scenarios.cenario_3(c, updates=a.updates)}
    keys = list(plan) if a.which == "all" else [a.which]
    results = {"ambiente": ambiente(ctx)}
    for k in keys:
        print(f"==> {k}")
        results[k] = plan[k](ctx)
        r = results[k]
        print("   resultado:", r.get("resultado") or r.get("resultado_inicial") or
              f"{r['transacoes']['commit']} commits / {len(r['transacoes']['abortadas'])} aborts / "
              f"{r['divergencia']['amostras']} amostras divergentes / {r['leituras_sujas']} leituras sujas")
    os.makedirs("reports", exist_ok=True)
    with open("reports/resultados.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    if a.which == "all":
        with open("RELATORIO.md", "w", encoding="utf-8") as f:
            f.write(build_report(results))
        print("RELATORIO.md gerado")


if __name__ == "__main__":
    main()
