"""Regras da agenda: dias e horarios de cada profissional, duracao, antecedencia, janela,
conflitos, bloqueios, cancelamento, simulacao, fuso e feriados.

"Agora" padrao: segunda 23/11/2026 10:00 em Sao Paulo (13:00 UTC).
"""
from datetime import date, datetime, timezone

import pytest

import agenda as AG
from conftest import SLUG

SEG, TER, QUA, QUI, SEX, SAB, DOM = (date(2026, 11, d) for d in (23, 24, 25, 26, 27, 28, 29))


def prof(conf, pid):
    return next(p for p in conf["profissionais"] if p["id"] == pid)


def livres(conf, pid, dia, sim=False):
    return AG.livres_no_dia(SLUG, conf, prof(conf, pid), dia, sim)


def marcar(conf, pid, dia, hora, sim=False, nome="Paciente", tel="5511999990000"):
    return AG.reservar(SLUG, conf, prof(conf, pid), dia, hora, {"paciente": nome, "tel": tel}, sim=sim)


# ---------- horarios escritos de varios jeitos ----------
@pytest.mark.parametrize("txt,esperado", [
    ("8h-12h, 14h-18h", [(480, 720), (840, 1080)]),
    ("08:00-12:00", [(480, 720)]),
    ("8h às 12h", [(480, 720)]),
    ("8h30-12h", [(510, 720)]),
    ("13h30 - 18h", [(810, 1080)]),
    ("8h a 12h e 14h a 18h", [(480, 720), (840, 1080)]),
    ("", []),
])
def test_ler_horarios(txt, esperado):
    assert AG.ler_intervalos(txt) == esperado


# ---------- dias e duracao ----------
def test_fabio_nao_atende_quarta(conf):
    assert livres(conf, "fab1", QUA) == []


def test_fabio_consulta_de_uma_hora(conf):
    assert livres(conf, "fab1", TER) == ["14:00", "15:00", "16:00", "17:00"]


def test_ninguem_no_fim_de_semana(conf):
    assert livres(conf, "ana1", SAB) == [] and livres(conf, "ana1", DOM) == []


def test_ana_dia_inteiro(conf):
    hs = livres(conf, "ana1", TER)
    assert hs[0] == "08:00" and hs[-1] == "17:30" and "12:00" not in hs and len(hs) == 16


# ---------- horario passado e antecedencia ----------
def test_hoje_so_depois_da_antecedencia(conf):
    # agora 10:00, antecedencia 2h: nada antes de 12:00; manha acabou -> comeca 14:00
    assert livres(conf, "ana1", SEG)[0] == "14:00"


def test_nao_marca_horario_que_ja_passou(conf):
    ok, motivo = marcar(conf, "ana1", SEG, "09:00")
    assert not ok


def test_nao_marca_ontem(conf):
    ok, _ = marcar(conf, "ana1", date(2026, 11, 20), "14:00")
    assert not ok


def test_janela_de_30_dias(conf):
    assert livres(conf, "ana1", date(2026, 12, 23)) != []      # 30 dias
    assert livres(conf, "ana1", date(2026, 12, 28)) == []      # 35 dias
    ok, _ = marcar(conf, "ana1", date(2026, 12, 28), "09:00")
    assert not ok


# ---------- conflitos ----------
def test_mesmo_horario_nao_marca_duas_vezes(conf):
    assert marcar(conf, "ana1", TER, "09:00")[0]
    ok, motivo = marcar(conf, "ana1", TER, "09:00", nome="Outro")
    assert not ok and "livre" in motivo


def test_outro_profissional_no_mesmo_horario_pode(conf):
    assert marcar(conf, "fab1", TER, "14:00")[0]
    assert marcar(conf, "ana1", TER, "14:00")[0]


def test_consulta_de_uma_hora_ocupa_a_hora_toda(conf):
    assert marcar(conf, "fab1", TER, "15:00")[0]
    assert "15:00" not in livres(conf, "fab1", TER)
    # nao da para marcar 15:30 encaixe por cima
    ok, _ = AG.reservar(SLUG, conf, prof(conf, "fab1"), TER, "15:30", {}, encaixe=True)
    assert not ok


def test_horario_fora_da_grade_nao_marca(conf):
    ok, _ = marcar(conf, "fab1", TER, "14:30")    # grade do Fabio e de hora em hora
    assert not ok
    ok, _ = marcar(conf, "ana1", TER, "12:30")    # almoco
    assert not ok


