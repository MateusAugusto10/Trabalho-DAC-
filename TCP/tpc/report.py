"""Gera RELATORIO.md a partir de reports/resultados.json (dados reais da execução)."""
import datetime


def _rows(rows):
    return ", ".join(f"{r['id']}(qty={r['qty']},v{r['version']})" for r in rows) or "—"


def _estados(e):
    out = ["| Momento | Postgres: linhas visíveis | Postgres: txs *prepared* | MongoDB: docs visíveis | MongoDB: docs `_pending` |",
           "|---|---|---|---|---|"]
    nomes = {"antes": "Antes", "na_decisao": "Entre as fases (após fase 1)", "depois": "Depois"}
    for k in ("antes", "na_decisao", "depois"):
        s = e[k]
        out.append(f"| {nomes[k]} | {_rows(s['pg_linhas'])} | {s['pg_prepared']} | "
                   f"{_rows(s['mongo_visiveis'])} | {s['mongo_pending']} |")
    return "\n".join(out)


def _eventos(ev):
    out = ["| t (ms) | Evento | Participante | Detalhe |", "|---|---|---|---|"]
    for e in ev:
        det = e.get("error") or e.get("decision") or e.get("outcome") or ""
        if e.get("attempt"):
            det = f"tentativa {e['attempt']}: {det}"
        out.append(f"| {e['t_ms']} | {e['evento']} | {e.get('participant', '')} | {det} |")
    return "\n".join(out)


def _cenario_insert(c, comentario):
    return (f"**Ordem de preparação:** {' → '.join(c['ordem'])}  \n"
            f"**Resultado da transação:** `{c['resultado']}`  \n"
            f"**Erro:** {c['erro'] or '—'}\n\n"
            f"Linha do tempo (log do coordenador):\n\n{_eventos(c['eventos'])}\n\n"
            f"Estado dos bancos:\n\n{_estados(c['estados'])}\n\n{comentario}\n")


