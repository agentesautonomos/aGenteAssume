"""
Edicao de lead no painel (/admin/leads/editar/<tel>).

Redis de mentira (fakeredis): nada encosta no Redis de verdade, sem custo.
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
ADMIN = ("admin", "admin123")
TEL = "5511970001111"
PID = "ChIJXdJy2m7BzpQRAN_eKcH3_Bg"
LEAD = {"nome": "Oral Max", "telefone": "11 97000-1111", "nicho": "dentista", "cidade": "Atibaia",
        "endereco": "Rua A, 10", "place_id": PID,
        "maps_url": f"https://www.google.com/maps/place/Oral+Max/data=!4m7!3m6!1s0x94ce:0x18fc!19s{PID}?hl=pt-BR"}


@pytest.fixture
def m(monkeypatch):
    monkeypatch.setenv("LEADS_API_KEY", CHAVE)
    falso = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis, "from_url", lambda *a, **k: falso)
    import main
    main = importlib.reload(main)
    c = TestClient(main.app)
    c.post("/api/leads", json=[LEAD], headers={"X-API-Key": CHAVE})
    main.cliente = c
    return main


def _salvar(m, dados):
    return m.cliente.post(f"/admin/leads/editar/{TEL}", data=dados, auth=ADMIN, follow_redirects=False)


def _form_atual(m, **mudancas):
    lead = m._registro_editavel(TEL)[1]
    d = {k: lead.get(k, "") for k, _ in m.LEAD_CAMPOS_EDITAVEIS}
    d.update(mudancas)
    return d


def test_tela_mostra_campos_e_telefone_fixo(m):
    html = m.cliente.get(f"/admin/leads/editar/{TEL}", auth=ADMIN).text
    assert 'name="endereco"' in html and "Rua A, 10" in html
    assert 'name="telefone"' not in html and TEL in html and PID in html
    assert f'/admin/leads/editar/{TEL}' in m.cliente.get("/admin/leads", auth=ADMIN).text


def test_edita_qualquer_campo(m):
    resp = _salvar(m, _form_atual(m, nome="Oral Max Atibaia", endereco="Rua B, 20", horario="seg a sex 8h-18h",
                                  instagram="@oralmax", site="oralmax.com.br"))
    assert resp.status_code == 303
    lead = m.obter_lead(TEL)
    assert lead["nome"] == "Oral Max Atibaia" and lead["endereco"] == "Rua B, 20"
    assert lead["horario"] == "seg a sex 8h-18h" and lead["instagram"] == "@oralmax"
    assert set(lead["editado_manual"]) == {"nome", "endereco", "horario", "instagram", "site"}
    assert lead["telefone"] == TEL and lead["place_id"] == PID and lead["status"] == "pendente"
    assert m.obter_funil(TEL)["nome"] == "Oral Max Atibaia"


def test_telefone_e_ids_nao_mudam_mesmo_se_mandados(m):
    _salvar(m, _form_atual(m, telefone="5511999999999", place_id="ChIJoutro", nome="X"))
    lead = m.obter_lead(TEL)
    assert lead["telefone"] == TEL and lead["place_id"] == PID and lead["nome"] == "X"
    assert m.obter_lead("5511999999999") is None


def test_scraper_nao_sobrescreve_edicao_manual_nem_campo_apagado(m):
    _salvar(m, _form_atual(m, horario="meu horario"))
    _salvar(m, _form_atual(m, instagram=""))                        # apagou de proposito (estava vazio: nada muda)
    m.salvar_lead({**m.obter_lead(TEL), "instagram": "@errado"})
    _salvar(m, _form_atual(m, instagram=""))                        # agora apagou de verdade
    res = m.cliente.post("/api/leads/atualizar", headers={"X-API-Key": CHAVE},
                         json=[{"place_id": PID, "horario": "do scraper", "instagram": "@doscraper"}]).json()
    lead = m.obter_lead(TEL)
    assert lead["horario"] == "meu horario"
    assert "instagram" not in lead
    assert res["sem_mudanca"] == 1


def test_edita_lead_que_ja_respondeu(m):
    m.processar_retorno_lead(TEL)
    assert m.obter_lead(TEL) is None
    _salvar(m, _form_atual(m, endereco="Rua C, 30"))
    assert m.obter_lead_info(TEL)["endereco"] == "Rua C, 30"
    assert m.obter_lead(TEL) is None                                # nao volta para a lista de pendentes


def test_sem_login_nao_edita(m):
    resp = m.cliente.post(f"/admin/leads/editar/{TEL}", data={"nome": "hack"})
    assert resp.status_code == 401
    assert m.obter_lead(TEL)["nome"] == "Oral Max"
