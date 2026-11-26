"""
Funil de vendas (pendente, abordado, respondeu, nao quero, testando, pagou),
bloqueio permanente do "nao quero" e origem do contato (LGPD).

Redis de mentira (fakeredis) e WhatsApp de mentira: nada sai de verdade, sem custo.
"""
import asyncio
import importlib
import os
import sys

import fakeredis
import pytest
import redis
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CHAVE = "chave-de-teste"
TEL = "5511998351187"
LEAD = {"nome": "Sorria Jundiaí", "telefone": "11 99835-1187", "nicho": "dentista", "cidade": "Jundiaí",
        "maps_url": "https://www.google.com/maps/place/Sorria/data=!4m7!3m6!1s0x94ce:0x18fc!19sChIJabc?hl=pt-BR",
        "coletado_em": "2026-11-20T21:00:00"}
ADMIN = ("admin", "admin123")


@pytest.fixture
def m(monkeypatch):
    monkeypatch.setenv("LEADS_API_KEY", CHAVE)
    monkeypatch.setenv("DEBOUNCE_SEGUNDOS", "0")
    falso = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis, "from_url", lambda *a, **k: falso)
    import main
    main = importlib.reload(main)
    main.enviados = []

    async def enviar_falso(tel, msg):
        main.enviados.append((tel, msg))
        return True

    async def claude_proibido(*a, **k):
        raise AssertionError("a IA nao deveria ser chamada")

    monkeypatch.setattr(main, "enviar_whatsapp", enviar_falso)
    monkeypatch.setattr(main, "chamar_claude", claude_proibido)
    return main


def _cliente(m):
    return TestClient(m.app)


def _importar(m, lead=LEAD):
    return _cliente(m).post("/api/leads", json=[lead], headers={"X-API-Key": CHAVE}).json()


def _isca(m, tel=TEL):
    return _cliente(m).post(f"/admin/leads/oi/{tel}", auth=ADMIN, follow_redirects=False)


def _chega(m, texto, tel=TEL):
    m.r.rpush(f"{m.FILA_PREFIX}{tel}", texto)
    seq = m.r.incr(f"{m.FILA_SEQ_PREFIX}{tel}")
    asyncio.run(m._responder_apos_espera(tel, int(seq)))


def test_importar_cria_funil_pendente_com_origem_do_maps(m):
    assert _importar(m)["importados"] == 1
    f = m.obter_funil(TEL)
    assert f["status"] == "pendente"
    assert f["origem"]["fonte"] == m.ORIGEM_MAPS
    assert f["origem"]["link"] == LEAD["maps_url"]
    assert f["origem"]["coletado_em"] == "2026-11-20T21:00:00"
    assert m.obter_lead(TEL)["origem"] == m.ORIGEM_MAPS


def test_importacao_manual_tem_origem_manual(m):
    m.importar_leads("Dr Joao ; 11 99999-8888 ; dentista")
    f = m.obter_funil("5511999998888")
    assert f["origem"]["fonte"] == m.ORIGEM_MANUAL and f["origem"]["coletado_em"]


def test_isca_e_resposta_mudam_o_status_com_data(m):
    _importar(m)
    _isca(m)
    f = m.obter_funil(TEL)
    assert f["status"] == "enviado"
    m.processar_retorno_lead(TEL)
    f = m.obter_funil(TEL)
    assert f["status"] == "respondeu"
    assert [h["status"] for h in f["historico"]] == ["pendente", "enviado", "respondeu"]
    assert all(h["em"] for h in f["historico"])


def test_status_automatico_nao_volta_para_tras(m):
    _importar(m)
    m.mudar_status(TEL, "pagou", forcar=True)
    m.mudar_status(TEL, "respondeu")
    assert m.obter_funil(TEL)["status"] == "pagou"


@pytest.mark.parametrize("texto", ["Não quero", "nao quero, obrigado", "PARE", "Sair", "sem interesse.",
                                   "Não tenho interesse no momento", "por favor tira meu número dessa lista",
                                   "parem de mandar mensagem", "Quero me descadastrar"])
def test_reconhece_nao_quero(m, texto):
    assert m.eh_nao_quero(texto)


