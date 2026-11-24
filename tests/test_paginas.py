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


# ---------- Profissionais (cards + painel de adicionar/editar) ----------
import asyncio  # noqa: E402

from starlette.datastructures import FormData  # noqa: E402


class _Req:
    def __init__(self, dados: dict):
        self._f = FormData(dados)

    async def form(self):
        return self._f


def _post(fn, dados, *args):
    r = fn(*args, _Req(dados), s=sessao()) if args else fn(_Req(dados), s=sessao())
    return asyncio.run(r) if asyncio.iscoroutine(r) else r


def test_profissionais_lista_em_cards_com_menu(conf):
    html = CL.painel_profissionais(s=sessao()).body.decode()
    assert html.count('class="icard"') == 3
    assert "?editar=ana1" in html and "?remover=fab1" in html
    assert 'class="folha"' not in html     # o painel só abre quando pede


def test_profissionais_sem_ninguem_mostra_lista_vazia():
    html = CL.painel_profissionais(s=sessao()).body.decode()
    assert "Adicionar o primeiro profissional" in html


def test_profissional_novo_editar_e_remover(conf):
    assert 'class="folha"' in CL.painel_profissionais(novo=1, s=sessao()).body.decode()
    dados = {"prof_id": "", "prof_nome": "Dra. Bia Nunes", "prof_especialidades": "Endodontia",
             "prof_convenios": "particular", "prof_ag0": "8h-12h", "prof_dur": "45"}
    _post(CL.painel_profissionais_post, dados)
    profs = CL.obter_conf("teste")["profissionais"]
    bia = next(p for p in profs if p["nome"] == "Dra. Bia Nunes")
    assert bia["id"] and bia["agenda"]["duracao"] == 45 and CL.AG.intervalos_do_dia(bia, 0)
    assert len(profs) == 4

    # editar mantém o id (a agenda usa o id) e não mexe nos outros
    _post(CL.painel_profissionais_post, {**dados, "prof_id": "ana1", "prof_nome": "Dra. Ana Souza Lima"})
    profs = CL.obter_conf("teste")["profissionais"]
    assert next(p for p in profs if p["id"] == "ana1")["nome"] == "Dra. Ana Souza Lima"
    assert len(profs) == 4

    CL.painel_profissionais_remover("fab1", s=sessao())
    assert [p["id"] for p in CL.obter_conf("teste")["profissionais"]] == ["ana1", "car1", bia["id"]]


def test_salvar_sua_clinica_nao_apaga_profissionais(conf):
    _post(CL.painel_config_post, {"endereco": "Rua Nova, 1", "funcionamento": "Seg a sex"})
    c = CL.obter_conf("teste")
    assert c["endereco"] == "Rua Nova, 1"
    assert len(c["profissionais"]) == 3


def test_simular_abre_com_sugestoes(conf):
    CL.salvar_clinica(dict(CLINICA))
    html = CL.painel_experimentar(s=sessao()).body.decode()
    assert "Seja o paciente" in html and 'class="chip"' in html
