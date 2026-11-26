"""
Provedor de WhatsApp do numero principal (o do agente de vendas).

Hoje o numero principal fala pela Z-API. Este modulo deixa trocar para a Evolution API
(open source, roda num servidor nosso) so mudando variavel no Railway, sem mexer no codigo:

  WHATSAPP_PROVEDOR    "zapi" (padrao) ou "evolution". Quem ENVIA e quem tem as mensagens RESPONDIDAS.
  ZAPI_INSTANCE_ID / ZAPI_TOKEN / ZAPI_CLIENT_TOKEN   como antes.
  EVOLUTION_URL        endereco do servidor da Evolution (ex.: https://evolution-xxxx.up.railway.app)
  EVOLUTION_API_KEY    chave da Evolution (AUTHENTICATION_API_KEY do servidor dela)
  EVOLUTION_INSTANCE   nome da instancia (o "numero") criada na Evolution
  EVOLUTION_WEBHOOK_TOKEN  opcional: se preenchido, o webhook da Evolution precisa vir com ?token=<isso>
  WHATSAPP_CHECAGEM_MIN    de quantos em quantos minutos perguntar "esta conectado?" (padrao 5)

As clinicas (clinicas.py) continuam na Z-API, cada uma com a sua instancia. Nada aqui muda isso.

Medicao (o teste de 2-3 semanas): para cada provedor configurado, guardamos no Redis
  - cada mudanca de conexao (conectado / caiu / pediu QR / servidor sem resposta);
  - as quedas: inicio, fim e quantos minutos ficou fora;
  - envios que deram certo e que falharam, por dia;
  - mensagens recebidas, por dia (se o mesmo numero estiver nos dois, da para comparar se algum perdeu mensagem).
O painel /admin/whatsapp mostra tudo isso.
"""
import base64
import json
import os
from datetime import datetime, timedelta, timezone

import httpx

try:
    from zoneinfo import ZoneInfo
    TZ_SP = ZoneInfo("America/Sao_Paulo")
except Exception:  # pragma: no cover
    TZ_SP = timezone(timedelta(hours=-3))

R = None   # cliente Redis; o main.py liga com usar_redis(r)

PFX = "wa:"
MAX_EVENTOS = 1000
MAX_QUEDAS = 500
ESTADO_OK = "conectado"


def usar_redis(cliente):
    global R
    R = cliente


# ------------------------------------------------------------
# configuracao (lida na hora, para mudar no Railway sem codigo)
# ------------------------------------------------------------
def provedor() -> str:
    p = (os.environ.get("WHATSAPP_PROVEDOR") or "zapi").strip().lower()
    return "evolution" if p in ("evolution", "evo") else "zapi"


def _zapi_conf() -> tuple[str, str, str]:
    return (os.environ.get("ZAPI_INSTANCE_ID", ""), os.environ.get("ZAPI_TOKEN", ""),
            os.environ.get("ZAPI_CLIENT_TOKEN", ""))


def _evo_conf() -> tuple[str, str, str]:
    return ((os.environ.get("EVOLUTION_URL", "") or "").rstrip("/"), os.environ.get("EVOLUTION_API_KEY", ""),
            os.environ.get("EVOLUTION_INSTANCE", ""))


def zapi_configurada() -> bool:
    i, t, _ = _zapi_conf()
    return bool(i and t)


def evolution_configurada() -> bool:
    return all(_evo_conf())


def provedores_configurados() -> list[str]:
    return [p for p, ok in (("zapi", zapi_configurada()), ("evolution", evolution_configurada())) if ok]


def agora() -> datetime:
    return datetime.now(timezone.utc)


def normalizar_numero(telefone: str) -> str:
    """So digitos, com 55. Celular antigo sem o 9 (55 + DDD + 8 digitos) ganha o 9."""
    n = "".join(ch for ch in str(telefone) if ch.isdigit())
    if n.startswith("55") and len(n) == 12:
        n = n[:4] + "9" + n[4:]
    return n


