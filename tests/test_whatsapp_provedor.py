"""
Provedor de WhatsApp do numero principal: Z-API (padrao) ou Evolution (por variavel),
e a medicao do teste (quedas, tempo fora, envios e recebidas).

Redis de mentira e HTTP de mentira: nada sai de verdade.
"""
import asyncio
import base64
import importlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import fakeredis
import httpx
import pytest
import redis
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import whatsapp_provedor as WA   # noqa: E402

ADMIN = ("admin", "admin123")


@pytest.fixture
def wa(monkeypatch):
    for k in ("WHATSAPP_PROVEDOR", "EVOLUTION_WEBHOOK_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ZAPI_INSTANCE_ID", "INST")
    monkeypatch.setenv("ZAPI_TOKEN", "TOK")
    monkeypatch.setenv("ZAPI_CLIENT_TOKEN", "CT")
    monkeypatch.setenv("EVOLUTION_URL", "https://evo.exemplo/")
    monkeypatch.setenv("EVOLUTION_API_KEY", "CHAVE")
    monkeypatch.setenv("EVOLUTION_INSTANCE", "vendas")
    WA.usar_redis(fakeredis.FakeRedis(decode_responses=True))
    return WA


@pytest.fixture
def http_falso(monkeypatch):
    """Troca o httpx.AsyncClient por um que grava as chamadas e responde o que o teste mandar."""
    chamadas = []
    respostas = {}

    def tratar(req: httpx.Request):
        chamadas.append(req)
        for trecho, (status, corpo) in respostas.items():
            if trecho in str(req.url):
                return httpx.Response(status, json=corpo)
        return httpx.Response(200, json={})

    original = httpx.AsyncClient

    def cliente(*a, **k):
        k["transport"] = httpx.MockTransport(tratar)
        return original(*a, **k)

    monkeypatch.setattr(httpx, "AsyncClient", cliente)
    return chamadas, respostas


# ---------------- envio ----------------

def test_padrao_e_zapi(wa, http_falso):
    chamadas, _ = http_falso
    assert wa.provedor() == "zapi"
    assert asyncio.run(wa.enviar_texto("+55 (51) 9926-8735", "oi")) is True
    req = chamadas[0]
    assert "api.z-api.io/instances/INST/token/TOK/send-text" in str(req.url)
    assert req.headers["Client-Token"] == "CT"
    corpo = json.loads(req.content)
    assert corpo["phone"] == "5551999268735"   # ganhou o 9
    assert corpo["message"] == "oi"


def test_evolution_por_variavel(wa, http_falso, monkeypatch):
    chamadas, _ = http_falso
    monkeypatch.setenv("WHATSAPP_PROVEDOR", "evolution")
    assert asyncio.run(wa.enviar_texto("5551999268735", "olá")) is True
    req = chamadas[0]
    assert str(req.url) == "https://evo.exemplo/message/sendText/vendas"
    assert req.headers["apikey"] == "CHAVE"
    assert json.loads(req.content) == {"number": "5551999268735", "text": "olá"}


def test_falha_de_envio_e_contada(wa, http_falso, monkeypatch):
    _, respostas = http_falso
    monkeypatch.setenv("WHATSAPP_PROVEDOR", "evolution")
    respostas["sendText"] = (500, {"erro": "desconectado"})
    assert asyncio.run(wa.enviar_texto("5551999268735", "x")) is False
    assert asyncio.run(wa.enviar_texto("5551999268735", "x", via="zapi")) is True
    assert wa.resumo("evolution")["envios_falha"] == 1
    assert wa.resumo("zapi")["envios_ok"] == 1


def test_evolution_sem_variaveis_nao_envia(wa, http_falso, monkeypatch):
    chamadas, _ = http_falso
    monkeypatch.setenv("WHATSAPP_PROVEDOR", "evolution")
    monkeypatch.delenv("EVOLUTION_URL")
    assert asyncio.run(wa.enviar_texto("5551999268735", "x")) is False
    assert chamadas == []


# ---------------- traducao do webhook da Evolution ----------------

def _evo_msg(message: dict, jid="5551999268735@s.whatsapp.net", from_me=False, **key_extra):
    return {"event": "messages.upsert", "instance": "vendas",
            "data": {"key": {"remoteJid": jid, "fromMe": from_me, "id": "ABC", **key_extra},
                     "pushName": "Ana", "message": message, "messageType": "conversation"}}


def test_texto_vira_formato_zapi(wa):
    d = asyncio.run(wa.evolution_para_zapi(_evo_msg({"conversation": "Oi, quanto custa?"})))
    assert d["type"] == "ReceivedCallback" and d["phone"] == "5551999268735"
    assert d["text"]["message"] == "Oi, quanto custa?"
    assert d["fromMe"] is False and d["isGroup"] is False
    d = asyncio.run(wa.evolution_para_zapi(_evo_msg({"extendedTextMessage": {"text": "com link"}})))
    assert d["text"]["message"] == "com link"