@pytest.mark.parametrize("texto", ["Não quero pagar mensalidade, como funciona?", "quero sim",
                                   "para quando seria?", "Para clareamento custa R$ 450", "Quanto custa?",
                                   "não quero perder paciente", ""])
def test_nao_confunde_com_conversa_normal(m, texto):
    assert not m.eh_nao_quero(texto)


def test_nao_quero_bloqueia_para_sempre(m):
    _importar(m)
    _isca(m)
    m.enviados.clear()
    _chega(m, "Não tenho interesse, obrigado")

    assert m.esta_bloqueado(TEL)
    assert m.obter_funil(TEL)["status"] == "nao_quero"
    assert m.enviados == [(TEL, m.MSG_NAO_QUERO)]            # so a despedida, sem IA
    assert m.obter_flow(TEL)[1] is None                      # nao recebe o toque de 24h
    assert m.obter_lead_info(TEL)["cidade"] == "Jundiaí"              # dados do Maps continuam guardados

    # nunca volta: reimportar e ignorado, isca e recusada
    assert _importar(m)["importados"] == 0
    m.enviados.clear()
    _isca(m)
    assert m.enviados == []

    # "Zerar banco" e "Apagar conversa" nao tiram o bloqueio
    _cliente(m).post("/admin/zerar-banco", auth=ADMIN, follow_redirects=False)
    _cliente(m).get(f"/admin/apagar/{TEL}", auth=ADMIN, follow_redirects=False)
    assert m.esta_bloqueado(TEL)
    assert _importar(m)["importados"] == 0


def test_nao_quero_de_quem_nao_e_lead_nao_bloqueia(m):
    async def claude_ok(tel, texto):
        return "resposta"
    m.chamar_claude = claude_ok
    _chega(m, "pare", tel="5511912345678")
    assert not m.esta_bloqueado("5511912345678")


def test_toque_24h_pula_bloqueado(m, monkeypatch):
    _importar(m)
    _isca(m)
    m.r.set(f"{m.NAO_CONTATAR_PREFIX}{TEL}", "{}")           # bloqueado sem apagar o fluxo
    chave, fl = m.obter_flow(TEL)
    fl["enviado_em"] = "2026-01-01T00:00:00"
    m.salvar_flow(chave, fl)
    monkeypatch.setattr(m, "horario_comercial", lambda: True)
    m.enviados.clear()
    asyncio.run(m._verificar_toques())
    assert m.enviados == []


def test_painel_mostra_funil_e_marca_testando_pagou_e_desbloqueia(m):
    _importar(m)
    _isca(m)
    m.processar_retorno_lead(TEL)
    c = _cliente(m)
    html = c.get("/admin/leads", auth=ADMIN).text
    assert "Funil" in html and "Respondeu" in html and m.ORIGEM_MAPS in html

    c.post(f"/admin/leads/status/{TEL}", data={"status": "testando"}, auth=ADMIN, follow_redirects=False)
    assert m.obter_funil(TEL)["status"] == "testando"
    c.post(f"/admin/leads/status/{TEL}", data={"status": "pagou"}, auth=ADMIN, follow_redirects=False)
    assert m.obter_funil(TEL)["status"] == "pagou"
    c.post(f"/admin/leads/status/{TEL}", data={"status": "nao_quero"}, auth=ADMIN, follow_redirects=False)
    assert m.esta_bloqueado(TEL)
    c.post(f"/admin/leads/desbloquear/{TEL}", auth=ADMIN, follow_redirects=False)
    assert not m.esta_bloqueado(TEL)


def test_lead_antigo_sem_funil_aparece_no_painel(m):
    m.salvar_lead({"telefone": TEL, "nome": "Antigo", "status": "enviado",
                   "enviado_em": "2026-11-16T10:00:00", "importado_em": "2026-11-16T09:00:00",
                   "maps_url": "https://maps"})
    itens = m.listar_funil()
    assert itens[0]["status"] == "enviado" and itens[0]["origem"]["fonte"] == m.ORIGEM_MAPS
    m.processar_retorno_lead(TEL)
    assert m.obter_funil(TEL)["status"] == "respondeu"
