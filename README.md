# Two-Phase Commit (2PC) entre PostgreSQL e MongoDB

Este projeto mostra, na prática, como gravar dados em **dois bancos diferentes** (PostgreSQL e MongoDB) de forma que **ou os dois guardam o dado, ou nenhum guarda**, usando o protocolo **Two-Phase Commit (2PC)**.

Ele roda 3 cenários e gera um relatório com o que aconteceu em cada um:

1. **Cenário 1:** grava no Postgres e depois no MongoDB. O MongoDB falha, então o Postgres desfaz (*rollback*).
2. **Cenário 2:** grava no MongoDB e depois no Postgres. O Postgres falha, então o MongoDB desfaz.
3. **Cenário 3:** atualiza os dois bancos enquanto um serviço lê os dois a cada 10 ms.

Há ainda um cenário **extra** (falha na 2ª fase e recuperação pelo log) e um **controle** sem falhas.

> Não precisa entender o 2PC para rodar. Siga os passos abaixo na ordem. Tempo estimado: 10 a 15 minutos.

---

## Passo 1: Instalar o que é necessário

Você precisa de duas ferramentas.

### 1.1 Docker (roda os bancos de dados para você, sem instalar Postgres nem MongoDB)
- **Windows / Mac:** instale o **Docker Desktop**: https://www.docker.com/products/docker-desktop/ e **abra o programa** (ele precisa estar rodando; espere o ícone da baleia ficar estável).
- **Linux (Ubuntu):** https://docs.docker.com/engine/install/ubuntu/

### 1.2 Python 3.10 ou mais novo
- Baixe em https://www.python.org/downloads/
- **Windows:** durante a instalação, marque a caixa **"Add Python to PATH"**.

### 1.3 Conferir se deu certo
Abra um terminal (Windows: **PowerShell**; Mac/Linux: **Terminal**) e rode:

```bash
docker --version
docker compose version
python --version      # no Mac/Linux, se não funcionar, use: python3 --version
```

Cada comando deve mostrar um número de versão. Se aparecer "comando não encontrado", refaça a instalação daquele item.

---

## Passo 2: Obter o projeto

Se recebeu o arquivo `tpc-demo.zip`, extraia-o (botão direito → *Extrair tudo*) e entre na pasta pelo terminal:

```bash
cd caminho/para/tpc-demo
```

Dica: no Windows, abra a pasta no Explorador, clique na barra de endereço, digite `powershell` e tecle Enter. O terminal abre já dentro da pasta.

Confira que está no lugar certo: `ls` (Mac/Linux) ou `dir` (Windows) deve listar `docker-compose.yml`, `run.py` e `requirements.txt`.

---

## Passo 3: Subir os bancos de dados

```bash
docker compose up -d --wait
```

Na primeira vez ele baixa as imagens (1 a 3 minutos). Quando terminar, confira:

```bash
docker compose ps
```

Os dois serviços (`postgres` e `mongo`) devem aparecer como **healthy** (saudável).

> O Postgres já vem configurado com `max_prepared_transactions=20`, que é o que habilita o 2PC nele.

---

## Passo 4: Preparar o Python

Ainda na pasta do projeto, crie um ambiente virtual e instale as dependências.

**Mac / Linux:**
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

**Windows (PowerShell):**
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```
Se o Windows bloquear a ativação, rode antes: `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`

Quando o ambiente estiver ativo, o terminal mostra `(.venv)` no começo da linha.

---

## Passo 5: Rodar os cenários

```bash
python run.py all
```

Em poucos segundos você deve ver algo parecido com:

```
==> s0
   resultado: COMMITTED
==> s1
   resultado: ABORTED
==> s2
   resultado: ABORTED
==> extra
   resultado: IN_DOUBT
==> s3
   resultado: 78 commits / 2 aborts / 93 amostras divergentes / 0 leituras sujas
