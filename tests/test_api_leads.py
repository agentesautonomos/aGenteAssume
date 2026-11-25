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
