"""As telas mexidas abrem sem erro (Configuracoes e Agenda, inclusive em feriado)."""
from datetime import date, datetime, timezone

import clinicas as CL
from conftest import CLINICA


def sessao():
    return {"clinica": CLINICA, "usuario": {"email": "a@b.c", "nome": "Equipe"}}


def test_configuracoes_tem_opcao_de_feriado(conf):
    html = CL.painel_config(s=sessao()).body.decode()
    assert 'name="atende_feriados"' in html


def test_agenda_dia_e_semana_no_natal(conf, relogio):
    relogio.ajustar(datetime(2026, 12, 20, 13, 0, tzinfo=timezone.utc))
    profs = CL.AG.profs_com_agenda(conf)
    assert "Feriado: Natal" in CL.html_ag_dia("teste", conf, profs, date(2026, 12, 25), False)
    assert "Natal" in CL.html_ag_semana("teste", conf, profs, date(2026, 12, 21), False, "")