RELATORIO.md gerado
```

**Como interpretar:**

| Linha | O que significa |
|---|---|
| `s0 COMMITTED` | Sem falhas, o dado foi gravado nos dois bancos |
| `s1 ABORTED` | O MongoDB falhou e o Postgres foi desfeito (Cenário 1) |
| `s2 ABORTED` | O Postgres falhou e o MongoDB foi desfeito (Cenário 2) |
| `extra IN_DOUBT` | Falha na 2ª fase; depois foi recuperada pelo log (detalhes no relatório) |
| `s3 ... 0 leituras sujas` | Nenhuma leitura viu dado de transação cancelada |

Para rodar só um cenário: `python run.py s1`, `s2`, `s3` ou `extra`. No cenário 3 dá para aumentar a carga: `python run.py s3 --updates 150`.

---

## Passo 6: Ver o relatório

Abra o arquivo **`RELATORIO.md`** (criado na pasta do projeto) no VS Code, no Bloco de Notas ou em qualquer visualizador de Markdown. Ele contém:

- a arquitetura utilizada;
- a linha do tempo de cada transação (`BEGIN → PREPARE → DECISION → COMMIT/ROLLBACK → END`);
- o estado dos dois bancos antes, entre as fases e depois;
- as métricas do cenário 3.

Outros arquivos gerados na pasta `reports/`:
- `resultados.json`: dados brutos;
- `coordinator.log.jsonl`: log do coordenador, linha por linha.

---

## Passo 7 (opcional): Conferir você mesmo que o rollback funcionou

Depois de rodar `python run.py s1` ou `python run.py s2`, os bancos devem estar **vazios** e sem transações pendentes:

```bash
# Linhas na tabela do Postgres (esperado: 0 rows)
docker compose exec postgres psql -U postgres -d tpc -c "SELECT * FROM items;"

# Transações preparadas e esquecidas no Postgres (esperado: 0 rows)
docker compose exec postgres psql -U postgres -d tpc -c "SELECT gid FROM pg_prepared_xacts;"

# Documentos no MongoDB (esperado: nada)
docker compose exec mongo mongosh tpc --quiet --eval "db.items.find()"
```

---

## Passo 8 (opcional): Rodar os testes automáticos

```bash
python -m pytest -v
```

Esperado: `5 passed`.

---

## Passo 9: Encerrar

```bash
docker compose down -v
```

Isso desliga e apaga os bancos. Para sair do ambiente Python: `deactivate`.

---

## Problemas comuns

| O que aparece | Causa provável | Como resolver |
|---|---|---|
| `docker: command not found` / `Cannot connect to the Docker daemon` | Docker não instalado ou não aberto | Abra o Docker Desktop, espere iniciar e repita o passo 3 |
| `port is already allocated` (5432 ou 27017) | Já existe um Postgres/MongoDB rodando na máquina | Pare o serviço local, ou mude a porta no `docker-compose.yml` (ex.: `"5433:5432"`) e defina `PG_DSN` (ver abaixo) |
| `bancos indisponíveis` | Os containers ainda não estão prontos | Rode `docker compose ps`; espere ficarem `healthy` e repita o passo 5 |
| `prepared transactions are disabled` | Está usando outro Postgres, sem 2PC habilitado | Use o Postgres do `docker-compose.yml` |
| `python: command not found` | Python fora do PATH | No Mac/Linux use `python3`; no Windows reinstale marcando "Add Python to PATH" |
| `No module named psycopg2` / `pymongo` | Ambiente virtual não ativado | Repita o passo 4 e veja se aparece `(.venv)` |

### Usando bancos próprios (avançado)
Defina estas variáveis antes de rodar. O Postgres precisa ter `max_prepared_transactions` maior que 0.

```bash
export PG_DSN="postgresql://usuario:senha@host:5432/banco"
export MONGO_URI="mongodb://host:27017"
```
(No PowerShell: `$env:PG_DSN="..."`.)

---

## Como o projeto funciona (resumo)

```
Cenários ─► Coordenador 2PC ─┬─► Participante Postgres (PREPARE TRANSACTION / COMMIT PREPARED)
                             ├─► Participante MongoDB  (grava em `_pending` e confirma de forma atômica)
                             └─► Log durável (tpc/txlog.py), usado para recuperar falhas
```

1. **Fase 1 (preparar):** cada banco recebe o dado e diz "consigo gravar" ou "não consigo". O dado fica guardado, mas **invisível**.
2. **Fase 2 (decidir):** se **todos** disseram sim, o coordenador manda **confirmar**; se **algum** disse não, manda **desfazer** em todos.

| Arquivo | Papel |
|---|---|
| `run.py` | Ponto de entrada: roda os cenários e gera o relatório |
| `tpc/coordinator.py` | Coordenador do 2PC e recuperação pelo log |
| `tpc/participants.py` | Adaptadores do Postgres e do MongoDB |
| `tpc/faults.py` | Injeção de falhas para os testes |
| `tpc/poller.py` | Serviço que lê os dois bancos a cada 10 ms |
| `tpc/scenarios.py` | Definição dos cenários |
| `tpc/txlog.py` | Log do coordenador |
| `tests/test_2pc.py` | Testes automáticos |