def test_numero_escondido_lid_usa_o_numero_ao_lado(wa):
    d = asyncio.run(wa.evolution_para_zapi(
        _evo_msg({"conversation": "oi"}, jid="12345@lid", remoteJidAlt="5511988887777@s.whatsapp.net")))
    assert d["phone"] == "5511988887777"


def test_grupo_e_marcado(wa):
    d = asyncio.run(wa.evolution_para_zapi(_evo_msg({"conversation": "oi"}, jid="1203@g.us")))
    assert d["isGroup"] is True


def test_audio_em_base64(wa):
    b64 = base64.b64encode(b"OGG").decode()
    d = asyncio.run(wa.evolution_para_zapi(_evo_msg({"audioMessage": {"mimetype": "audio/ogg"}, "base64": b64})))
    assert wa.b64_para_bytes(d["audio"]["base64"]) == b"OGG"


def test_audio_sem_base64_pede_para_a_evolution(wa, http_falso):
    chamadas, respostas = http_falso
    respostas["getBase64FromMediaMessage"] = (201, {"base64": base64.b64encode(b"PDF").decode()})
    d = asyncio.run(wa.evolution_para_zapi(
        _evo_msg({"documentMessage": {"fileName": "orcamento.pdf", "mimetype": "application/pdf"}})))
    assert wa.b64_para_bytes(d["document"]["base64"]) == b"PDF"
    assert d["document"]["fileName"] == "orcamento.pdf"
    assert "chat/getBase64FromMediaMessage/vendas" in str(chamadas[0].url)


# ---------------- medicao de quedas ----------------

def _no_instante(monkeypatch, t):
    monkeypatch.setattr(WA, "agora", lambda: t)


def test_queda_abre_e_fecha_com_minutos(wa, monkeypatch):
    t0 = datetime(2026, 12, 1, 12, 0, tzinfo=timezone.utc)
    _no_instante(monkeypatch, t0)
    wa.registrar_estado("evolution", wa.ESTADO_OK)
    _no_instante(monkeypatch, t0 + timedelta(hours=1))
    r = wa.registrar_estado("evolution", "caiu", origem="webhook")
    assert r["queda_aberta"]["motivo"] == "caiu"
    wa.registrar_estado("evolution", "reconectando", origem="webhook")   # continua a mesma queda
    _no_instante(monkeypatch, t0 + timedelta(hours=1, minutes=12))
    r = wa.registrar_estado("evolution", wa.ESTADO_OK)
    assert r["queda_fechada"]["minutos"] == 12.0
    _no_instante(monkeypatch, t0 + timedelta(hours=2))
    rs = wa.resumo("evolution")
    assert rs["quedas"] == 1 and rs["minutos_fora"] == 12.0 and rs["maior_queda_min"] == 12.0
    assert rs["disponibilidade"] == 90.0     # 12 de 120 minutos fora
    assert [e["estado"] for e in wa.ultimos_eventos()] == ["conectado", "reconectando", "caiu", "conectado"]


def test_primeiro_estado_desconectado_nao_conta_queda(wa):
    r = wa.registrar_estado("evolution", "pediu QR", origem="webhook")
    assert r["queda_aberta"] is None


def test_checagem_avisa_uma_vez_por_queda(wa, monkeypatch):
    estados = iter(["conectado", "caiu", "caiu", "conectado"])

    async def consultar(via):
        return next(estados) if via == "evolution" else "conectado"

    avisos = []

    async def avisar(texto, assunto):
        avisos.append(assunto)

    monkeypatch.setattr(WA, "consultar_conexao", consultar)
    for _ in range(4):
        asyncio.run(wa.checar_todos(avisar))
    assert avisos == ["WhatsApp Evolution caiu", "WhatsApp Evolution voltou"]


def test_consulta_de_conexao(wa, http_falso):
    _, respostas = http_falso
    respostas["connectionState"] = (200, {"instance": {"instanceName": "vendas", "state": "open"}})
    respostas["/status"] = (200, {"connected": False})
    assert asyncio.run(wa.consultar_conexao("evolution")) == "conectado"
    assert asyncio.run(wa.consultar_conexao("zapi")) == "caiu"
    respostas["connectionState"] = (502, {})
    assert asyncio.run(wa.consultar_conexao("evolution")) == "sem resposta"


# ---------------- webhook no main ----------------

@pytest.fixture
def m(monkeypatch, wa):
    monkeypatch.setenv("DEBOUNCE_SEGUNDOS", "0")
    falso = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis, "from_url", lambda *a, **k: falso)
    import main
    main = importlib.reload(main)
    main.agendados = []
    monkeypatch.setattr(main, "agendar_resposta", lambda tel, txt: main.agendados.append((tel, txt)))
    return main


def test_webhook_evolution_responde_quando_e_o_provedor(m, monkeypatch):
    monkeypatch.setenv("WHATSAPP_PROVEDOR", "evolution")
    resp = TestClient(m.app).post("/webhook", json=_evo_msg({"conversation": "Oi"})).json()
    assert resp["status"] == "ok"
    assert m.agendados == [("5551999268735", "Oi")]
    assert m.WA.resumo("evolution")["recebidas"] == 1