# ------------------------------------------------------------
# envio
# ------------------------------------------------------------
async def _enviar_zapi(numero: str, mensagem: str) -> bool:
    inst, tok, ct = _zapi_conf()
    url = f"https://api.z-api.io/instances/{inst}/token/{tok}/send-text"
    async with httpx.AsyncClient(timeout=30) as http:
        resp = await http.post(url, headers={"Content-Type": "application/json", "Client-Token": ct},
                               json={"phone": numero, "message": mensagem})
    print(f"Z-API STATUS: {resp.status_code} | {resp.text[:300]}")
    return resp.status_code < 300


async def _enviar_evolution(numero: str, mensagem: str) -> bool:
    base, chave, inst = _evo_conf()
    async with httpx.AsyncClient(timeout=30) as http:
        resp = await http.post(f"{base}/message/sendText/{inst}",
                               headers={"Content-Type": "application/json", "apikey": chave},
                               json={"number": numero, "text": mensagem})
    print(f"EVOLUTION STATUS: {resp.status_code} | {resp.text[:300]}")
    return resp.status_code < 300


async def enviar_texto(telefone: str, mensagem: str, via: str | None = None) -> bool:
    """Envia texto pelo provedor ativo (ou pelo indicado em via). Conta acerto e falha para a medicao."""
    via = via or provedor()
    numero = normalizar_numero(telefone)
    print(f"ENVIANDO para {numero} via {via}")
    try:
        if via == "evolution":
            if not evolution_configurada():
                print("EVOLUTION nao configurada (EVOLUTION_URL, EVOLUTION_API_KEY, EVOLUTION_INSTANCE)")
                ok = False
            else:
                ok = await _enviar_evolution(numero, mensagem)
        else:
            ok = await _enviar_zapi(numero, mensagem)
    except Exception as e:
        print(f"ERRO ao enviar WhatsApp via {via}: {e}")
        ok = False
    _contar(via, "envio_ok" if ok else "envio_falha")
    return ok


# ------------------------------------------------------------
# webhook: tudo que chega vira o formato da Z-API, que o resto do codigo ja entende
# ------------------------------------------------------------
def eh_evolution(dados: dict) -> bool:
    return isinstance(dados, dict) and "event" in dados and "type" not in dados


def _evento_nome(dados: dict) -> str:
    return str(dados.get("event", "")).lower().replace("_", ".")


def _jid_para_numero(key: dict, data: dict) -> str:
    jid = key.get("remoteJid", "") or ""
    if jid.endswith("@lid"):   # WhatsApp esconde o numero; versoes novas da Evolution mandam o numero ao lado
        jid = key.get("remoteJidAlt") or key.get("senderPn") or data.get("senderPn") or ""
    return jid.split("@")[0].split(":")[0]


