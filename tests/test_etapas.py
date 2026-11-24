"""Etapas da conversa (o sistema decide o proximo dado que falta) e o conferente que revisa a resposta."""
import asyncio
import json
from types import SimpleNamespace

import clinicas as CL
from conftest import SLUG


def est(**kw):
    base = {"motivo": None, "profissional": None, "quando": None, "horario": None, "nome": None, "convenio": None,
            "perguntas": [], "fora_do_agendamento": False, "ja_marcado": False}
    base.update(kw)
    return base


def test_primeiro_pede_o_motivo(conf):
    e = CL.proxima_etapa(est(), conf)
    assert "é para quê" in e and "única pergunta" in e


def test_com_motivo_pede_o_profissional(conf):
    e = CL.proxima_etapa(est(motivo="dor de dente"), conf)
    assert "preferência" in e and "dor de dente" in e


def test_profissional_escolhido_pula_o_motivo_e_pede_o_dia(conf):
    e = CL.proxima_etapa(est(profissional="Dra. Ana"), conf)
    assert "dia e período" in e


def test_com_dia_e_sem_profissional_oferece_horarios_e_pergunta_qual_prefere(conf):
    e = CL.proxima_etapa(est(motivo="limpeza", quando="amanhã à tarde"), conf)
    assert "Qual prefere" in e


def test_ordem_horario_nome_convenio_e_marcar(conf):
    b = dict(motivo="limpeza", profissional="Dra. Ana", quando="amanhã")
    assert "até 3 opções" in CL.proxima_etapa(est(**b), conf)
    assert "nome" in CL.proxima_etapa(est(**b, horario="14h"), conf)
    assert "particular ou por convênio" in CL.proxima_etapa(est(**b, horario="14h", nome="João"), conf)
    assert "marcar_consulta agora" in CL.proxima_etapa(est(**b, horario="14h", nome="João", convenio="particular"), conf)


def test_pergunta_do_paciente_vem_primeiro(conf):
    e = CL.proxima_etapa(est(motivo="limpeza", perguntas=["atende Amil?"]), conf)
    assert e.index("atende Amil?") < e.index("preferência")


def test_fora_do_agendamento_so_responde(conf):
    assert CL.proxima_etapa(est(fora_do_agendamento=True), conf) == ""
    e = CL.proxima_etapa(est(fora_do_agendamento=True, perguntas=["qual o endereço?"]), conf)
    assert "endereço" in e and "única pergunta" not in e


def test_um_profissional_so_nao_pergunta_motivo_nem_profissional(conf):
    conf["profissionais"] = conf["profissionais"][:1]
    assert "dia e período" in CL.proxima_etapa(est(), conf)


def test_json_de_tolera_texto_em_volta():
    assert CL._json_de('Aqui: {"a": 1} fim') == {"a": 1}
    assert CL._json_de("nada") == {}


class Falso:
    """Cliente falso: devolve respostas prontas e guarda os pedidos."""
    def __init__(self, principal, conferente):
        self.principal, self.conferente, self.pedidos = list(principal), conferente, []
        self.messages = self

    def create(self, **kw):
        self.pedidos.append(kw)
        if kw["model"] == CL.MODELO_CONFERENTE and "system" not in kw:
            txt = kw["messages"][0]["content"]
            out = json.dumps(est(motivo="limpeza", perguntas=["atende Amil?"])) if txt.startswith("[ETAPAS]") \
                else json.dumps({"problemas": self.conferente.pop(0) if self.conferente else []})
        else:
            out = self.principal.pop(0)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=out)], stop_reason="end_turn",
                               usage=SimpleNamespace(input_tokens=10, output_tokens=5))


def test_conversar_ia_manda_a_etapa_e_reescreve_quando_o_conferente_aponta(conf, monkeypatch):
    f = Falso(["Para limpeza atendem a Dra. Ana e o Dr. Fabio. Tem preferência?",
               "A clínica não atende Amil, só particular. Para limpeza atendem a Dra. Ana e o Dr. Fabio. Tem preferência?"],
              [["não respondeu se atende Amil"]])
    monkeypatch.setattr(CL, "claude", lambda: f)
    monkeypatch.setattr(CL, "MODELO", "outro-modelo")
    ctx = {"slug": SLUG, "conf": conf, "tel": CL.EXP_TEL, "sim": True, "reservas": []}
    msgs = [{"role": "user", "content": "Oi, quero uma limpeza. Atende Amil?"}]
    r = asyncio.run(CL.conversar_ia("SYS", msgs, ctx, CL.K(SLUG, "tk")))
    assert r.startswith("A clínica não atende Amil")
    principal = [p for p in f.pedidos if "system" in p]
    assert "ETAPA DESTA RESPOSTA" in principal[0]["system"] and "atende Amil?" in principal[0]["system"]
    assert ctx["revisoes"] == [["não respondeu se atende Amil"]]
    assert len(msgs) == 1     # a lista do chamador nao muda


def test_conferente_desligado_nao_chama_ia_extra(conf, monkeypatch):
    f = Falso(["Claro! Me conta, é para quê?"], [])
    monkeypatch.setattr(CL, "claude", lambda: f)
    monkeypatch.setattr(CL, "MODELO", "outro-modelo")
    monkeypatch.setattr(CL, "CONFERENTE_LIGADO", False)
    ctx = {"slug": SLUG, "conf": conf, "tel": CL.EXP_TEL, "sim": True, "reservas": []}
    asyncio.run(CL.conversar_ia("SYS", [{"role": "user", "content": "Oi"}], ctx, CL.K(SLUG, "tk")))
    assert len(f.pedidos) == 1


def test_falha_do_conferente_nao_derruba_a_resposta(conf, monkeypatch):
    f = Falso(["Claro! Me conta, é para quê?"], [])
    def quebra(*a, **k):
        raise RuntimeError("fora do ar")
    monkeypatch.setattr(CL, "claude", lambda: f)
    monkeypatch.setattr(CL, "MODELO", "outro-modelo")
    monkeypatch.setattr(CL, "ler_estado", quebra)
    monkeypatch.setattr(CL, "conferir_resposta", quebra)
    ctx = {"slug": SLUG, "conf": conf, "tel": CL.EXP_TEL, "sim": True, "reservas": []}
    assert asyncio.run(CL.conversar_ia("SYS", [{"role": "user", "content": "Oi"}], ctx, CL.K(SLUG, "tk"))) == "Claro! Me conta, é para quê?"
