"""Coordenador do Two-Phase Commit."""
import time
import uuid
from dataclasses import dataclass


@dataclass
class TxResult:
    txid: str
    outcome: str          # COMMITTED | ABORTED | IN_DOUBT
    error: str = ""


class Coordinator:
    def __init__(self, log, participants, commit_retries=3, retry_delay=0.05, phase_gap=0.0):
        self.log = log
        self.registry = {p.name: p for p in participants}   # usado na recuperação
        self.commit_retries = commit_retries
        self.retry_delay = retry_delay
        self.phase_gap = phase_gap  # pausa entre decisão e fase 2 (só p/ observar o estado "prepared")

    # ------------------------------------------------------------------ #
    def execute(self, op, participants, txid=None):
        """Executa `op` em todos os `participants` (na ordem dada) com 2PC."""
        txid = txid or uuid.uuid4().hex[:12]
        L = self.log.append
        L(txid, "BEGIN", participants=[p.name for p in participants], op=op)

        # ---------------- FASE 1: PREPARE (votação) ----------------
        error = None
        attempted = []
        for p in participants:
            attempted.append(p)
            try:
                p.prepare(txid, op)
                L(txid, "PREPARE_OK", participant=p.name)
            except Exception as e:
                error = str(e)
                L(txid, "PREPARE_FAIL", participant=p.name, error=error)
                break

        # ---------------- FASE 2: decisão + COMMIT/ROLLBACK ----------------
        if error:
            L(txid, "DECISION", decision="ABORT")           # decisão durável antes de agir
            if self._finish_all(txid, list(reversed(attempted)), "rollback"):
                L(txid, "END", outcome="ABORTED")
                return TxResult(txid, "ABORTED", error)
            return TxResult(txid, "IN_DOUBT", error)        # sem END -> recover()

        L(txid, "DECISION", decision="COMMIT")              # ponto sem retorno
        if self.phase_gap:
            time.sleep(self.phase_gap)
        if self._finish_all(txid, participants, "commit"):
            L(txid, "END", outcome="COMMITTED")
            return TxResult(txid, "COMMITTED")
        return TxResult(txid, "IN_DOUBT", "commit incompleto; use recover()")

    # ------------------------------------------------------------------ #
    def _finish_all(self, txid, participants, action):
        ok_all = True
        for p in participants:
            for attempt in range(1, self.commit_retries + 1):
                try:
                    getattr(p, action)(txid)
                    self.log.append(txid, f"{action.upper()}_OK", participant=p.name)
                    break
                except Exception as e:
                    self.log.append(txid, f"{action.upper()}_FAIL", participant=p.name,
                                    attempt=attempt, error=str(e))
                    time.sleep(self.retry_delay)
            else:
                ok_all = False
        return ok_all

    # ------------------------------------------------------------------ #
    def recover(self):
        """Conclui transações em dúvida lendo o log (reinício do coordenador)."""
        done = []
        for txid, info in self.log.in_doubt().items():
            parts = [self.registry[n] for n in info["participants"]]
            if info["decision"] == "COMMIT":
                action, outcome = "commit", "COMMITTED"
            else:  # sem decisão durável => abort presumido
                if info["decision"] is None:
                    self.log.append(txid, "DECISION", decision="ABORT", recovered=True)
                action, outcome = "rollback", "ABORTED"
            if self._finish_all(txid, parts, action):
                self.log.append(txid, "END", outcome=outcome, recovered=True)
                done.append((txid, outcome))
        return done
