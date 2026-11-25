"""
Testes do POST /api/leads (leads que chegam do scraper do Google Maps).

Redis de mentira (fakeredis): nada encosta no Redis de verdade.
"""
import importlib
import os
import sys

import fakeredis
import pytest
import redis
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CHAVE = "chave-de-teste"


@pytest.fixture
def app_main(monkeypatch):
    monkeypatch.setenv("LEADS_API_KEY", CHAVE)
    falso = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis, "from_url", lambda *a, **k: falso)
    import main
    main = importlib.reload(main)
    return main


def _post(app_main, corpo, chave=CHAVE):
    cliente = TestClient(app_main.app)
    return cliente.post("/api/leads", json=corpo, headers={"X-API-Key": chave})


LEAD = {
    "nome": "Sorria Jundiaí",
    "telefone": "11 99835-1187",
    "nicho": "dentista",
    "cidade": "Jundiaí",
    "endereco": "R. Barão de Jundiaí, 486 - Centro",
    "instagram": "https://www.instagram.com/sorriajundiai/",
    "horario": "seg a sex 09:00–19:00 | sáb 09:00–15:00 | dom fechado",
    "avaliacao": "4.7",
    "maps_url": "https://www.google.com/maps/place/Sorria+Jundia%C3%AD/",
}


def test_guarda_horario_e_instagram(app_main):
    resp = _post(app_main, [LEAD])
    assert resp.status_code == 200
    assert resp.json() == {"recebidos": 1, "importados": 1, "duplicados": 0, "invalidos": 0}
    lead = app_main.obter_lead("5511998351187")
    assert lead["horario"] == LEAD["horario"]
    assert lead["instagram"] == LEAD["instagram"]
    assert lead["cidade"] == "Jundiaí"
    assert lead["maps_url"] == LEAD["maps_url"]


def test_lead_sem_campos_novos_continua_valendo(app_main):
    antigo = {k: v for k, v in LEAD.items() if k not in ("horario", "instagram")}
    assert _post(app_main, antigo).json()["importados"] == 1
    lead = app_main.obter_lead("5511998351187")
    assert "horario" not in lead and "instagram" not in lead


def test_duplicado_e_fixo(app_main):
    _post(app_main, [LEAD])
    fixo = dict(LEAD, telefone="11 4411-5211")
    assert _post(app_main, [LEAD, fixo]).json() == {"recebidos": 2, "importados": 0, "duplicados": 1, "invalidos": 1}


def test_chave_errada(app_main):
    assert _post(app_main, [LEAD], chave="errada").status_code == 401


def test_painel_mostra_horario_e_instagram(app_main):
    _post(app_main, [LEAD])
    html = app_main.lead_detalhes_html(app_main.obter_lead("5511998351187"))
    assert "seg a sex 09:00–19:00" in html
    assert 'href="https://www.instagram.com/sorriajundiai/"' in html
    assert app_main.lead_detalhes_html({"nome": "x"}) == ""


# ---------------------------------------------------------------------------
# POST /api/leads/atualizar: completa horario e instagram de leads que ja existem
# ---------------------------------------------------------------------------
PID = "ChIJXdJy2m7BzpQRAN_eKcH3_Bg"
LEAD_ANTIGO = {
    "nome": "Oral Max", "telefone": "11 97000-1111", "nicho": "dentista", "cidade": "Atibaia",
    "maps_url": f"https://www.google.com/maps/place/Oral+Max/data=!4m7!3m6!1s0x94ce:0x18fc!19s{PID}?authuser=0&hl=pt-BR",
}


def _atualizar(app_main, corpo, simular=False, chave=CHAVE):
    cliente = TestClient(app_main.app)
    url = "/api/leads/atualizar" + ("?simular=1" if simular else "")
    return cliente.post(url, json=corpo, headers={"X-API-Key": chave})


def test_atualizar_completa_pelo_id_do_google(app_main):
    _post(app_main, [LEAD_ANTIGO])
    resp = _atualizar(app_main, [{"place_id": PID, "horario": "seg a sex 08:00–18:00", "instagram": "https://www.instagram.com/oralmax/"}])
    assert resp.status_code == 200
    assert resp.json() == {"recebidos": 1, "atualizados": 1, "sem_mudanca": 0, "nao_encontrados": 0,
                           "invalidos": 0, "simulacao": False}
    lead = app_main.obter_lead("5511970001111")
    assert lead["horario"] == "seg a sex 08:00–18:00"
    assert lead["instagram"] == "https://www.instagram.com/oralmax/"
    assert lead["nome"] == "Oral Max" and lead["status"] == "pendente"


def test_atualizar_nao_cria_lead(app_main):
    _post(app_main, [LEAD_ANTIGO])
    antes = len(app_main.listar_leads())
    resp = _atualizar(app_main, [{"place_id": "ChIJoutroLugar", "horario": "seg 08:00–12:00"}, {"horario": "sem id"}])
    assert resp.json()["nao_encontrados"] == 1 and resp.json()["invalidos"] == 1
    assert len(app_main.listar_leads()) == antes