def test_webhook_evolution_so_mede_quando_zapi_responde(m):
    resp = TestClient(m.app).post("/webhook", json=_evo_msg({"conversation": "Oi"})).json()
    assert resp["status"] == "so_medicao"
    assert m.agendados == []
    assert m.WA.resumo("evolution")["recebidas"] == 1


def test_webhook_zapi_continua_igual(m):
    corpo = {"type": "ReceivedCallback", "phone": "5551999268735", "fromMe": False, "isGroup": False,
             "text": {"message": "Oi"}}
    assert TestClient(m.app).post("/webhook", json=corpo).json()["status"] == "ok"
    assert m.agendados == [("5551999268735", "Oi")]


def test_webhook_evolution_ignora_minhas_e_grupo(m, monkeypatch):
    monkeypatch.setenv("WHATSAPP_PROVEDOR", "evolution")
    c = TestClient(m.app)
    c.post("/webhook", json=_evo_msg({"conversation": "eu"}, from_me=True))
    c.post("/webhook", json=_evo_msg({"conversation": "grupo"}, jid="1@g.us"))
    assert m.agendados == []


def test_webhook_evolution_com_token(m, monkeypatch):
    monkeypatch.setenv("WHATSAPP_PROVEDOR", "evolution")
    monkeypatch.setenv("EVOLUTION_WEBHOOK_TOKEN", "segredo")
    c = TestClient(m.app)
    assert c.post("/webhook", json=_evo_msg({"conversation": "Oi"})).json()["status"] == "ignorado"
    assert c.post("/webhook?token=segredo", json=_evo_msg({"conversation": "Oi"})).json()["status"] == "ok"


def test_webhook_evolution_conexao_entra_na_medicao(m):
    c = TestClient(m.app)
    c.post("/webhook", json={"event": "connection.update", "data": {"instance": "vendas", "state": "open"}})
    c.post("/webhook", json={"event": "connection.update", "data": {"instance": "vendas", "state": "close"}})
    assert m.WA.estado_atual("evolution")["estado"] == "caiu"
    assert m.WA.resumo("evolution")["queda_aberta"] is not None


def test_painel_whatsapp_abre(m):
    m.WA.registrar_estado("evolution", "conectado")
    html = TestClient(m.app).get("/admin/whatsapp", auth=ADMIN).text
    assert "Quem responde agora: Z-API" in html and "Evolution" in html and "conectado" in html


def test_audio_da_evolution_e_transcrito_sem_baixar_link(m, monkeypatch):
    recebido = {}

    async def transcrever(url="", conteudo=None):
        recebido["url"], recebido["conteudo"] = url, conteudo
        return "[Audio transcrito]: oi"

    monkeypatch.setattr(m, "transcrever_audio", transcrever)
    dados = {"audio": {"base64": base64.b64encode(b"OGG").decode()}}
    assert asyncio.run(m.processar_midia(dados)) == "[Audio transcrito]: oi"
    assert recebido == {"url": "", "conteudo": b"OGG"}


def test_tela_de_qr(m, http_falso):
    _, respostas = http_falso
    respostas["connectionState"] = (200, {"instance": {"state": "connecting"}})
    respostas["instance/connect"] = (200, {"base64": "data:image/png;base64,AAA"})
    c = TestClient(m.app)
    assert "Mostrar o QR Code" in c.get("/admin/whatsapp", auth=ADMIN).text
    assert "Conectar o WhatsApp na Evolution" in c.get("/admin/whatsapp/conectar", auth=ADMIN).text
    assert c.get("/admin/whatsapp/qr", auth=ADMIN).json() == {"conectado": False, "qr": "data:image/png;base64,AAA",
                                                             "estado": "connecting"}
    respostas["connectionState"] = (200, {"instance": {"state": "open"}})
    assert c.get("/admin/whatsapp/qr", auth=ADMIN).json() == {"conectado": True}
    assert c.get("/admin/whatsapp/qr").status_code == 401


def test_zerar_medicao_so_do_provedor_escolhido(m):
    W = m.WA
    W.registrar_estado("evolution", "conectado")
    W.registrar_estado("evolution", "caiu")
    W.registrar_estado("zapi", "conectado")
    W.contar_recebida("evolution")
    W.contar_recebida("zapi")
    r = TestClient(m.app).post("/admin/whatsapp/zerar/evolution", auth=ADMIN, follow_redirects=False)
    assert r.status_code == 303
    rs = W.resumo("evolution")
    assert rs["quedas"] == 0 and rs["recebidas"] == 0 and rs["estado"] == {} and rs["queda_aberta"] is None
    assert [e["via"] for e in W.ultimos_eventos()] == ["zapi"]
    assert W.resumo("zapi")["recebidas"] == 1
    assert "Zerar medição da Evolution" in TestClient(m.app).get("/admin/whatsapp", auth=ADMIN).text
