"""Testes de integração (exigem Postgres e MongoDB no ar). Rode: pytest -v"""
import pytest

from tpc import config, scenarios
from tpc.txlog import TxLog


@pytest.fixture(scope="module")
def ctx(tmp_path_factory):
    return scenarios.Context(TxLog(str(tmp_path_factory.mktemp("log") / "tx.jsonl")))


def test_controle_commit_nos_dois(ctx):
    r = scenarios.cenario_0(ctx)
    assert r["resultado"] == "COMMITTED"
    assert len(r["estados"]["depois"]["pg_linhas"]) == 1
    assert len(r["estados"]["depois"]["mongo_visiveis"]) == 1
    # entre as fases: preparado, mas invisível
    assert r["estados"]["na_decisao"]["pg_prepared"] == 1
    assert r["estados"]["na_decisao"]["pg_linhas"] == []
    assert r["estados"]["na_decisao"]["mongo_visiveis"] == []


def test_cenario1_rollback_no_postgres(ctx):
    r = scenarios.cenario_1(ctx)
    assert r["resultado"] == "ABORTED"
    assert r["estados"]["na_decisao"]["pg_prepared"] == 1   # PG tinha votado sim
    d = r["estados"]["depois"]
    assert d["pg_linhas"] == [] and d["pg_prepared"] == 0
    assert d["mongo_visiveis"] == [] and d["mongo_pending"] == 0


def test_cenario2_rollback_no_mongo(ctx):
    r = scenarios.cenario_2(ctx)
    assert r["resultado"] == "ABORTED"
    assert "check constraint" in r["erro"]
    assert r["estados"]["na_decisao"]["mongo_pending"] == 1  # Mongo tinha votado sim
    d = r["estados"]["depois"]
    assert d["pg_linhas"] == [] and d["pg_prepared"] == 0
    assert d["mongo_visiveis"] == [] and d["mongo_pending"] == 0


def test_recuperacao_pelo_log(ctx):
    r = scenarios.cenario_extra_recuperacao(ctx)
    assert r["resultado_inicial"] == "IN_DOUBT"
    assert r["estado_final"]["pg_prepared"] == 0
    assert r["estado_final"]["pg_linhas"] == r["estado_final"]["mongo_visiveis"] != []


def test_cenario3_sem_leitura_suja_e_estado_final_igual(ctx):
    r = scenarios.cenario_3(ctx, updates=30, fail_mongo_at=7, fail_pg_at=15)
    assert r["leituras_sujas"] == 0
    assert len(r["transacoes"]["abortadas"]) == 2
    assert r["estado_final"]["identicos"]
    assert r["estado_final"]["pg_prepared"] == 0 and r["estado_final"]["mongo_pending"] == 0
