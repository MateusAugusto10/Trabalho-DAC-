"""Injeção de falhas: envolve um participante e faz uma fase falhar."""


class InjectedFailure(Exception):
    pass


class FaultyParticipant:
    def __init__(self, inner, fail_on=("prepare",), times=None, when=None,
                 message="falha injetada"):
        self.inner, self.fail_on, self.times, self.when, self.message = \
            inner, set(fail_on), times, when, message

    def __getattr__(self, item):  # delega name, list(), reset()...
        return getattr(self.inner, item)

    def _maybe(self, phase, op=None):
        if phase not in self.fail_on:
            return
        if self.when is not None and op is not None and not self.when(op):
            return
        if self.times is not None:
            if self.times <= 0:
                return
            self.times -= 1
        raise InjectedFailure(f"{self.inner.name}: {self.message} ({phase})")

    def prepare(self, txid, op):
        self._maybe("prepare", op)
        return self.inner.prepare(txid, op)

    def commit(self, txid):
        self._maybe("commit")
        return self.inner.commit(txid)

    def rollback(self, txid):
        self._maybe("rollback")
        return self.inner.rollback(txid)
