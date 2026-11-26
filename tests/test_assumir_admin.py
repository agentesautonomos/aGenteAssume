"""
Admin da a gente assume assume uma conversa do assistente de vendas pelo painel:
o assistente para de responder e de mandar toque ate alguem devolver.
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

TEL = "5511998351187"
ADMIN = ("admin", "admin123")


@pytest.fixture
def m(monkeypatch):
    monkeypatch.setenv("DEBOUNCE_SEGUNDOS", "0")
    falso = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis, "from_url", lambda *a, **k: falso)
    import main
    main = importlib.reload(main)
    main.enviados, main.chamadas = [], []

    async def enviar_falso(tel, msg):
        main.enviados.append((tel, msg))
        return True

    async def claude_falso(tel, texto):
        main.chamadas.append(texto)
        main.salvar_mensagem(tel, "user", texto)
        main.salvar_mensagem(tel, "assistant", "resposta da IA")
        return "resposta da IA"

    monkeypatch.setattr(main, "enviar_whatsapp", enviar_falso)
    monkeypatch.setattr(main, "chamar_claude", claude_falso)
    monkeypatch.setattr(main, "fluxo_resposta", lambda *a, **k: None)
    return main


def _chega(m, texto, tel=TEL):
    m.r.rpush(f"{m.FILA_PREFIX}{tel}", texto)
    seq = m.r.incr(f"{m.FILA_SEQ_PREFIX}{tel}")
    asyncio.run(m._responder_apos_espera(tel, int(seq)))


def test_assumir_pelo_painel_cala_o_assistente(m):
    c = TestClient(m.app)
    _chega(m, "Oi")
    assert len(m.enviados) == 1
    resp = c.post(f"/admin/conversa/{TEL}/assumir", auth=ADMIN, follow_redirects=False)
    assert resp.status_code == 303
    assert m.em_humano(TEL)

    _chega(m, "Quanto custa?")
    assert len(m.enviados) == 1 and m.chamadas == ["Oi"]
    assert m.obter_historico(TEL)[-1] == {"role": "user", "content": "Quanto custa?"}

    pagina = c.get(f"/admin/conversa/{TEL}", auth=ADMIN).text
    assert "Devolver ao assistente" in pagina and "Com a equipe" in pagina
    assert "Com a equipe" in c.get("/admin", auth=ADMIN).text

    c.post(f"/admin/conversa/{TEL}/devolver", auth=ADMIN, follow_redirects=False)
    assert not m.em_humano(TEL)
    _chega(m, "E agora?")
    assert len(m.enviados) == 2
    assert "Assumir conversa" in c.get(f"/admin/conversa/{TEL}", auth=ADMIN).text


def test_assumido_vale_para_o_numero_sem_o_9(m):
    m.assumir_conversa(TEL)
    assert m.em_humano("551198351187")


def test_o_que_a_equipe_escreve_pelo_celular_entra_no_historico(m):
    m.assumir_conversa(TEL)
    corpo = {"type": "ReceivedCallback", "phone": TEL, "fromMe": True, "isGroup": False,
             "text": {"message": "Oi, aqui é o Marcio"}}
    TestClient(m.app).post("/webhook", json=corpo)
    assert m.obter_historico(TEL)[-1] == {"role": "assistant", "content": "Oi, aqui é o Marcio"}


def test_fora_do_assumido_minhas_mensagens_continuam_ignoradas(m):
    corpo = {"type": "ReceivedCallback", "phone": TEL, "fromMe": True, "isGroup": False,
             "text": {"message": "eco do assistente"}}
    TestClient(m.app).post("/webhook", json=corpo)
    assert m.obter_historico(TEL) == []


def test_nao_quero_com_a_equipe_bloqueia_sem_responder(m):
    m.r.set(f"{m.LEAD_PREFIX}{TEL}", '{"nome": "x", "telefone": "%s"}' % TEL)
    m.assumir_conversa(TEL)
    _chega(m, "não quero")
    assert m.esta_bloqueado(TEL) and m.enviados == []