async def _evo_midia_base64(data: dict, msg: dict) -> str:
    """Base64 da midia: vem no webhook se WEBHOOK_BASE64=true na Evolution; senao, pedimos para ela."""
    if msg.get("base64"):
        return msg["base64"]
    if data.get("base64"):
        return data["base64"]
    base, chave, inst = _evo_conf()
    if not (base and chave and inst):
        return ""
    try:
        async with httpx.AsyncClient(timeout=60) as http:
            resp = await http.post(f"{base}/chat/getBase64FromMediaMessage/{inst}", headers={"apikey": chave},
                                   json={"message": {"key": data.get("key", {})}, "convertToMp4": False})
        if resp.status_code < 300:
            return resp.json().get("base64", "") or ""
        print(f"EVOLUTION midia {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        print(f"EVOLUTION midia erro: {e}")
    return ""


async def evolution_para_zapi(dados: dict, com_midia: bool = True) -> dict | None:
    """Traduz um 'messages.upsert' da Evolution para o formato ReceivedCallback da Z-API.
    Devolve None para o que nao e mensagem recebida (eventos de conexao sao tratados em registrar_webhook)."""
    if _evento_nome(dados) != "messages.upsert":
        return None
    data = dados.get("data") or {}
    if isinstance(data, list):          # algumas versoes mandam lista
        data = data[0] if data else {}
    key = data.get("key") or {}
    msg = data.get("message") or {}
    jid = key.get("remoteJid", "") or ""
    saida = {
        "type": "ReceivedCallback",
        "fromMe": bool(key.get("fromMe")),
        "isGroup": jid.endswith("@g.us"),
        "phone": _jid_para_numero(key, data),
        "messageId": key.get("id", ""),
        "senderName": data.get("pushName", ""),
        "_provedor": "evolution",
    }
    texto = (msg.get("conversation")
             or (msg.get("extendedTextMessage") or {}).get("text")
             or (msg.get("imageMessage") or {}).get("caption")
             or (msg.get("videoMessage") or {}).get("caption") or "")
    if texto:
        saida["text"] = {"message": texto}
    if not com_midia:
        return saida
    if msg.get("audioMessage"):
        saida["audio"] = {"base64": await _evo_midia_base64(data, msg),
                          "mimeType": msg["audioMessage"].get("mimetype", "audio/ogg")}
    doc = msg.get("documentMessage") or ((msg.get("documentWithCaptionMessage") or {}).get("message") or {}).get("documentMessage")
    if doc:
        saida["document"] = {"base64": await _evo_midia_base64(data, msg), "fileName": doc.get("fileName", ""),
                             "mimeType": doc.get("mimetype", "")}
    return saida


def b64_para_bytes(conteudo: str) -> bytes:
    if "," in conteudo[:100] and conteudo.startswith("data:"):
        conteudo = conteudo.split(",", 1)[1]
    return base64.b64decode(conteudo)


def registrar_webhook_evolution(dados: dict) -> None:
    """Eventos de conexao da Evolution entram na medicao."""
    ev = _evento_nome(dados)
    data = dados.get("data") or {}
    if ev == "connection.update" and isinstance(data, dict):
        estado = {"open": ESTADO_OK, "close": "caiu", "connecting": "reconectando"}.get(
            str(data.get("state", "")).lower(), str(data.get("state", "")) or "desconhecido")
        registrar_estado("evolution", estado, origem="webhook", detalhe=str(data.get("statusReason", "")))
    elif ev == "qrcode.updated":
        registrar_estado("evolution", "pediu QR", origem="webhook")


def contar_recebida(via: str) -> None:
    _contar(via, "recebida")


# ------------------------------------------------------------
# medicao: estado, quedas e contadores
# ------------------------------------------------------------
def _dia() -> str:
    return agora().astimezone(TZ_SP).strftime("%Y-%m-%d")


def _contar(via: str, campo: str) -> None:
    if R is None:
        return
    try:
        k = f"{PFX}contagem:{_dia()}"
        R.hincrby(k, f"{via}:{campo}", 1)
        R.expire(k, 60 * 60 * 24 * 120)
    except Exception as e:
        print(f"WA contagem erro: {e}")


def estado_atual(via: str) -> dict:
    if R is None:
        return {}
    bruto = R.get(f"{PFX}estado:{via}")
    return json.loads(bruto) if bruto else {}


def registrar_estado(via: str, estado: str, origem: str = "checagem", detalhe: str = "") -> dict:
    """Guarda o estado. Se mudou, vira evento; se saiu de 'conectado' abre uma queda; se voltou, fecha.
    Devolve {'mudou': bool, 'queda_aberta': dict|None, 'queda_fechada': dict|None}."""
    res = {"mudou": False, "queda_aberta": None, "queda_fechada": None}
    if R is None:
        return res
    t = agora().isoformat(timespec="seconds")
    atual = estado_atual(via)
    if origem == "checagem":
        R.set(f"{PFX}ultima_checagem:{via}", t)
    if not R.get(f"{PFX}desde:{via}"):
        R.set(f"{PFX}desde:{via}", t)
    if atual.get("estado") == estado:
        return res
    res["mudou"] = True
    R.set(f"{PFX}estado:{via}", json.dumps({"estado": estado, "desde": t}))
    R.lpush(f"{PFX}eventos", json.dumps({"quando": t, "via": via, "estado": estado, "origem": origem,
                                         "detalhe": detalhe[:200]}))
    R.ltrim(f"{PFX}eventos", 0, MAX_EVENTOS - 1)
    chave_q = f"{PFX}queda_aberta:{via}"
    aberta = R.get(chave_q)
    if estado != ESTADO_OK and not aberta and atual:      # so conta queda depois de ja ter visto conectado
        q = {"via": via, "inicio": t, "motivo": estado}
        R.set(chave_q, json.dumps(q))
        res["queda_aberta"] = q
    elif estado == ESTADO_OK and aberta:
        q = json.loads(aberta)
        q["fim"] = t
        q["minutos"] = round((datetime.fromisoformat(t) - datetime.fromisoformat(q["inicio"])).total_seconds() / 60, 1)
        R.lpush(f"{PFX}quedas", json.dumps(q))
        R.ltrim(f"{PFX}quedas", 0, MAX_QUEDAS - 1)
        R.delete(chave_q)
        res["queda_fechada"] = q
    return res


def marcar_queda_avisada(via: str) -> bool:
    """True na primeira vez (para avisar a equipe so uma vez por queda)."""
    if R is None:
        return False
    chave_q = f"{PFX}queda_aberta:{via}"
    bruto = R.get(chave_q)
    if not bruto:
        return False
    q = json.loads(bruto)
    if q.get("avisada"):
        return False
    q["avisada"] = True
    R.set(chave_q, json.dumps(q))
    return True


async def consultar_conexao(via: str) -> str:
    """Pergunta ao provedor se o numero esta conectado."""
    try:
        async with httpx.AsyncClient(timeout=20) as http:
            if via == "evolution":
                base, chave, inst = _evo_conf()
                resp = await http.get(f"{base}/instance/connectionState/{inst}", headers={"apikey": chave})
                if resp.status_code >= 300:
                    return "sem resposta"
                st = str(((resp.json() or {}).get("instance") or {}).get("state", "")).lower()
                return {"open": ESTADO_OK, "close": "caiu", "connecting": "reconectando"}.get(st, st or "desconhecido")
            inst, tok, ct = _zapi_conf()
            resp = await http.get(f"https://api.z-api.io/instances/{inst}/token/{tok}/status",
                                  headers={"Client-Token": ct})
            if resp.status_code >= 300:
                return "sem resposta"
            return ESTADO_OK if (resp.json() or {}).get("connected") else "caiu"
    except Exception as e:
        print(f"WA checagem {via}: {e}")
        return "sem resposta"


async def checar_todos(avisar=None) -> dict:
    """Checa cada provedor configurado. 'avisar' e uma funcao async(texto, assunto) para avisar a equipe."""
    saida = {}
    for via in provedores_configurados():
        estado = await consultar_conexao(via)
        res = registrar_estado(via, estado, origem="checagem")
        saida[via] = estado
        if avisar is None:
            continue
        nome = "Evolution" if via == "evolution" else "Z-API"
        if estado != ESTADO_OK and marcar_queda_avisada(via):
            await avisar(f"O WhatsApp principal na {nome} está fora: {estado}. "
                         f"Veja em /admin/whatsapp.", f"WhatsApp {nome} caiu")
        if res.get("queda_fechada") and res["queda_fechada"].get("avisada"):
            await avisar(f"O WhatsApp principal na {nome} voltou. Ficou fora {res['queda_fechada']['minutos']} min.",
                         f"WhatsApp {nome} voltou")
    return saida


def resumo(via: str, dias: int = 21) -> dict:
    """Numeros do teste para um provedor nos ultimos 'dias'."""
    if R is None:
        return {}
    fim = agora()
    desde_str = R.get(f"{PFX}desde:{via}")
    inicio = max(fim - timedelta(days=dias), datetime.fromisoformat(desde_str)) if desde_str else fim
    quedas = [q for q in (json.loads(x) for x in R.lrange(f"{PFX}quedas", 0, -1))
              if q.get("via") == via and datetime.fromisoformat(q["fim"]) >= inicio]
    aberta_b = R.get(f"{PFX}queda_aberta:{via}")
    aberta = json.loads(aberta_b) if aberta_b else None
    min_fora = sum(q.get("minutos", 0) for q in quedas)
    if aberta:
        min_fora += (fim - datetime.fromisoformat(aberta["inicio"])).total_seconds() / 60
    janela_min = max((fim - inicio).total_seconds() / 60, 0)
    envio_ok = envio_falha = recebidas = 0
    d = fim.astimezone(TZ_SP)
    for i in range(dias):
        h = R.hgetall(f"{PFX}contagem:{(d - timedelta(days=i)).strftime('%Y-%m-%d')}") or {}
        envio_ok += int(h.get(f"{via}:envio_ok", 0))
        envio_falha += int(h.get(f"{via}:envio_falha", 0))
        recebidas += int(h.get(f"{via}:recebida", 0))
    return {
        "via": via,
        "estado": estado_atual(via),
        "monitorando_desde": desde_str,
        "ultima_checagem": R.get(f"{PFX}ultima_checagem:{via}"),
        "quedas": len(quedas) + (1 if aberta else 0),
        "minutos_fora": round(min_fora, 1),
        "maior_queda_min": max([q.get("minutos", 0) for q in quedas] or [0]),
        "disponibilidade": round(100 * (1 - min_fora / janela_min), 2) if janela_min > 0 else None,
        "envios_ok": envio_ok,
        "envios_falha": envio_falha,
        "recebidas": recebidas,
        "queda_aberta": aberta,
        "lista_quedas": quedas[:30],
    }


async def qr_evolution() -> dict:
    """Estado da instancia e, se nao estiver conectada, o QR Code novo (imagem base64)."""
    base, chave, inst = _evo_conf()
    if not (base and chave and inst):
        return {"erro": "Evolution nao configurada"}
    try:
        async with httpx.AsyncClient(timeout=20) as http:
            st = await http.get(f"{base}/instance/connectionState/{inst}", headers={"apikey": chave})
            estado = str(((st.json() or {}).get("instance") or {}).get("state", "")).lower() if st.status_code < 300 else ""
            if estado == "open":
                return {"conectado": True}
            c = await http.get(f"{base}/instance/connect/{inst}", headers={"apikey": chave})
            d = c.json() if c.status_code < 300 else {}
            return {"conectado": False, "qr": d.get("base64", ""), "estado": estado or "sem resposta"}
    except Exception as e:
        return {"erro": str(e)}


def zerar(via: str) -> None:
    """Apaga a medicao de um provedor (estado, quedas, eventos e contagens) e recomeca do zero."""
    if R is None:
        return
    for k in ("estado", "desde", "ultima_checagem", "queda_aberta"):
        R.delete(f"{PFX}{k}:{via}")
    for lista in ("quedas", "eventos"):
        manter = [x for x in R.lrange(f"{PFX}{lista}", 0, -1) if json.loads(x).get("via") != via]
        R.delete(f"{PFX}{lista}")
        if manter:
            R.rpush(f"{PFX}{lista}", *manter)
    for k in R.scan_iter(f"{PFX}contagem:*"):
        campos = [c for c in R.hkeys(k) if c.startswith(f"{via}:")]
        if campos:
            R.hdel(k, *campos)


def ultimos_eventos(n: int = 50) -> list[dict]:
    if R is None:
        return []
    return [json.loads(x) for x in R.lrange(f"{PFX}eventos", 0, n - 1)]
