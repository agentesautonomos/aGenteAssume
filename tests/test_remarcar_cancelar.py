"""O assistente remarca e cancela sozinho, so consultas do proprio paciente e respeitando a antecedencia."""
import re
from datetime import date

import agenda as AG
import clinicas as CL
from conftest import SLUG

TEL = "5511999990000"


def ctx_de(conf, tel=TEL):
    return {"slug": SLUG, "conf": conf, "tel": tel, "sim": False, "reservas": []}


def marcar(conf, dia="2026-11-26", hora="09:00", tel=TEL):
    ok, it = AG.reservar(SLUG, conf, AG.achar_prof(conf, "ana1"), date.fromisoformat(dia), hora,
                         {"paciente": "João", "tel": tel})
    assert ok, it
    return it


def test_lista_traz_o_codigo_e_a_data_escrita(conf):
    it = marcar(conf)
    r = CL.executar_ferramenta("consultas_do_paciente", {}, ctx_de(conf))
    assert it["id"] in r and "quinta 26/11 às 9h" in r and "Dra. Ana" in r


def test_remarca_libera_o_horario_antigo_e_registra(conf):
    it = marcar(conf)
    ctx = ctx_de(conf)
    r = CL.executar_ferramenta("remarcar_consulta", {"consulta": it["id"], "profissional": "Dra. Ana Souza",
                                                     "data": "2026-11-27", "hora": "10:00"}, ctx)
    assert "remarcada" in r and "sexta 27/11 às 10h" in r
    assert "09:00" in AG.livres_no_dia(SLUG, conf, AG.achar_prof(conf, "ana1"), date(2026, 11, 26))
    assert AG.obter(SLUG, it["id"])["data"] == "2026-11-27"
    assert ctx["mudancas"][0]["acao"] == "remarcada"


def test_cancela_e_registra(conf):
    it = marcar(conf)
    ctx = ctx_de(conf)
    r = CL.executar_ferramenta("cancelar_consulta", {"consulta": it["id"], "motivo": "viagem"}, ctx)
    assert "cancelada" in r
    assert AG.obter(SLUG, it["id"])["status"] == "cancelada"
    assert ctx["mudancas"][0]["acao"] == "cancelada" and "viagem" in CL.aviso_mudanca(ctx["mudancas"][0], TEL)


def test_nao_mexe_em_consulta_de_outro_paciente(conf):
    it = marcar(conf, tel="5511888880000")
    r = CL.executar_ferramenta("cancelar_consulta", {"consulta": it["id"]}, ctx_de(conf))
    assert "não encontrada" in r and AG.obter(SLUG, it["id"])["status"] == "marcada"


def test_em_cima_da_hora_passa_para_a_equipe(conf):
    it = marcar(conf, dia="2026-11-24", hora="09:00")     # agora e segunda 10:00: faltam 23h
    r = CL.executar_ferramenta("cancelar_consulta", {"consulta": it["id"]}, ctx_de(conf))
    assert "Não alterado" in r and "24 horas" in r and "ENCAMINHAR" in r
    assert AG.obter(SLUG, it["id"])["status"] == "marcada"


def test_antecedencia_configuravel(conf):
    conf["ag_antecedencia_mudar_h"] = "12"
    it = marcar(conf, dia="2026-11-24", hora="09:00")
    r = CL.executar_ferramenta("cancelar_consulta", {"consulta": it["id"]}, ctx_de(conf))
    assert "cancelada" in r


def test_remarcar_para_horario_ocupado_sugere_outros(conf):
    it = marcar(conf)
    marcar(conf, dia="2026-11-27", hora="10:00", tel="5511777770000")
    r = CL.executar_ferramenta("remarcar_consulta", {"consulta": it["id"], "profissional": "Dra. Ana Souza",
                                                     "data": "2026-11-27", "hora": "10:00"}, ctx_de(conf))
    assert r.startswith("Não remarcado") and "Livres perto disso" in r
    assert AG.obter(SLUG, it["id"])["data"] == "2026-11-26"


def test_contagem_no_painel(conf):
    CL.contar(SLUG, "remarcacoes")
    CL.contar(SLUG, "cancelamentos", 2)
    e = CL.estatisticas(SLUG, 7)
    assert e["remarcacoes"] == 1 and e["cancelamentos"] == 2


def test_prompt_ensina_a_remarcar_e_a_regra_de_antecedencia(conf):
    p = CL.montar_prompt({"slug": SLUG, "nome": "Clínica"}, conf)
    assert "remarcar_consulta" in p and "24 horas antes" in p
    assert "Remarcar ou cancelar consulta: passe para a equipe" not in p


def test_horario_da_consulta_atual_nao_conta_como_oferta(conf):
    marcar(conf)   # quinta 26/11 9h
    t = "Sua consulta é quinta 26/11 às 9h. Na sexta 27/11 tenho 8h, 10h ou 11h30. Qual prefere?"
    _, p = CL.revisar_resposta(t, ja_marcadas=CL._horas_ja_marcadas(ctx_de(conf), []))
    assert not any("horários" in x for x in p)
    _, p = CL.revisar_resposta(t)
    assert any("ofereceu 4 horários" in x for x in p)


def test_disse_que_passa_para_a_recepcao_sem_a_linha():
    _, p = CL.revisar_resposta("Vou passar o seu pedido para a recepção confirmar o horário.")
    assert any("faltou a linha" in x for x in p)
    _, p = CL.revisar_resposta("Vou passar o seu pedido para a recepção.\n[AGENDAMENTO: Lia | Dr. Carlos | sábado | particular | ]")
    assert not any("faltou a linha" in x for x in p)
