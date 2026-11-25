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


# ---------- Logo da clínica ----------
from urllib.parse import unquote  # noqa: E402


class _Arq:
    def __init__(self, dados: bytes):
        self.dados = dados

    async def read(self, n=-1):
        return self.dados


PNG_1PX = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                        "1f15c4890000000d49444154789c6360000002000001e221bc330000000049454e44ae426082")


def test_logo_enviar_mostrar_e_tirar():
    CL.salvar_clinica(dict(CLINICA))
    s = {"clinica": CL.obter_clinica("teste"), "usuario": {"email": "a@b.c"}}
    assert 'class="clogo sem"' in CL.pagina("x", "", s)          # sem logo: iniciais

    r = asyncio.run(CL.painel_logo_post(_Arq(PNG_1PX), s=s))
    assert "Logo atualizado" in unquote(r.headers["location"])
    s["clinica"] = CL.obter_clinica("teste")
    assert '/painel/logo?v=' in CL.pagina("x", "", s)
    img = CL.painel_logo(s=s)
    assert img.body == PNG_1PX and img.media_type == "image/png"

    CL.painel_logo_remover(s=s)
    assert not CL.obter_clinica("teste").get("logo_v")


def test_logo_recusa_svg_e_arquivo_grande():
    CL.salvar_clinica(dict(CLINICA))
    s = {"clinica": CL.obter_clinica("teste"), "usuario": {"email": "a@b.c"}}
    r = asyncio.run(CL.painel_logo_post(_Arq(b'<svg onload="alert(1)"></svg>'), s=s))
    assert "PNG" in unquote(r.headers["location"])
    r = asyncio.run(CL.painel_logo_post(_Arq(PNG_1PX + b"0" * CL.LOGO_MAX_BYTES), s=s))
    assert "500" in r.headers["location"]
    assert not CL.obter_clinica("teste").get("logo_v")


# ---------- agendamento com horário, nome e número ----------
def test_agenda_do_dia_mostra_nome_e_telefone(conf):
    p = CL.AG.achar_prof(conf, "ana1")
    CL.AG.reservar("teste", conf, p, date(2026, 11, 26), "09:00", {"paciente": "João", "tel": "5511999990000"})
    html = CL.html_ag_dia("teste", conf, CL.AG.profs_com_agenda(conf), date(2026, 11, 26), False)
    assert "João" in html and "(11) 99999-0000" in html and "09:00" in html
    sem = CL.html_ag_semana("teste", conf, CL.AG.profs_com_agenda(conf), date(2026, 11, 23), False, "")
    assert 'title="(11) 99999-0000"' in sem


def test_marcar_pela_equipe_exige_nome_e_whatsapp(conf):
    base = {"prof": "ana1", "d": "2026-11-26", "h": "10:00", "paciente": "Maria"}
    r = _post(CL.painel_agenda_nova_post, {**base, "tel": ""})
    assert "erro=" in r.headers["location"]
    r = _post(CL.painel_agenda_nova_post, {**base, "tel": "(11) 98888-7777"})
    assert "msg=" in r.headers["location"]
    it = [x for x in CL.AG.listar("teste", date(2026, 11, 26), date(2026, 11, 26)) if x["paciente"] == "Maria"][0]
    assert it["tel"] == "5511988887777"     # no formato do WhatsApp: o assistente acha a consulta pelo número


def test_tel_paciente():
    assert CL.tel_paciente("(51) 99268-7358") == "5551992687358"
    assert CL.tel_paciente("5551992687358") == "5551992687358"
    assert CL.tel_paciente("123") == ""