def test_atualizar_simulacao_nao_grava(app_main):
    _post(app_main, [LEAD_ANTIGO])
    resp = _atualizar(app_main, [{"place_id": PID, "horario": "seg a sex 08:00–18:00"}], simular=True)
    assert resp.json()["atualizados"] == 1 and resp.json()["simulacao"] is True
    assert "horario" not in app_main.obter_lead("5511970001111")


def test_atualizar_nao_troca_valor_existente_e_e_idempotente(app_main):
    _post(app_main, [dict(LEAD_ANTIGO, horario="seg 09:00–17:00")])
    corpo = [{"place_id": PID, "horario": "seg a sex 08:00–18:00", "instagram": "https://www.instagram.com/oralmax/"}]
    assert _atualizar(app_main, corpo).json()["atualizados"] == 1      # so o instagram entra
    lead = app_main.obter_lead("5511970001111")
    assert lead["horario"] == "seg 09:00–17:00"
    assert _atualizar(app_main, corpo).json()["sem_mudanca"] == 1      # rodar de novo nao muda nada


def test_atualizar_preserva_status_de_isca_enviada(app_main):
    _post(app_main, [LEAD_ANTIGO])
    lead = app_main.obter_lead("5511970001111")
    lead["status"] = "enviado"
    app_main.salvar_lead(lead)
    _atualizar(app_main, [{"place_id": PID, "horario": "seg a sex 08:00–18:00"}])
    lead = app_main.obter_lead("5511970001111")
    assert lead["status"] == "enviado" and lead["horario"] == "seg a sex 08:00–18:00"


def test_atualizar_aceita_maps_url_no_lugar_do_id(app_main):
    _post(app_main, [LEAD_ANTIGO])
    resp = _atualizar(app_main, [{"maps_url": LEAD_ANTIGO["maps_url"], "horario": "seg a sex 08:00–18:00"}])
    assert resp.json()["atualizados"] == 1


def test_atualizar_chave_errada(app_main):
    assert _atualizar(app_main, [{"place_id": PID}], chave="errada").status_code == 401


DID = "0x94cec16eda72d25d:0x18fcf7c129dedf00"
URL_LONGA = ("https://www.google.com/maps/place/Oral+Max+-+Implantes+%7C+Pr%C3%B3teses+-+Cl%C3%ADnica+Odontol%C3%B3gica"
             f"+em+Atibaia/data=!4m7!3m6!1s{DID}!8m2!3d-23.1144656!4d-46.5551582!16s%2Fg%2F11nryr9dl6!19s{PID}"
             "?authuser=0&hl=pt-BR&g_ep=EgoyMDI2MTAyNy4wIJJjKgBIAVAD&rclk=1")


def test_link_cortado_casa_pelo_data_id_e_nunca_por_id_pela_metade(app_main):
    # lead antigo: o link foi salvo cortado em 200 caracteres, no meio do place_id
    lead = {"telefone": "5511970002222", "nome": "Oral Max", "status": "pendente",
            "maps_url": URL_LONGA[:URL_LONGA.index(PID) + 8]}
    assert "!19sChIJ" in lead["maps_url"] and PID not in lead["maps_url"]
    app_main.salvar_lead(lead)
    assert app_main.ids_do_google(lead) == [f"data_id:{DID}"]
    resp = _atualizar(app_main, [{"place_id": PID, "data_id": DID, "horario": "seg a sex 08:00–18:00"}])
    assert resp.json()["atualizados"] == 1
    assert app_main.obter_lead("5511970002222")["horario"] == "seg a sex 08:00–18:00"


def test_link_do_maps_agora_e_guardado_inteiro(app_main):
    _post(app_main, [dict(LEAD_ANTIGO, maps_url=URL_LONGA)])
    assert app_main.obter_lead("5511970001111")["maps_url"] == URL_LONGA


def test_resposta_do_dentista_nao_perde_os_dados_do_maps(app_main):
    _post(app_main, [LEAD])
    tel = "5511998351187"
    lead = app_main.obter_lead(tel)
    lead["status"], lead["enviado_em"] = "enviado", "2026-11-25T10:00:00"
    app_main.salvar_lead(lead)

    app_main.processar_retorno_lead(tel)

    assert app_main.obter_lead(tel) is None          # saiu da lista de leads (virou conversa)
    info = app_main.obter_lead_info(tel)
    for campo in ("nome", "cidade", "endereco", "horario", "instagram", "avaliacao", "maps_url", "nicho_texto"):
        assert info[campo] == lead[campo], campo
    assert info["enviado_em"] == "2026-11-25T10:00:00"
    assert info["status"] == "respondeu" and info["respondeu_em"]
    # o aviso de primeiro contato continua igual (nome, nicho, cidade e demora)
    pend = app_main.json.loads(app_main.r.get(f"contato_pendente:{tel}"))
    assert pend["nome"] == LEAD["nome"] and "demora_seg" in pend and "endereco" not in pend


def test_resposta_mantem_o_que_ja_estava_em_lead_info(app_main):
    _post(app_main, [LEAD])
    tel = "5511998351187"
    app_main.r.set(f"lead_info:{tel}", app_main.json.dumps({"sem_script": True}))
    app_main.processar_retorno_lead(tel)
    info = app_main.obter_lead_info(tel)
    assert info["sem_script"] is True and info["endereco"] == LEAD["endereco"]
