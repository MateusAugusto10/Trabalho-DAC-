"""Participantes (Resource Managers) do 2PC.

Contrato (commit/rollback são IDEMPOTENTES, essencial para a recuperação):
    prepare(txid, op)  -> deixa a mudança "pronta" e INVISÍVEL; lança exceção se não puder
    commit(txid)       -> torna a mudança visível/definitiva
    rollback(txid)     -> descarta a mudança

Postgres : 2PC nativo  (PREPARE TRANSACTION / COMMIT PREPARED / ROLLBACK PREPARED).
MongoDB  : o cliente não tem "prepared transaction" (transações multi-documento
           exigem replica set e não oferecem prepare/commit separados ao
           cliente). Usa-se o padrão de *staging*: a mudança é gravada no marcador
           `_pending` (inserts ficam ocultos; updates mantêm o valor antigo
           visível até o commit) e o commit é UMA
           atualização atômica de documento único que remove o marcador.
"""
import psycopg2
import psycopg2.errors
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

GID_PREFIX = "tpc-"


class ParticipantError(Exception):
    pass


def _row(r):
    return {"id": r[0], "name": r[1], "qty": r[2], "version": r[3]}


# --------------------------------------------------------------------------- #
class PostgresParticipant:
    name = "postgres"

    def __init__(self, dsn):
        self.dsn = dsn
        self._reader = None

    @staticmethod
    def _gid(txid):
        return GID_PREFIX + txid

    def ensure_schema(self):
        conn = psycopg2.connect(self.dsn)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                """CREATE TABLE IF NOT EXISTS items(
                       id      text PRIMARY KEY,
                       name    text NOT NULL,
                       qty     int  NOT NULL CHECK (qty >= 0),
                       version int  NOT NULL DEFAULT 1)""")
        conn.close()
        return self

    # ---- fase 1 ----
    def prepare(self, txid, op):
        conn = psycopg2.connect(self.dsn)
        try:
            conn.autocommit = False
            with conn.cursor() as cur:
                cur.execute("SET LOCAL lock_timeout = '1s'")
                if op["kind"] == "insert":
                    cur.execute(
                        "INSERT INTO items(id,name,qty,version) "
                        "VALUES (%(id)s,%(name)s,%(qty)s,%(version)s)", op)
                elif op["kind"] == "update":
                    cur.execute(
                        "UPDATE items SET name=%(name)s, qty=%(qty)s, version=%(version)s "
                        "WHERE id=%(id)s", op)
                    if cur.rowcount != 1:
                        raise ParticipantError(f"postgres: id={op['id']} não encontrado")
                else:
                    raise ParticipantError(f"operação desconhecida: {op['kind']}")
                cur.execute("PREPARE TRANSACTION %s", (self._gid(txid),))
        except ParticipantError:
            conn.rollback()
            raise
        except Exception as e:  # erro real do banco (constraint, lock, conexão...)
            try:
                conn.rollback()
            except Exception:
                pass
            raise ParticipantError(f"postgres: {str(e).strip().splitlines()[0]}") from e
        finally:
            conn.close()

    # ---- fase 2 ----
    def _finish(self, sql, txid):
        conn = psycopg2.connect(self.dsn)
        try:
            conn.autocommit = True  # COMMIT/ROLLBACK PREPARED não rodam dentro de bloco
            with conn.cursor() as cur:
                try:
                    cur.execute(sql, (self._gid(txid),))
                except psycopg2.errors.UndefinedObject:
                    pass  # gid inexistente => já finalizada (idempotência)
        finally:
            conn.close()

    def commit(self, txid):
        self._finish("COMMIT PREPARED %s", txid)

    def rollback(self, txid):
        self._finish("ROLLBACK PREPARED %s", txid)

    # ---- leitura / utilidades ----
    def list(self):
        if self._reader is None or self._reader.closed:
            self._reader = psycopg2.connect(self.dsn)
            self._reader.autocommit = True
        with self._reader.cursor() as cur:
            cur.execute("SELECT id,name,qty,version FROM items ORDER BY id")
            return [_row(r) for r in cur.fetchall()]

    def count_prepared(self):
        conn = psycopg2.connect(self.dsn)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM pg_prepared_xacts WHERE gid LIKE %s",
                        (GID_PREFIX + "%",))
            n = cur.fetchone()[0]
        conn.close()
        return n

    def reset(self):
        conn = psycopg2.connect(self.dsn)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT gid FROM pg_prepared_xacts WHERE gid LIKE %s", (GID_PREFIX + "%",))
            for (gid,) in cur.fetchall():
                cur.execute("ROLLBACK PREPARED %s", (gid,))
            cur.execute("TRUNCATE items")
        conn.close()

    def close(self):
        if self._reader is not None:
            self._reader.close()


