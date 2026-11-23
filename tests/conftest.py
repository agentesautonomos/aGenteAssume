"""
Base dos testes da agenda e do assistente.

- Redis de mentira (fakeredis): nada encosta no Redis de verdade.
- Relogio congelado: os testes escolhem "agora" (em UTC, como no servidor do Railway),
  e o codigo ve a hora de Sao Paulo, como em producao.
- Uma clinica de exemplo com tres profissionais:
    Dra. Ana Souza   seg a sex 08:00-12:00 e 14:00-18:00, consulta de 30 min, Unimed Odonto e particular
    Dr. Fabio Lima   seg, ter e qui 14:00-18:00, consulta de 60 min, so particular (NAO atende quarta)
    Dr. Carlos Reis  sem agenda preenchida (o assistente so anota o pedido)

Rodar:  pip install -r requirements.txt -r requirements-dev.txt && pytest
"""
import os
import sys
from datetime import datetime, timezone

import fakeredis
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agenda as AG          # noqa: E402
import clinicas as CL        # noqa: E402

SLUG = "teste"

ANA = {"id": "ana1", "nome": "Dra. Ana Souza", "especialidades": "Clínico geral, clareamento",
       "convenios": "Unimed Odonto, particular",
       "agenda": {"dias": {"0": "8h-12h, 14h-18h", "1": "8h-12h, 14h-18h", "2": "8h-12h, 14h-18h",
                           "3": "8h-12h, 14h-18h", "4": "8h-12h, 14h-18h", "5": "", "6": ""}, "duracao": 30}}
FABIO = {"id": "fab1", "nome": "Dr. Fabio Lima", "especialidades": "Ortodontia", "convenios": "particular",
         "agenda": {"dias": {"0": "14:00-18:00", "1": "14:00-18:00", "2": "", "3": "14:00-18:00",
                             "4": "", "5": "", "6": ""}, "duracao": 60}}
CARLOS = {"id": "car1", "nome": "Dr. Carlos Reis", "especialidades": "Implante", "horarios": "sábado de manhã",
          "agenda": {"dias": {}, "duracao": 30}}

CLINICA = {"slug": SLUG, "nome": "Clínica Sorriso", "bot_ativo": True}


def conf_padrao() -> dict:
    import copy
    return {"endereco": "Rua das Flores, 100, São Paulo", "funcionamento": "Seg a sex 8h-18h",
            "convenios": "Unimed Odonto", "pagamento": "Pix e cartão",
            "profissionais": copy.deepcopy([ANA, FABIO, CARLOS])}


class Relogio:
    """Congela o datetime.now() dos modulos agenda e clinicas num instante UTC."""
    instante = datetime(2026, 11, 23, 13, 0, tzinfo=timezone.utc)   # segunda 23/11/2026 10:00 em SP

    def ajustar(self, utc: datetime):
        Relogio.instante = utc


class _DT(datetime):
    @classmethod
    def now(cls, tz=None):
        t = Relogio.instante
        return t.astimezone(tz) if tz else t.astimezone().replace(tzinfo=None)


@pytest.fixture(autouse=True)
def ambiente(monkeypatch):
    r = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(AG, "R", r)
    monkeypatch.setattr(CL, "R", r)
    monkeypatch.setattr(AG, "datetime", _DT)
    monkeypatch.setattr(CL, "datetime", _DT)
    Relogio.instante = datetime(2026, 11, 23, 13, 0, tzinfo=timezone.utc)
    yield r


@pytest.fixture
def relogio():
    return Relogio()


@pytest.fixture
def conf():
    c = conf_padrao()
    CL.salvar_conf(SLUG, c)
    return CL.obter_conf(SLUG)


@pytest.fixture
def ferramenta(conf):
    """ferramenta(nome, **entrada) -> texto que o assistente recebe (como na conversa de verdade)."""
    reservas = []

    def chamar(nome, tel="5511999990000", sim=False, **ent):
        ctx = {"slug": SLUG, "conf": conf, "tel": tel, "sim": sim, "reservas": reservas}
        return CL.executar_ferramenta(nome, ent, ctx)
    chamar.reservas = reservas
    return chamar