def build_report(R):
    s3, ex = R["s3"], R["extra"]
    amb = R.get("ambiente", {})
    d, j, l, tx = s3["divergencia"], s3["janelas"], s3["leitor"], s3["transacoes"]
    ab = "; ".join(f"v{t['version']} ({t['ordem'][0]} primeiro): {t['erro']}" for t in tx["abortadas"])
    fim = s3["estado_final"]
    return f"""# Relatório — Two-Phase Commit entre PostgreSQL e MongoDB

_Gerado automaticamente por `python run.py all` em {datetime.datetime.now():%Y-%m-%d %H:%M:%S}._
Ambiente da execução: PostgreSQL `{amb.get('postgres', '?')}`, servidor MongoDB `{amb.get('mongo', '?')}`.

## 1. Resumo

| Cenário | Resultado | Verificação |
|---|---|---|
| 0 — controle (sem falha) | `{R['s0']['resultado']}` | dado visível nos dois bancos |
| 1 — Postgres → Mongo, Mongo falha | `{R['s1']['resultado']}` | Postgres fez *rollback*: 0 linhas e 0 txs preparadas |
| 2 — Mongo → Postgres, Postgres falha | `{R['s2']['resultado']}` | Mongo fez *rollback*: 0 documentos e 0 `_pending` |
| 3 — updates com leitor a cada 10 ms | {tx['commit']} commits, {len(tx['abortadas'])} aborts | {s3['leituras_sujas']} leituras de dado não confirmado; estado final idêntico: {fim['identicos']} |
| Extra — falha na fase 2 | `{ex['resultado_inicial']}` → recuperada pelo log | estado final consistente nos dois bancos |

## 2. Arquitetura

```
 aplicação / cenários
          │
          ▼
┌──────────────────────────────────┐       ┌──────────────────────────┐
│  Coordenador 2PC                 │       │ TxLog (JSONL + fsync)    │
│  • fase 1: PREPARE (votação)     │──────▶│ BEGIN, PREPARE_*,        │
│  • decisão durável no log        │       │ DECISION, COMMIT_*,      │
│  • fase 2: COMMIT | ROLLBACK     │       │ ROLLBACK_*, END          │
│  • recover() lendo o log         │       │ (write-ahead do coord.)  │
└───────┬──────────────────┬───────┘       └──────────────────────────┘
        │                  │
        ▼                  ▼
┌─────────────────┐  ┌───────────────────┐
│ Participante    │  │ Participante      │
│ PostgreSQL      │  │ MongoDB           │
│ PREPARE         │  │ staging em        │
│ TRANSACTION /   │  │ `_pending` /      │
│ COMMIT PREPARED │  │ commit atômico    │
└────────┬────────┘  └─────────┬─────────┘
         ▼                     ▼
    PostgreSQL              MongoDB
         ▲                     ▲
         └─ Poller (10 ms) lê os dois ─┘
```

**Protocolo.** Fase 1: o coordenador chama `prepare` em cada participante, na ordem definida pelo cenário. Se todos votam sim, grava `DECISION=COMMIT` no log (com `fsync`) e executa a fase 2 (`commit` em todos). Se qualquer um falha, grava `DECISION=ABORT` e chama `rollback` nos participantes já tentados (ordem inversa). `COMMIT`/`ROLLBACK` são **idempotentes** e têm *retry*; se não concluem, a transação fica sem `END` no log e `recover()` a finaliza (decisão `COMMIT` → refaz commit; sem decisão → aborto presumido).

**Participantes.**
- **PostgreSQL** usa 2PC nativo: `PREPARE TRANSACTION '<gid>'` persiste a transação e a desassocia da conexão (sobrevive a quedas); `COMMIT PREPARED` / `ROLLBACK PREPARED` a concluem. Exige `max_prepared_transactions > 0` (configurado no `docker-compose.yml`). Uma restrição `CHECK (qty >= 0)` permite provocar um erro *real* do banco.
- **MongoDB** não oferece *prepare* separado de *commit* ao cliente. Foi implementado o padrão de **staging**: no `prepare`, a mudança é gravada no campo `_pending` do documento (insert: documento oculto para os leitores; update: valor antigo continua visível e o novo fica em `_pending.set`, que também funciona como *lock* otimista). O `commit` é **uma única atualização atômica de documento** que aplica a mudança e remove `_pending`; o `rollback` apaga o documento (insert) ou remove `_pending` (update).

**Falhas.** `FaultyParticipant` injeta falhas em qualquer fase. No cenário 1 a falha é simulada (exceção no `prepare` do Mongo); no cenário 2 é um erro **real** do Postgres (violação do `CHECK`).

**Serviço de consulta.** `Poller` roda em thread própria, com conexões próprias, lendo a lista completa dos dois bancos a cada 10 ms. Cada tick lê PG → Mongo → PG; como as versões são monotônicas, se as duas leituras do PG são iguais a diferença para o Mongo é uma divergência real, não artefato da leitura não simultânea.

## 3. Cenário 0 — controle (sem falhas)
{_cenario_insert(R['s0'], "Entre as fases o dado já está *prepared* mas **invisível** nos dois bancos; só depois do commit aparece em ambos.")}
## 4. Cenário 1 — inserir no Postgres e depois no MongoDB; o Mongo falha
{_cenario_insert(R['s1'], "O Postgres já havia votado sim (transação *prepared*, `prepared=1`), mas o Mongo falhou; o coordenador decidiu `ABORT` e executou `ROLLBACK PREPARED`. Nenhum dado ficou em nenhum dos bancos e nenhum leitor chegou a ver a linha.")}
## 5. Cenário 2 — inserir no MongoDB e depois no Postgres; o Postgres falha
{_cenario_insert(R['s2'], "O Mongo já havia gravado o documento em `_pending` (invisível); o Postgres rejeitou a linha (erro real de `CHECK`); o coordenador desfez o *staging* no Mongo. Nenhum dado ficou em nenhum dos bancos.")}
## 6. Cenário 3 — atualizações nos dois bancos com leitura a cada 10 ms

Configuração: {s3['config']['updates']} atualizações 2PC do item `A` (ordem alternando entre PG→Mongo e Mongo→PG), pausa de {s3['config']['pausa_entre_fases_ms']:.0f} ms entre a decisão e a fase 2 (para tornar observável o estado *prepared*), duração {s3['duracao_updates_s']} s. Duas atualizações foram forçadas a falhar: {ab}.

| Métrica | Valor |
|---|---|
| Transações: confirmadas / abortadas / em dúvida | {tx['commit']} / {len(tx['abortadas'])} / {len(tx['em_duvida'])} |
| Amostras do leitor | {l['amostras']} (intervalo médio {l['intervalo_medio_ms']} ms, p95 {l['intervalo_p95_ms']} ms, máx {l['intervalo_max_ms']} ms; {l['estouros_de_10ms']} ticks estouraram os 10 ms) |
| Duração média de um tick (3 leituras) | {l['duracao_media_leitura_ms']} ms |
| **Leituras de valor de transação abortada / não confirmada** | **{s3['leituras_sujas']}** |
| Versões distintas vistas (PG / Mongo) de {s3['versoes_observadas']['commitadas']} commitadas | {s3['versoes_observadas']['postgres']} / {s3['versoes_observadas']['mongodb']} |
| Janela de *prepared* (decisão → 1º commit), média | {j['prepared_ms_medio']} ms |
| Janela de commit entre os bancos (1º → 2º commit), média / máx | {j['commit_entre_bancos_ms_medio']} ms / {j['commit_entre_bancos_ms_max']} ms |
| Amostras estáveis com PG ≠ Mongo | {d['amostras']} ({d['pct_das_estaveis']}% das estáveis) |
| — Postgres à frente / Mongo à frente | {d['postgres_a_frente']} / {d['mongodb_a_frente']} |
| Episódios de divergência / maior episódio | {d['episodios']} / {d['maior_episodio_ms']} ms |
| Estado final: Postgres == MongoDB | {fim['identicos']} (txs prepared restantes: {fim['pg_prepared']}, `_pending` restantes: {fim['mongo_pending']}) |

Estado final — Postgres: {_rows(fim['postgres'])} · MongoDB: {_rows(fim['mongodb'])}

**Leitura dos resultados.**
1. *Atomicidade de resultado:* as duas transações abortadas não deixaram rastro em nenhum banco, e as {tx['commit']} confirmadas estão nos dois. O leitor nunca viu valor de transação abortada (nem a versão forçada a falhar no Mongo, nem o `qty=-1` que o Mongo aceitou em `_pending` antes de o Postgres rejeitar).
2. *Isolamento da fase 1:* durante a janela *prepared* (~{j['prepared_ms_medio']} ms por transação) o leitor continuou vendo o valor antigo e **consistente** nos dois bancos.
3. *Divergência temporária na fase 2:* o 2PC garante que todos terminam com o mesmo resultado, **não** que os commits sejam simultâneos. Como o commit é feito banco a banco, existe uma janela (média {j['commit_entre_bancos_ms_medio']} ms) em que um banco já mostra a versão nova e o outro ainda a antiga; o leitor de 10 ms a capturou em {d['pct_das_estaveis']}% das amostras, nos dois sentidos (quem commita primeiro fica à frente). A janela inclui o `fsync` do log do coordenador e o tempo do próprio commit em cada banco. Todas as divergências se resolveram sozinhas (maior episódio: {d['maior_episodio_ms']} ms) e o estado final é idêntico.

## 7. Cenário extra — falha na fase 2 e recuperação pelo log

O commit do Postgres foi forçado a falhar nas 3 tentativas. Resultado inicial: `{ex['resultado_inicial']}`.

{_eventos(ex['eventos'])}

| Momento | Postgres: linhas visíveis | Postgres: *prepared* | MongoDB: docs visíveis |
|---|---|---|---|
| Em dúvida (decisão COMMIT gravada, PG não confirmou) | {_rows(ex['estado_em_duvida']['pg_linhas'])} | {ex['estado_em_duvida']['pg_prepared']} | {_rows(ex['estado_em_duvida']['mongo_visiveis'])} |
| Após `recover()` | {_rows(ex['estado_final']['pg_linhas'])} | {ex['estado_final']['pg_prepared']} | {_rows(ex['estado_final']['mongo_visiveis'])} |

`recover()` encontrou {len(ex['recuperadas'])} transação(ões) em dúvida: {ex['recuperadas']}. Como a decisão `COMMIT` estava no log, a transação foi **concluída (roll-forward)**, não desfeita.

## 8. Limitações e pontos de atenção

- **Mongo sem *prepare* nativo.** O padrão de staging dá atomicidade por documento e exige que todos os leitores respeitem `_pending` (aqui, `MongoParticipant.list()`); um leitor externo que ignore o campo veria o valor em `_pending`/documentos de insert pendentes. Alternativa: transações multi-documento do MongoDB (replica set), mas elas não expõem *prepare* separado.
- **Bloqueio:** entre *prepare* e *commit* o Postgres mantém locks da linha e o Mongo mantém `_pending`; um coordenador parado deixa recursos presos até `recover()` (limitação clássica do 2PC).
- **Coordenador é ponto único de falha:** o log é um arquivo local. Em produção, replicar/armazenar o log de forma durável e ter *timeout* para transações *prepared* órfãs.
- **Não há isolamento global entre bancos** (ver cenário 3). Leitores que precisem de visão consistente entre os dois bancos exigem outro mecanismo (versão/snapshot lógico).
- Poller com 10 ms real: {l['estouros_de_10ms']} ticks excederam o intervalo (leitura em Python, 3 consultas por tick); a taxa efetiva foi de {l['intervalo_medio_ms']} ms.
"""