# --------------------------------------------------------------------------- #
class MongoParticipant:
    name = "mongodb"

    def __init__(self, uri, db):
        self.client = MongoClient(uri, serverSelectionTimeoutMS=3000)
        self.col = self.client[db]["items"]

    def ensure_schema(self):
        self.client.admin.command("ping")
        return self

    # ---- fase 1 ----
    def prepare(self, txid, op):
        try:
            if op["kind"] == "insert":
                self.col.insert_one({
                    "_id": op["id"], "name": op["name"], "qty": op["qty"],
                    "version": op["version"],
                    "_pending": {"tx": txid, "op": "insert"},   # invisível p/ leitores
                })
            elif op["kind"] == "update":
                # "lock" otimista: só prepara se não há outra tx pendente no doc
                r = self.col.update_one(
                    {"_id": op["id"], "_pending": {"$exists": False}},
                    {"$set": {"_pending": {
                        "tx": txid, "op": "update",
                        "set": {"name": op["name"], "qty": op["qty"], "version": op["version"]},
                    }}})
                if r.matched_count != 1:
                    raise ParticipantError(
                        f"mongodb: id={op['id']} não encontrado ou bloqueado por outra tx")
            else:
                raise ParticipantError(f"operação desconhecida: {op['kind']}")
        except DuplicateKeyError as e:
            raise ParticipantError(f"mongodb: chave duplicada id={op['id']}") from e
        except ParticipantError:
            raise
        except Exception as e:
            raise ParticipantError(f"mongodb: {e}") from e

    # ---- fase 2 (idempotentes: a busca é feita pelo txid gravado no documento) ----
    def commit(self, txid):
        doc = self.col.find_one({"_pending.tx": txid})
        if doc is None:
            return  # já confirmado (ou nunca preparado)
        upd = {"$unset": {"_pending": ""}}
        if doc["_pending"]["op"] == "update":
            upd["$set"] = doc["_pending"]["set"]
        self.col.update_one({"_id": doc["_id"], "_pending.tx": txid}, upd)  # atômico (1 doc)

    def rollback(self, txid):
        doc = self.col.find_one({"_pending.tx": txid})
        if doc is None:
            return
        if doc["_pending"]["op"] == "insert":
            self.col.delete_one({"_id": doc["_id"], "_pending.tx": txid})
        else:
            self.col.update_one({"_id": doc["_id"], "_pending.tx": txid},
                                {"$unset": {"_pending": ""}})

    # ---- leitura / utilidades ----
    def list(self):
        # insert pendente => invisível; update pendente => continua visível o valor
        # antigo (o novo valor só existe dentro de `_pending.set` até o commit).
        cur = self.col.find({"_pending.op": {"$ne": "insert"}}).sort("_id", 1)
        return [{"id": d["_id"], "name": d["name"], "qty": d["qty"], "version": d["version"]}
                for d in cur]

    def count_pending(self):
        return self.col.count_documents({"_pending": {"$exists": True}})

    def reset(self):
        self.col.delete_many({})

    def close(self):
        self.client.close()