# ---------- bloqueios ----------
def test_bloqueio_do_dia_todo_para_todos(conf):
    AG.bloquear(SLUG, None, TER, "", "", "Feriado da cidade")
    assert livres(conf, "ana1", TER) == [] and livres(conf, "fab1", TER) == []


def test_bloqueio_de_um_profissional_nao_afeta_o_outro(conf):
    AG.bloquear(SLUG, prof(conf, "ana1"), TER, "", "", "Férias")
    assert livres(conf, "ana1", TER) == []
    assert livres(conf, "fab1", TER) != []


def test_bloqueio_parcial(conf):
    AG.bloquear(SLUG, prof(conf, "ana1"), TER, "15:00", "16:00", "Dentista")
    hs = livres(conf, "ana1", TER)
    assert "15:00" not in hs and "15:30" not in hs and "14:30" in hs and "16:00" in hs


def test_desbloquear_libera(conf):
    ok, it = AG.bloquear(SLUG, None, TER, "", "", "x")
    AG.desbloquear(SLUG, it["id"])
    assert livres(conf, "ana1", TER) != []


# ---------- cancelar e remarcar ----------
def test_cancelar_libera_o_horario(conf):
    ok, it = marcar(conf, "ana1", TER, "09:00")
    AG.cancelar(SLUG, it["id"])
    assert "09:00" in livres(conf, "ana1", TER)


def test_remarcar_para_horario_livre_e_libera_o_antigo(conf):
    ok, it = marcar(conf, "ana1", TER, "09:00")
    ok, it2 = AG.mover(SLUG, conf, it["id"], prof(conf, "ana1"), QUI, "10:00")
    assert ok and it2["data"] == QUI.isoformat()
    assert "09:00" in livres(conf, "ana1", TER) and "10:00" not in livres(conf, "ana1", QUI)


def test_remarcar_para_dia_que_o_profissional_nao_atende(conf):
    ok, it = marcar(conf, "fab1", TER, "14:00")
    ok, motivo = AG.mover(SLUG, conf, it["id"], prof(conf, "fab1"), QUA, "14:00")
    assert not ok


# ---------- simulacao (aba Experimentar) ----------
def test_simulacao_nao_ocupa_a_agenda_real(conf):
    assert marcar(conf, "ana1", TER, "09:00", sim=True)[0]
    assert "09:00" in livres(conf, "ana1", TER)
    assert "09:00" not in livres(conf, "ana1", TER, sim=True)


def test_agenda_real_ocupa_a_simulacao(conf):
    assert marcar(conf, "ana1", TER, "09:00")[0]
    assert "09:00" not in livres(conf, "ana1", TER, sim=True)


# ---------- fuso de Sao Paulo ----------
def test_fuso_noite_em_sp_ja_e_amanha_em_utc(conf, relogio):
    # 01:30 UTC de terca = 22:30 de segunda em Sao Paulo
    relogio.ajustar(datetime(2026, 11, 24, 1, 30, tzinfo=timezone.utc))
    assert AG.agora_local().date() == SEG
    # terca 08:00 ja da (antecedencia de 2h folgada); segunda nao tem mais nada
    assert livres(conf, "ana1", SEG) == []
    assert livres(conf, "ana1", TER)[0] == "08:00"


def test_fuso_madrugada(conf, relogio):
    # 10:30 UTC = 07:30 em SP: 08:00 esta a menos de 2h, primeiro livre e 09:30
    relogio.ajustar(datetime(2026, 11, 24, 10, 30, tzinfo=timezone.utc))
    assert livres(conf, "ana1", TER)[0] == "09:30"


# ---------- feriados nacionais ----------
@pytest.mark.parametrize("dia", [date(2026, 12, 25), date(2027, 1, 1)])
def test_nao_oferece_feriado_nacional(conf, relogio, dia):
    relogio.ajustar(datetime(2026, 12, 20, 13, 0, tzinfo=timezone.utc))
    assert livres(conf, "ana1", dia) == []


def test_sexta_feira_santa(conf, relogio):
    relogio.ajustar(datetime(2027, 3, 20, 13, 0, tzinfo=timezone.utc))
    assert livres(conf, "ana1", date(2027, 3, 26)) == []     # Pascoa 2027: 28/03


def test_clinica_que_atende_em_feriado(conf, relogio):
    relogio.ajustar(datetime(2026, 12, 20, 13, 0, tzinfo=timezone.utc))
    conf["atende_feriados"] = "1"
    assert livres(conf, "ana1", date(2026, 12, 25)) != []
