"""
Area da clinica — atendimento de pacientes por clinica, com login proprio.

Fica separado do main.py de proposito: o fluxo de prospeccao, o /webhook e o
/api/leads continuam exatamente como estao. O main.py so faz
`app.include_router(clinicas_router)`.

Cada clinica tem:
  - o proprio numero de WhatsApp (uma instancia Z-API so dela);
  - um webhook proprio: /webhook/clinica/{slug}/{chave};
  - os proprios usuarios (login por e-mail e senha; o login ja identifica a clinica);
  - os proprios dados no Redis, todos com o prefixo cl:{slug}:

Rotas:
  /entrar, /sair                    login da clinica
  /painel                           dashboard (conexao, contadores, fila, ultimas conversas)
  /painel/conversas                 todas as conversas
  /painel/conversa/{tel}            uma conversa, com assumir / devolver
  /painel/agendamentos              pedidos de agendamento que o assistente anotou
  /painel/configuracoes             profissionais, horarios, convenios -> prompt
  /painel/senha                     trocar a senha
  /admin/clinicas                   (admin da a gente assume) cadastrar e editar clinicas
  /webhook/clinica/{slug}/{chave}   webhook da Z-API de cada clinica
"""
import os
import re
import json
import html
import hmac
import time
import asyncio
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import httpx
import redis
from anthropic import Anthropic
from fastapi import APIRouter, Request, Depends, HTTPException, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

router = APIRouter()

R = redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379"), decode_responses=True)

BASE_URL      = os.environ.get("BASE_URL", "https://SEU-DOMINIO.up.railway.app").rstrip("/")
ADMIN_USER    = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS    = os.environ.get("ADMIN_PASS", "admin123")
MODELO        = os.environ.get("CLINICA_MODEL", os.environ.get("AGENT_MODEL", "claude-haiku-4-5-20251001"))
DEBOUNCE_SEG  = float(os.environ.get("CLINICA_DEBOUNCE_SEGUNDOS", "10"))
HIST_LIMITE   = 300         # mensagens guardadas por paciente
HIST_PROMPT   = 30          # mensagens que vao para a IA
SESSAO_DIAS   = 30
COOKIE        = "aga_sessao"
BR            = timezone(timedelta(hours=-3))   # Brasil sem horario de verao desde 2019

_claude = None


def claude() -> Anthropic:
    global _claude
    if _claude is None:
        _claude = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    return _claude


# ============================================================
# UTILITARIOS
# ============================================================
def agora() -> datetime:
    return datetime.now(BR)


def esc(v) -> str:
    return html.escape(str(v if v is not None else ""), quote=True)


def so_digitos(v) -> str:
    return re.sub(r"\D", "", str(v or ""))


def K(slug: str, *partes) -> str:
    return "cl:" + slug + (":" + ":".join(str(p) for p in partes) if partes else "")


def slugificar(txt: str) -> str:
    import unicodedata
    t = unicodedata.normalize("NFKD", txt or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", t).strip("-")[:40] or "clinica"


def tel_envio(tel: str) -> str:
    """Numero no formato da Z-API (com 55). Aceita '(51) 99999-8888' ou '5551999998888'."""
    d = so_digitos(tel)
    return "55" + d if len(d) in (10, 11) else d


def fmt_tel(tel: str) -> str:
    d = so_digitos(tel)
    if d.startswith("55") and len(d) in (12, 13):
        d = d[2:]
    if len(d) == 11:
        return f"({d[:2]}) {d[2:7]}-{d[7:]}"
    if len(d) == 10:
        return f"({d[:2]}) {d[2:6]}-{d[6:]}"
    return tel


def ha_quanto(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso)
    except Exception:
        return ""
    seg = (agora() - dt).total_seconds()
    if seg < 60:
        return "agora"
    if seg < 3600:
        return f"há {int(seg // 60)} min"
    if seg < 86400:
        return f"há {int(seg // 3600)}h"
    if seg < 2 * 86400:
        return "ontem, " + dt.strftime("%H:%M")
    return dt.strftime("%d/%m %H:%M")


# ============================================================
# CLINICAS
# ============================================================
def obter_clinica(slug: str) -> dict | None:
    v = R.get(K(slug, "cfg"))
    return json.loads(v) if v else None


def salvar_clinica(c: dict):
    R.set(K(c["slug"], "cfg"), json.dumps(c))
    R.sadd("cl:index", c["slug"])


def listar_clinicas() -> list:
    out = []
    for s in sorted(R.smembers("cl:index")):
        c = obter_clinica(s)
        if c:
            out.append(c)
    return out


def url_webhook(c: dict) -> str:
    return f"{BASE_URL}/webhook/clinica/{c['slug']}/{c['webhook_chave']}"


# ============================================================
# USUARIOS E SESSAO
# ============================================================
def hash_senha(senha: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", senha.encode(), salt.encode(), 200_000).hex()
    return f"pbkdf2${salt}${h}"


def conferir_senha(senha: str, guardado: str) -> bool:
    try:
        _, salt, _h = guardado.split("$")
    except ValueError:
        return False
    return hmac.compare_digest(hash_senha(senha, salt), guardado)


def obter_usuario(email: str) -> dict | None:
    v = R.get(f"cl:usuario:{email.strip().lower()}")
    return json.loads(v) if v else None


def salvar_usuario(u: dict):
    u["email"] = u["email"].strip().lower()
    R.set(f"cl:usuario:{u['email']}", json.dumps(u))
    R.sadd(K(u["slug"], "usuarios"), u["email"])


def usuarios_da_clinica(slug: str) -> list:
    return [u for u in (obter_usuario(e) for e in sorted(R.smembers(K(slug, "usuarios")))) if u]


def criar_sessao(email: str) -> str:
    token = secrets.token_urlsafe(32)
    R.set(f"cl:sessao:{token}", email, ex=SESSAO_DIAS * 86400)
    R.sadd(f"cl:sessoes:{email}", token)
    R.expire(f"cl:sessoes:{email}", SESSAO_DIAS * 86400)
    return token


def encerrar_sessoes(email: str):
    """Derruba todas as sessoes abertas de um usuario (ex.: depois de redefinir a senha)."""
    for t in R.smembers(f"cl:sessoes:{email}"):
        R.delete(f"cl:sessao:{t}")
    R.delete(f"cl:sessoes:{email}")


def sessao_atual(request: Request) -> dict | None:
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    email = R.get(f"cl:sessao:{token}")
    if not email:
        return None
    u = obter_usuario(email)
    if not u:
        return None
    c = obter_clinica(u["slug"])
    if not c or c.get("desativada"):
        return None
    return {"usuario": u, "clinica": c}


def situacao(c: dict) -> str:
    """ativa | pendente (criou a conta e ainda nao pagou) | bloqueada (assinatura cancelada ou pausada).
    Clinicas cadastradas pelo admin, sem assinatura, contam como ativas."""
    return c.get("situacao") or "ativa"


def exigir_login(request: Request) -> dict:
    s = sessao_atual(request)
    if not s:
        raise HTTPException(status_code=303, headers={"Location": "/entrar"})
    if situacao(s["clinica"]) != "ativa" and not request.url.path.startswith(("/assinar", "/painel/senha")):
        raise HTTPException(status_code=303, headers={"Location": "/assinar"})
    return s


_basic = HTTPBasic()


def exigir_admin(cred: HTTPBasicCredentials = Depends(_basic)) -> str:
    ok_u = secrets.compare_digest(cred.username.encode(), ADMIN_USER.encode())
    ok_p = secrets.compare_digest(cred.password.encode(), ADMIN_PASS.encode())
    if not (ok_u and ok_p):
        raise HTTPException(status_code=401, detail="Acesso negado", headers={"WWW-Authenticate": "Basic"})
    return cred.username


# ============================================================
# CONVERSAS, FILA, PEDIDOS, CONTADORES
# ============================================================
# estado da conversa:
#   assistente  -> o assistente responde
#   aguardando  -> encaminhada para a equipe; assistente pausado; ninguem assumiu ainda
#   humano      -> alguem da equipe assumiu; assistente pausado
def conv(slug: str, tel: str) -> dict:
    d = R.hgetall(K(slug, "conv", tel))
    d.setdefault("estado", "assistente")
    d["tel"] = tel
    return d


def contar(slug: str, campo: str, n: int = 1):
    chave = K(slug, "stats", agora().strftime("%Y-%m-%d"))
    R.hincrby(chave, campo, n)
    R.expire(chave, 400 * 86400)


def registrar_msg(slug: str, tel: str, papel: str, texto: str, nome: str = "", autor: str = ""):
    """papel: paciente | assistente | equipe"""
    agora_iso = agora().isoformat(timespec="seconds")
    item = {"papel": papel, "texto": texto, "em": agora_iso}
    if autor:
        item["autor"] = autor
    hk = K(slug, "hist", tel)
    R.rpush(hk, json.dumps(item, ensure_ascii=False))
    R.ltrim(hk, -HIST_LIMITE, -1)
    ck = K(slug, "conv", tel)
    campos = {"ultima_em": agora_iso, "ultima_txt": texto[:160], "ultima_papel": papel}
    if nome:
        campos["nome"] = nome
    if not R.hexists(ck, "primeira_em"):
        campos["primeira_em"] = agora_iso
    R.hset(ck, mapping=campos)
    R.zadd(K(slug, "convs"), {tel: time.time()})
    if papel == "paciente":
        dk = K(slug, "dia", agora().strftime("%Y-%m-%d"))
        R.sadd(dk, tel)
        R.expire(dk, 400 * 86400)
    elif papel == "assistente":
        contar(slug, "respostas")


def historico(slug: str, tel: str, n: int = HIST_LIMITE) -> list:
    return [json.loads(x) for x in R.lrange(K(slug, "hist", tel), -n, -1)]


def estatisticas(slug: str, dias: int) -> dict:
    hoje = agora().date()
    datas = [(hoje - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(dias)]
    pacientes = R.sunion([K(slug, "dia", d) for d in datas]) if datas else set()
    tot = {"conversas": len(pacientes), "respostas": 0, "agendamentos": 0, "encaminhados": 0}
    for d in datas:
        h = R.hgetall(K(slug, "stats", d))
        for campo in ("respostas", "agendamentos", "encaminhados"):
            tot[campo] += int(h.get(campo, 0))
    return tot


def encaminhar(slug: str, tel: str, motivo: str):
    R.hset(K(slug, "conv", tel), mapping={
        "estado": "aguardando", "motivo": motivo[:300],
        "encaminhado_em": agora().isoformat(timespec="seconds"), "assumido_por": "",
    })
    R.zadd(K(slug, "fila"), {tel: time.time()})
    contar(slug, "encaminhados")


def assumir(slug: str, tel: str, por: str):
    ck = K(slug, "conv", tel)
    mapa = {"estado": "humano", "assumido_por": por, "assumido_em": agora().isoformat(timespec="seconds")}
    if not R.hget(ck, "encaminhado_em"):
        mapa["encaminhado_em"] = mapa["assumido_em"]
    R.hset(ck, mapping=mapa)
    R.zadd(K(slug, "fila"), {tel: time.time()}, nx=True)


def devolver(slug: str, tel: str):
    R.hset(K(slug, "conv", tel), mapping={"estado": "assistente", "assumido_por": "", "motivo": ""})
    R.zrem(K(slug, "fila"), tel)


def criar_pedido(slug: str, tel: str, campos: dict) -> dict:
    pid = agora().strftime("%Y%m%d%H%M%S") + secrets.token_hex(2)
    p = {"id": pid, "tel": tel, "em": agora().isoformat(timespec="seconds"), "status": "novo", **campos}
    R.hset(K(slug, "pedidos"), pid, json.dumps(p, ensure_ascii=False))
    R.zadd(K(slug, "pedidos_z"), {pid: time.time()})
    contar(slug, "agendamentos")
    return p


def listar_pedidos(slug: str, n: int = 100) -> list:
    ids = R.zrevrange(K(slug, "pedidos_z"), 0, n - 1)
    if not ids:
        return []
    return [json.loads(v) for v in R.hmget(K(slug, "pedidos"), ids) if v]


# ============================================================
# Z-API (instancia de cada clinica)
# ============================================================
def _zapi(c: dict, caminho: str) -> tuple[str, dict]:
    url = f"https://api.z-api.io/instances/{c.get('zapi_instance', '')}/token/{c.get('zapi_token', '')}/{caminho}"
    return url, {"Content-Type": "application/json", "Client-Token": c.get("zapi_client_token", "")}


def zapi_configurada(c: dict) -> bool:
    return bool(c.get("zapi_instance") and c.get("zapi_token"))


async def enviar(c: dict, tel: str, texto: str) -> bool:
    if not zapi_configurada(c):
        print(f"CLINICA {c['slug']}: Z-API nao configurada, mensagem nao enviada")
        return False
    url, hd = _zapi(c, "send-text")
    try:
        async with httpx.AsyncClient(timeout=30) as http:
            resp = await http.post(url, headers=hd, json={"phone": tel_envio(tel), "message": texto})
            print(f"CLINICA {c['slug']} Z-API {resp.status_code}")
            return resp.status_code < 300
    except Exception as e:
        print(f"CLINICA {c['slug']} ERRO envio: {e}")
        return False


async def status_conexao(c: dict) -> dict:
    """{'estado': 'conectado'|'desconectado'|'sem_config'|'erro', 'detalhe': str}"""
    if not zapi_configurada(c):
        return {"estado": "sem_config", "detalhe": "A gente está preparando o número da sua clínica. "
                "Em até 24 horas o QR Code aparece aqui para você conectar o WhatsApp."}
    ck = K(c["slug"], "zapi_status")
    cache = R.get(ck)
    if cache:
        return json.loads(cache)
    url, hd = _zapi(c, "status")
    try:
        async with httpx.AsyncClient(timeout=8) as http:
            resp = await http.get(url, headers=hd)
        d = resp.json() if resp.status_code < 300 else {}
        if d.get("connected"):
            st = {"estado": "conectado", "detalhe": "WhatsApp conectado."}
            if d.get("smartphoneConnected") is False:
                st["detalhe"] = "Conectado, mas o celular da clínica está sem internet."
        elif resp.status_code < 300:
            st = {"estado": "desconectado", "detalhe": d.get("error") or "O WhatsApp da clínica caiu. É preciso ler o QR Code de novo."}
        else:
            st = {"estado": "erro", "detalhe": f"Não deu para consultar a conexão (código {resp.status_code})."}
    except Exception as e:
        st = {"estado": "erro", "detalhe": f"Não deu para consultar a conexão ({type(e).__name__})."}
    R.set(ck, json.dumps(st), ex=20)
    return st


async def configurar_webhook_zapi(c: dict) -> list:
    """Aponta o webhook 'ao receber' da instancia para esta clinica e liga 'notificar enviadas por mim'."""
    res = []
    async with httpx.AsyncClient(timeout=15) as http:
        url, hd = _zapi(c, "update-webhook-received")
        r1 = await http.put(url, headers=hd, json={"value": url_webhook(c)})
        res.append(f"webhook ao receber: {r1.status_code} {r1.text[:120]}")
        url, hd = _zapi(c, "update-notify-sent-by-me")
        r2 = await http.put(url, headers=hd, json={"notifySentByMe": True})
        res.append(f"notificar enviadas por mim: {r2.status_code} {r2.text[:120]}")
    return res


async def avisar_recepcao(c: dict, texto: str):
    tel = so_digitos(c.get("recepcao_tel", ""))
    if tel:
        await enviar(c, tel, texto)


# ============================================================
# CONFIGURACOES DA CLINICA -> PROMPT
# ============================================================
def obter_conf(slug: str) -> dict:
    v = R.get(K(slug, "conf"))
    d = json.loads(v) if v else {}
    d.setdefault("profissionais", [])
    return d


def salvar_conf(slug: str, conf: dict):
    R.set(K(slug, "conf"), json.dumps(conf, ensure_ascii=False))


def montar_prompt(c: dict, conf: dict) -> str:
    nome_cl = c.get("nome", "a clínica")
    nome_ass = conf.get("nome_assistente") or f"assistente da {nome_cl}"
    p = [
        f"Você é {nome_ass}, e atende pelo WhatsApp os pacientes da {nome_cl}.",
        "Fale em português do Brasil, com educação e calor humano, como uma boa recepcionista.",
        "No WhatsApp, seja breve: uma ou duas frases por mensagem, sem listas longas e sem emoji.",
        "Nunca diga que é um robô ou um bot. Se perguntarem, diga que é o assistente virtual da clínica.",
        "Nunca invente informação: preço, horário, convênio ou procedimento que não está abaixo. "
        "Se não souber, diga que vai passar para a equipe e encaminhe.",
        "Não dê diagnóstico nem orientação de tratamento. Em dor forte, sangramento ou urgência, encaminhe na hora.",
        "",
        "=== A CLÍNICA ===",
        f"Nome: {nome_cl}",
    ]
    for rot, ch in (("Endereço", "endereco"), ("Funcionamento", "funcionamento"),
                    ("Convênios aceitos pela clínica", "convenios"), ("Formas de pagamento", "pagamento")):
        if conf.get(ch):
            p.append(f"{rot}: {conf[ch]}")
    profs = [x for x in conf.get("profissionais", []) if x.get("nome")]
    if profs:
        p += ["", "=== PROFISSIONAIS ==="]
        for x in profs:
            p.append(f"- {x['nome']}")
            for rot, ch in (("Especialidades", "especialidades"), ("Dias e horários de atendimento", "horarios"),
                            ("Convênios", "convenios"), ("Observações", "obs")):
                if x.get(ch):
                    p.append(f"  {rot}: {x[ch]}")
    if conf.get("extra"):
        p += ["", "=== OUTRAS INFORMAÇÕES E REGRAS DA CLÍNICA ===", conf["extra"]]
    p += [
        "",
        "=== AGENDAMENTO ===",
        "Você ainda não tem acesso à agenda, então não confirme horário como certo. "
        "Descubra o nome do paciente, o profissional ou a especialidade, o dia e período que prefere e se é particular ou convênio (qual). "
        "Quando tiver isso, diga que vai passar o pedido para a recepção confirmar o horário, "
        "e inclua no fim da sua mensagem, numa linha separada, exatamente:",
        "[AGENDAMENTO: nome do paciente | profissional ou especialidade | dia e horário desejado | particular ou convênio | observação]",
        "Essa linha não aparece para o paciente.",
        "",
        "=== PASSAR PARA A EQUIPE ===",
        "Encaminhe para a equipe quando: o paciente pedir para falar com uma pessoa; houver urgência, dor forte ou reclamação; "
        "a pergunta for sobre algo que não está nas informações acima; ou for remarcar ou cancelar consulta.",
    ]
    if conf.get("encaminhar_quando"):
        p.append(f"Também encaminhe quando: {conf['encaminhar_quando']}")
    p += [
        "Para encaminhar, avise o paciente que alguém da equipe vai continuar a conversa por aqui, "
        "e inclua no fim da mensagem, numa linha separada, exatamente:",
        "[ENCAMINHAR: motivo em poucas palavras]",
        "Mensagens marcadas como [equipe da clínica] foram escritas por uma pessoa da equipe; siga o que ela combinou.",
    ]
    return "\n".join(p)


# ============================================================
# ATENDIMENTO (IA)
# ============================================================
RE_ENC = re.compile(r"\[\s*ENCAMINHAR\s*:?\s*([^\]]*)\]", re.I)
RE_AG  = re.compile(r"\[\s*AGENDAMENTO\s*:?\s*([^\]]*)\]", re.I)


def mensagens_para_ia(slug: str, tel: str) -> list:
    msgs = []
    for m in historico(slug, tel, HIST_PROMPT):
        if m["papel"] == "paciente":
            role, txt = "user", m["texto"]
        elif m["papel"] == "equipe":
            role, txt = "assistant", "[equipe da clínica] " + m["texto"]
        else:
            role, txt = "assistant", m["texto"]
        if msgs and msgs[-1]["role"] == role:
            msgs[-1]["content"] += "\n" + txt
        else:
            msgs.append({"role": role, "content": txt})
    while msgs and msgs[0]["role"] != "user":
        msgs.pop(0)
    return msgs


def interpretar_resposta(texto: str) -> tuple[str, str | None, dict | None]:
    """Tira as marcacoes do texto. Devolve (texto_limpo, motivo_encaminhar, pedido_agendamento)."""
    motivo = None
    pedido = None
    m = RE_ENC.search(texto)
    if m:
        motivo = m.group(1).strip() or "pedido do paciente"
    m = RE_AG.search(texto)
    if m:
        partes = [x.strip() for x in m.group(1).split("|")] + [""] * 5
        pedido = {"nome": partes[0], "profissional": partes[1], "quando": partes[2],
                  "convenio": partes[3], "obs": partes[4]}
    limpo = RE_AG.sub("", RE_ENC.sub("", texto)).strip()
    limpo = re.sub(r"\n{3,}", "\n\n", limpo)
    return limpo, motivo, pedido


async def responder_paciente(slug: str, tel: str):
    c = obter_clinica(slug)
    if not c or not c.get("bot_ativo") or c.get("desativada") or situacao(c) != "ativa":
        return
    info = conv(slug, tel)
    if info["estado"] != "assistente":
        return
    msgs = mensagens_para_ia(slug, tel)
    if not msgs or msgs[-1]["role"] != "user":
        return
    dt = agora()
    dias = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"]
    system = montar_prompt(c, obter_conf(slug)) + (
        f"\n\nAGORA: {dias[dt.weekday()]}, {dt.strftime('%d/%m/%Y %H:%M')} (horário de Brasília)."
        + (f"\nNome do paciente no WhatsApp: {info['nome']}" if info.get("nome") else "")
    )
    resp = await asyncio.to_thread(
        claude().messages.create, model=MODELO, max_tokens=600, system=system, messages=msgs)
    bruto = "".join(getattr(b, "text", "") for b in resp.content)
    try:
        uso = resp.usage
        R.hincrby(K(slug, "tokens", dt.strftime("%Y-%m")), "entrada", uso.input_tokens)
        R.hincrby(K(slug, "tokens", dt.strftime("%Y-%m")), "saida", uso.output_tokens)
    except Exception:
        pass
    texto, motivo, pedido = interpretar_resposta(bruto)

    # se a equipe assumiu enquanto a IA pensava, nao responde
    c = obter_clinica(slug)
    if not c.get("bot_ativo") or conv(slug, tel)["estado"] != "assistente":
        return
    if texto:
        registrar_msg(slug, tel, "assistente", texto)
        await enviar(c, tel, texto)
    nome = (pedido or {}).get("nome") or info.get("nome") or ""
    link = f"{BASE_URL}/painel/conversa/{tel}"
    if pedido:
        criar_pedido(slug, tel, pedido)
        await avisar_recepcao(c, (
            f"Pedido de agendamento pelo WhatsApp.\nPaciente: {nome or fmt_tel(tel)} ({fmt_tel(tel)})\n"
            f"Com: {pedido['profissional'] or '-'}\nQuando: {pedido['quando'] or '-'}\n"
            f"Convênio: {pedido['convenio'] or '-'}\nConfirme com o paciente: {link}"))
    if motivo:
        encaminhar(slug, tel, motivo)
        await avisar_recepcao(c, (
            f"Um paciente precisa da equipe.\nPaciente: {nome or fmt_tel(tel)} ({fmt_tel(tel)})\n"
            f"Motivo: {motivo}\nO assistente pausou nessa conversa. Veja e assuma: {link}"))


_tarefas: set = set()
_locks: dict = {}


def _lock(chave: str) -> asyncio.Lock:
    if chave not in _locks:
        _locks[chave] = asyncio.Lock()
    return _locks[chave]


def agendar_resposta(slug: str, tel: str):
    seq = R.incr(K(slug, "seq", tel))
    R.expire(K(slug, "seq", tel), 3600)
    t = asyncio.create_task(_responder_depois(slug, tel, int(seq)))
    _tarefas.add(t)
    t.add_done_callback(_tarefas.discard)


async def _responder_depois(slug: str, tel: str, seq: int):
    try:
        await asyncio.sleep(DEBOUNCE_SEG)
        if int(R.get(K(slug, "seq", tel)) or 0) != seq:
            return
        async with _lock(f"{slug}:{tel}"):
            await responder_paciente(slug, tel)
    except Exception as e:
        print(f"CLINICA {slug} ERRO ao responder {tel}: {e}")


@router.post("/webhook/clinica/{slug}/{chave}")
async def webhook_clinica(slug: str, chave: str, request: Request):
    c = obter_clinica(slug)
    if not c or not secrets.compare_digest(chave, c.get("webhook_chave", "")):
        raise HTTPException(status_code=404)
    try:
        d = await request.json()
    except Exception:
        return {"status": "ignorado"}
    if d.get("type") != "ReceivedCallback" or d.get("isGroup") or d.get("isNewsletter") or d.get("broadcast"):
        return {"status": "ignorado"}
    tel = so_digitos(d.get("phone", ""))
    if not tel or "@" in str(d.get("phone", "")):
        return {"status": "ignorado"}

    texto = (d.get("text") or {}).get("message", "")
    if not texto:
        try:
            from main import processar_midia   # audio (transcricao), pdf, planilha
            texto = await processar_midia(d) or ""
        except Exception as e:
            print(f"CLINICA {slug} midia: {e}")
        if not texto:
            for tipo, rot in (("image", "imagem"), ("audio", "áudio"), ("document", "documento"),
                              ("video", "vídeo"), ("sticker", "figurinha"), ("location", "localização")):
                if d.get(tipo):
                    texto = f"[{rot}]" + (f" {d[tipo].get('caption')}" if isinstance(d[tipo], dict) and d[tipo].get("caption") else "")
                    break
    if not texto:
        return {"status": "ignorado"}

    if d.get("fromMe"):
        if d.get("fromApi"):
            return {"status": "ignorado"}   # foi o proprio assistente que enviou
        # alguem da clinica respondeu pelo celular: registra e pausa o assistente nessa conversa
        registrar_msg(slug, tel, "equipe", texto, autor="celular da clínica")
        if conv(slug, tel)["estado"] != "humano":
            assumir(slug, tel, "celular da clínica")
        return {"status": "ok"}

    registrar_msg(slug, tel, "paciente", texto, nome=d.get("senderName") or d.get("chatName") or "")
    if c.get("bot_ativo") and conv(slug, tel)["estado"] == "assistente":
        agendar_resposta(slug, tel)
    return {"status": "ok"}


# ============================================================
# HTML
# ============================================================
CSS = """
:root{--tinta:#10203A;--lilas:#C7B8FF;--lilas2:#EDE8FF;--papel:#F4F6F9;--txt2:#4A5568;--linha:#D7DDE6;--branco:#fff;--alerta:#B42318;--alerta-bg:#FDECEA;--ok:#1F4E8C;--ok-bg:#E6EEF8}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Instrument Sans',system-ui,sans-serif;background:var(--papel);color:var(--tinta);font-size:15px;line-height:1.5}
a{color:var(--tinta)}
h1,h2,h3{font-family:'Bricolage Grotesque',system-ui,sans-serif;letter-spacing:-.01em}
.mono{font-family:'JetBrains Mono',ui-monospace,monospace}
header{background:var(--tinta);color:#fff;padding:14px 20px;display:flex;align-items:center;gap:16px;flex-wrap:wrap}
header .marca{font-family:'Bricolage Grotesque',sans-serif;font-weight:700;font-size:17px;display:flex;align-items:center;gap:8px}
header .marca i{width:12px;height:12px;border-radius:50%;background:var(--lilas);display:inline-block}
header .cl{color:#B9C3D3;font-size:14px}
header nav{display:flex;gap:4px;margin-left:auto;flex-wrap:wrap}
header nav a{color:#DDE3EC;text-decoration:none;padding:6px 10px;border-radius:8px;font-size:14px}
header nav a.ativo,header nav a:hover{background:rgba(255,255,255,.1);color:#fff}
main{max-width:980px;margin:24px auto;padding:0 16px}
.card{background:var(--branco);border:1px solid var(--linha);border-radius:14px;padding:18px 20px;margin-bottom:16px}
.card h2{font-size:17px;margin-bottom:12px}
.linha{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.status{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:700px){.status{grid-template-columns:1fr}}
.ponto{width:10px;height:10px;border-radius:50%;display:inline-block;flex:none}
.p-ok{background:#2E7DDB}.p-mal{background:var(--alerta)}.p-off{background:#A0AEC0}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px}
@media(max-width:700px){.stats{grid-template-columns:repeat(2,1fr)}}
.stat{background:var(--branco);border:1px solid var(--linha);border-radius:14px;padding:14px 16px}
.stat .n{font-family:'JetBrains Mono',monospace;font-size:28px;font-weight:600}
.stat .r{font-size:13px;color:var(--txt2)}
.periodo{display:flex;gap:6px;margin-bottom:12px}
.periodo a{font-size:13px;padding:4px 10px;border-radius:20px;border:1px solid var(--linha);text-decoration:none;background:#fff}
.periodo a.ativo{background:var(--tinta);color:#fff;border-color:var(--tinta)}
.item{display:flex;gap:12px;align-items:center;padding:12px 0;border-top:1px solid var(--linha)}
.item:first-of-type{border-top:none}
.item .q{flex:1;min-width:0}
.item .q b{display:block}
.item .q small{color:var(--txt2);display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.item .t{font-size:12px;color:var(--txt2);white-space:nowrap}
.tag{display:inline-block;font-size:12px;padding:2px 8px;border-radius:20px;background:var(--papel);color:var(--txt2);border:1px solid var(--linha);white-space:nowrap}
.tag.ag{background:var(--alerta-bg);color:var(--alerta);border-color:#F5C2BD}
.tag.hu{background:var(--lilas2);color:var(--tinta);border-color:var(--lilas)}
.tag.ok{background:var(--ok-bg);color:var(--ok);border-color:#C3D5EC}
.btn{display:inline-block;font:inherit;font-size:14px;padding:7px 14px;border-radius:10px;border:1px solid var(--tinta);background:var(--tinta);color:#fff;text-decoration:none;cursor:pointer;white-space:nowrap}
.btn.sec{background:#fff;color:var(--tinta);border-color:var(--linha)}
.btn.lil{background:var(--lilas);color:var(--tinta);border-color:var(--lilas)}
.btn.peq{font-size:13px;padding:5px 10px}
form.in{display:inline}
.vazio{color:var(--txt2);font-size:14px;padding:8px 0}
.aviso{padding:10px 14px;border-radius:10px;margin-bottom:16px;font-size:14px;background:var(--ok-bg);color:var(--ok)}
.aviso.erro{background:var(--alerta-bg);color:var(--alerta)}
label{display:block;font-size:13px;color:var(--txt2);margin:10px 0 4px}
input[type=text],input[type=email],input[type=password],input[type=tel],textarea,select{width:100%;font:inherit;padding:9px 11px;border:1px solid var(--linha);border-radius:10px;background:#fff;color:var(--tinta)}
textarea{min-height:70px;resize:vertical}
input:focus,textarea:focus{outline:2px solid var(--lilas);border-color:var(--lilas)}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:0 14px}
@media(max-width:700px){.grid2{grid-template-columns:1fr}}
.prof{border:1px solid var(--linha);border-radius:12px;padding:6px 14px 14px;margin-bottom:12px;background:var(--papel)}
.chat{display:flex;flex-direction:column;gap:8px}
.msg{max-width:78%;padding:8px 12px;border-radius:14px;font-size:14px;white-space:pre-wrap;word-wrap:break-word}
.msg small{display:block;font-size:11px;color:var(--txt2);margin-top:2px}
.msg.paciente{align-self:flex-start;background:#fff;border:1px solid var(--linha);border-bottom-left-radius:4px}
.msg.assistente{align-self:flex-end;background:var(--lilas2);border-bottom-right-radius:4px}
.msg.equipe{align-self:flex-end;background:var(--tinta);color:#fff;border-bottom-right-radius:4px}
.msg.equipe small{color:#B9C3D3}
details summary{cursor:pointer;color:var(--txt2);font-size:14px}
pre{white-space:pre-wrap;font-size:12px;background:var(--papel);padding:12px;border-radius:10px;margin-top:8px}
.login{max-width:380px;margin:10vh auto;padding:0 16px}
.login h1{font-size:28px;margin-bottom:4px}
table{width:100%;border-collapse:collapse;font-size:14px}
td,th{padding:8px 6px;border-top:1px solid var(--linha);text-align:left;vertical-align:top}
"""

FONTES = ('<link rel="preconnect" href="https://fonts.googleapis.com">'
          '<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:wght@600;700'
          '&family=Instrument+Sans:wght@400;600&family=JetBrains+Mono:wght@500;600&display=swap" rel="stylesheet">')


def pagina(titulo: str, corpo: str, s: dict | None = None, ativa: str = "", extra_head: str = "") -> str:
    cab = ""
    if s:
        itens = [("painel", "Painel", "/painel"), ("conversas", "Conversas", "/painel/conversas"),
                 ("agendamentos", "Agendamentos", "/painel/agendamentos"),
                 ("config", "Configurações", "/painel/configuracoes"), ("sair", "Sair", "/sair")]
        nav = "".join(f'<a href="{u}" class="{"ativo" if k == ativa else ""}">{t}</a>' for k, t, u in itens)
        cab = (f'<header><span class="marca"><i></i>a gente assume</span>'
               f'<span class="cl">{esc(s["clinica"]["nome"])}</span><nav>{nav}</nav></header>')
    return (f'<!DOCTYPE html><html lang="pt-br"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{esc(titulo)} · a gente assume</title>{FONTES}<style>{CSS}</style>{extra_head}</head>'
            f'<body>{cab}<main>{corpo}</main></body></html>')


def html_resp(conteudo: str) -> HTMLResponse:
    return HTMLResponse(conteudo, headers={"Cache-Control": "no-store"})


def tag_estado(info: dict) -> str:
    e = info.get("estado", "assistente")
    if e == "aguardando":
        return '<span class="tag ag">aguardando a equipe</span>'
    if e == "humano":
        quem = info.get("assumido_por") or "equipe"
        return f'<span class="tag hu">com {esc(quem)}</span>'
    return '<span class="tag">assistente</span>'


def nome_ou_tel(info: dict) -> str:
    return esc(info.get("nome") or fmt_tel(info["tel"]))


# ============================================================
# LOGIN
# ============================================================
@router.get("/entrar", response_class=HTMLResponse)
def entrar_get(request: Request, erro: str = ""):
    if sessao_atual(request):
        return RedirectResponse("/painel", status_code=303)
    aviso = f'<div class="aviso erro">{esc(erro)}</div>' if erro else ""
    corpo = f"""<div class="login">
      <p class="mono" style="font-size:12px;color:var(--txt2);margin-bottom:8px">a gente assume</p>
      <h1>Entrar</h1>
      <p style="color:var(--txt2);margin-bottom:18px">Acompanhe o atendimento da sua clínica.</p>
      {aviso}
      <form method="post" class="card">
        <label>E-mail</label><input type="email" name="email" required autofocus autocomplete="username">
        <label>Senha</label><input type="password" name="senha" required autocomplete="current-password">
        <div style="margin-top:16px"><button class="btn" style="width:100%">Entrar</button></div>
      </form>
      <p style="font-size:14px;display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap">
        <a href="/esqueci">Esqueci a senha</a><a href="/criar-conta"><b>Criar conta</b></a></p>
    </div>"""
    return html_resp(pagina("Entrar", corpo))


@router.post("/entrar")
def entrar_post(request: Request, email: str = Form(""), senha: str = Form("")):
    email = email.strip().lower()
    ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "").split(",")[0].strip()
    tk = f"cl:tentativas:{email}:{ip}"
    if int(R.get(tk) or 0) >= 10:
        return RedirectResponse("/entrar?erro=Muitas+tentativas.+Espere+15+minutos.", status_code=303)
    u = obter_usuario(email)
    c = obter_clinica(u["slug"]) if u else None
    if not u or not c or c.get("desativada") or not conferir_senha(senha, u.get("hash", "")):
        R.incr(tk)
        R.expire(tk, 900)
        return RedirectResponse("/entrar?erro=E-mail+ou+senha+incorretos.", status_code=303)
    R.delete(tk)
    resp = RedirectResponse("/painel", status_code=303)
    resp.set_cookie(COOKIE, criar_sessao(email), max_age=SESSAO_DIAS * 86400, httponly=True,
                    secure=BASE_URL.startswith("https"), samesite="lax")
    return resp


@router.get("/sair")
def sair(request: Request):
    token = request.cookies.get(COOKIE)
    if token:
        R.delete(f"cl:sessao:{token}")
    resp = RedirectResponse("/entrar", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp


# ============================================================
# PAINEL
# ============================================================
def html_fila(slug: str) -> str:
    tels = R.zrange(K(slug, "fila"), 0, -1)
    if not tels:
        return '<div class="vazio">Ninguém esperando a equipe agora.</div>'
    out = []
    for tel in tels:
        info = conv(slug, tel)
        if info["estado"] == "assistente":
            R.zrem(K(slug, "fila"), tel)
            continue
        acoes = ""
        if info["estado"] == "aguardando":
            acoes += (f'<form class="in" method="post" action="/painel/conversa/{tel}/assumir">'
                      f'<button class="btn peq">Assumir conversa</button></form> ')
        acoes += (f'<form class="in" method="post" action="/painel/conversa/{tel}/devolver">'
                  f'<button class="btn sec peq">Devolver ao assistente</button></form>')
        out.append(f"""<div class="item">
          <div class="q"><b><a href="/painel/conversa/{tel}">{nome_ou_tel(info)}</a> {tag_estado(info)}</b>
            <small>{esc(info.get('motivo') or info.get('ultima_txt', ''))}</small></div>
          <span class="t">{ha_quanto(info.get('encaminhado_em', ''))}</span>{acoes}</div>""")
    return "".join(out) or '<div class="vazio">Ninguém esperando a equipe agora.</div>'


def html_lista_conversas(slug: str, ini: int, fim: int) -> str:
    tels = R.zrevrange(K(slug, "convs"), ini, fim)
    if not tels:
        return '<div class="vazio">Nenhuma conversa ainda. Assim que um paciente mandar mensagem, ela aparece aqui.</div>'
    out = []
    for tel in tels:
        info = conv(slug, tel)
        quem = {"assistente": "Assistente: ", "equipe": "Equipe: "}.get(info.get("ultima_papel"), "")
        out.append(f"""<div class="item">
          <div class="q"><b><a href="/painel/conversa/{tel}">{nome_ou_tel(info)}</a></b>
            <small>{esc(quem + info.get('ultima_txt', ''))}</small></div>
          {tag_estado(info)}<span class="t">{ha_quanto(info.get('ultima_em', ''))}</span></div>""")
    return "".join(out)


@router.get("/painel", response_class=HTMLResponse)
async def painel(request: Request, p: int = 7, msg: str = "", s: dict = Depends(exigir_login)):
    c = await reverificar_assinatura(s["clinica"])
    if situacao(c) != "ativa":
        return RedirectResponse("/assinar", status_code=303)
    slug = c["slug"]
    p = p if p in (1, 7, 30) else 7
    st = await status_conexao(c)
    est = estatisticas(slug, p)
    ponto = {"conectado": "p-ok", "desconectado": "p-mal", "erro": "p-mal"}.get(st["estado"], "p-off")
    titulo_con = {"conectado": "Conectado", "desconectado": "Desconectado",
                  "erro": "Sem resposta", "sem_config": "Em preparação"}[st["estado"]]
    ligado = bool(c.get("bot_ativo"))
    aviso = f'<div class="aviso">{esc(msg)}</div>' if msg else ""
    periodos = "".join(f'<a href="/painel?p={d}" class="{"ativo" if d == p else ""}">{t}</a>'
                       for d, t in ((1, "Hoje"), (7, "7 dias"), (30, "30 dias")))
    n_fila = R.zcard(K(slug, "fila"))
    corpo = f"""{aviso}
    <div class="status">
      <div class="card"><h2>WhatsApp da clínica</h2>
        <div class="linha"><span class="ponto {ponto}"></span><b>{titulo_con}</b></div>
        <p style="color:var(--txt2);font-size:14px;margin-top:6px">{esc(st['detalhe'])}</p>
        {'<a class="btn lil" style="margin-top:10px" href="/painel/whatsapp">Conectar o WhatsApp</a>' if st['estado'] == 'desconectado' else ''}</div>
      <div class="card"><h2>Assistente</h2>
        <div class="linha"><span class="ponto {'p-ok' if ligado else 'p-off'}"></span>
          <b>{'Ligado, respondendo os pacientes' if ligado else 'Desligado, ninguém está respondendo'}</b></div>
        <form method="post" action="/painel/assistente" style="margin-top:10px">
          <input type="hidden" name="ligar" value="{'0' if ligado else '1'}">
          <button class="btn {'sec' if ligado else 'lil'}">{'Desligar o assistente' if ligado else 'Ligar o assistente'}</button>
        </form></div>
    </div>
    <div class="periodo">{periodos}</div>
    <div class="stats">
      <div class="stat"><div class="n">{est['conversas']}</div><div class="r">pacientes atendidos</div></div>
      <div class="stat"><div class="n">{est['respostas']}</div><div class="r">respostas do assistente</div></div>
      <div class="stat"><div class="n">{est['agendamentos']}</div><div class="r">pedidos de agendamento</div></div>
      <div class="stat"><div class="n">{est['encaminhados']}</div><div class="r">passados para a equipe</div></div>
    </div>
    <div class="card"><h2>Esperando a equipe <span class="tag {'ag' if n_fila else ''}">{n_fila}</span></h2>{html_fila(slug)}</div>
    <div class="card"><h2>Últimas conversas</h2>{html_lista_conversas(slug, 0, 9)}
      <p style="margin-top:10px"><a href="/painel/conversas">Ver todas</a></p></div>"""
    return html_resp(pagina("Painel", corpo, s, "painel", '<meta http-equiv="refresh" content="60">'))


@router.post("/painel/assistente")
def painel_assistente(ligar: str = Form("0"), s: dict = Depends(exigir_login)):
    c = obter_clinica(s["clinica"]["slug"])
    c["bot_ativo"] = ligar == "1"
    salvar_clinica(c)
    m = "Assistente ligado." if c["bot_ativo"] else "Assistente desligado. As mensagens continuam chegando aqui, mas ninguém responde automaticamente."
    return RedirectResponse(f"/painel?msg={m}", status_code=303)


@router.get("/painel/conversas", response_class=HTMLResponse)
def painel_conversas(pg: int = 1, s: dict = Depends(exigir_login)):
    slug = s["clinica"]["slug"]
    pg = max(1, pg)
    total = R.zcard(K(slug, "convs"))
    por = 50
    nav = ""
    if pg > 1:
        nav += f'<a class="btn sec peq" href="?pg={pg - 1}">Anteriores</a> '
    if pg * por < total:
        nav += f'<a class="btn sec peq" href="?pg={pg + 1}">Mais antigas</a>'
    corpo = (f'<div class="card"><h2>Conversas <span class="tag">{total}</span></h2>'
             f'{html_lista_conversas(slug, (pg - 1) * por, pg * por - 1)}<p style="margin-top:10px">{nav}</p></div>')
    return html_resp(pagina("Conversas", corpo, s, "conversas"))


@router.get("/painel/conversa/{tel}", response_class=HTMLResponse)
def painel_conversa(tel: str, s: dict = Depends(exigir_login)):
    slug = s["clinica"]["slug"]
    tel = so_digitos(tel)
    if not R.exists(K(slug, "conv", tel)):
        raise HTTPException(status_code=404)
    info = conv(slug, tel)
    bolhas = []
    for m in historico(slug, tel):
        quem = {"paciente": info.get("nome") or "Paciente", "assistente": "Assistente",
                "equipe": m.get("autor") or "Equipe"}.get(m["papel"], m["papel"])
        try:
            quando = datetime.fromisoformat(m["em"]).strftime("%d/%m %H:%M")
        except Exception:
            quando = ""
        bolhas.append(f'<div class="msg {esc(m["papel"])}">{esc(m["texto"])}<small>{esc(quem)} · {quando}</small></div>')
    e = info["estado"]
    acoes = ""
    if e != "humano":
        acoes += (f'<form class="in" method="post" action="/painel/conversa/{tel}/assumir">'
                  f'<button class="btn">Assumir conversa</button></form> ')
    if e != "assistente":
        acoes += (f'<form class="in" method="post" action="/painel/conversa/{tel}/devolver">'
                  f'<button class="btn sec">Devolver ao assistente</button></form> ')
    acoes += f'<a class="btn sec" href="https://wa.me/{tel}" target="_blank" rel="noopener">Abrir no WhatsApp</a>'
    explica = {
        "assistente": "O assistente está respondendo esta conversa.",
        "aguardando": f"O assistente passou para a equipe ({esc(info.get('motivo') or 'sem motivo')}) e está pausado aqui. "
                      "Assuma e responda pelo WhatsApp da clínica.",
        "humano": f"Com {esc(info.get('assumido_por') or 'a equipe')}. O assistente não responde até alguém devolver a conversa.",
    }[e]
    corpo = f"""<p style="margin-bottom:12px"><a href="/painel">Voltar ao painel</a></p>
    <div class="card"><div class="linha"><h2 style="margin:0">{nome_ou_tel(info)}</h2>
      <span class="mono" style="color:var(--txt2);font-size:13px">{fmt_tel(tel)}</span>{tag_estado(info)}</div>
      <p style="color:var(--txt2);font-size:14px;margin:8px 0 12px">{explica}</p>
      <div class="linha">{acoes}</div></div>
    <div class="card"><div class="chat">{''.join(bolhas) or '<div class="vazio">Sem mensagens.</div>'}</div></div>"""
    return html_resp(pagina("Conversa", corpo, s, "conversas", '<meta http-equiv="refresh" content="30">'))


@router.post("/painel/conversa/{tel}/assumir")
def painel_assumir(tel: str, s: dict = Depends(exigir_login)):
    slug = s["clinica"]["slug"]
    tel = so_digitos(tel)
    if R.exists(K(slug, "conv", tel)):
        assumir(slug, tel, s["usuario"].get("nome") or s["usuario"]["email"])
    return RedirectResponse(f"/painel/conversa/{tel}", status_code=303)


@router.post("/painel/conversa/{tel}/devolver")
def painel_devolver(tel: str, s: dict = Depends(exigir_login)):
    slug = s["clinica"]["slug"]
    tel = so_digitos(tel)
    if R.exists(K(slug, "conv", tel)):
        devolver(slug, tel)
    return RedirectResponse(f"/painel/conversa/{tel}", status_code=303)


@router.get("/painel/agendamentos", response_class=HTMLResponse)
def painel_agendamentos(s: dict = Depends(exigir_login)):
    slug = s["clinica"]["slug"]
    linhas = []
    for p in listar_pedidos(slug):
        st = p.get("status", "novo")
        tag = {"novo": '<span class="tag ag">a confirmar</span>', "confirmado": '<span class="tag ok">confirmado</span>',
               "cancelado": '<span class="tag">não marcou</span>'}.get(st, esc(st))
        acoes = ""
        if st == "novo":
            acoes = (f'<form class="in" method="post" action="/painel/agendamentos/{esc(p["id"])}">'
                     f'<input type="hidden" name="status" value="confirmado"><button class="btn peq">Confirmado</button></form> '
                     f'<form class="in" method="post" action="/painel/agendamentos/{esc(p["id"])}">'
                     f'<input type="hidden" name="status" value="cancelado"><button class="btn sec peq">Não marcou</button></form>')
        linhas.append(f"""<tr><td><a href="/painel/conversa/{esc(p['tel'])}">{esc(p.get('nome') or fmt_tel(p['tel']))}</a>
          <br><span class="mono" style="font-size:12px;color:var(--txt2)">{fmt_tel(p['tel'])}</span></td>
          <td>{esc(p.get('profissional'))}</td><td>{esc(p.get('quando'))}</td><td>{esc(p.get('convenio'))}
          {('<br><small>' + esc(p['obs']) + '</small>') if p.get('obs') else ''}</td>
          <td>{tag}<br><small style="color:var(--txt2)">{ha_quanto(p['em'])}</small></td><td>{acoes}</td></tr>""")
    tabela = ("<table><tr><th>Paciente</th><th>Com</th><th>Quando</th><th>Convênio</th><th>Situação</th><th></th></tr>"
              + "".join(linhas) + "</table>") if linhas else '<div class="vazio">Nenhum pedido ainda.</div>'
    corpo = f"""<div class="card"><h2>Pedidos de agendamento</h2>
      <p style="color:var(--txt2);font-size:14px;margin-bottom:10px">O assistente anota o que o paciente quer e avisa a recepção.
      Marque o horário na agenda da clínica, confirme com o paciente e registre aqui.</p>{tabela}</div>"""
    return html_resp(pagina("Agendamentos", corpo, s, "agendamentos"))


@router.post("/painel/agendamentos/{pid}")
def painel_agendamento_status(pid: str, status: str = Form(""), s: dict = Depends(exigir_login)):
    slug = s["clinica"]["slug"]
    v = R.hget(K(slug, "pedidos"), pid)
    if v and status in ("confirmado", "cancelado", "novo"):
        p = json.loads(v)
        p["status"] = status
        p["atualizado_por"] = s["usuario"].get("nome") or s["usuario"]["email"]
        R.hset(K(slug, "pedidos"), pid, json.dumps(p, ensure_ascii=False))
    return RedirectResponse("/painel/agendamentos", status_code=303)


# ============================================================
# CONFIGURACOES
# ============================================================
PROF_CAMPOS = (("nome", "Nome", "Dra. Ana Souza"),
               ("especialidades", "Especialidades", "Ortodontia, clareamento"),
               ("horarios", "Dias e horários", "Seg e qua 8h–12h; sex 14h–18h"),
               ("convenios", "Convênios que atende", "Unimed Odonto, Amil Dental, particular"),
               ("obs", "Observações", "Não atende criança"))


def html_prof(i: int, x: dict) -> str:
    campos = "".join(
        f'<div><label>{rot}</label><input type="text" name="prof_{ch}_{i}" value="{esc(x.get(ch, ""))}" placeholder="{esc(ex)}"></div>'
        for ch, rot, ex in PROF_CAMPOS)
    remover = (f'<label style="margin-top:10px"><input type="checkbox" name="prof_remover_{i}" value="1"> Remover este profissional</label>'
               if x.get("nome") else '<p style="font-size:13px;color:var(--txt2);margin-top:10px">Preencha para adicionar um profissional.</p>')
    return f'<div class="prof"><div class="grid2">{campos}</div>{remover}</div>'


@router.get("/painel/configuracoes", response_class=HTMLResponse)
def painel_config(salvo: str = "", s: dict = Depends(exigir_login)):
    c = s["clinica"]
    conf = obter_conf(c["slug"])
    profs = [x for x in conf["profissionais"] if x.get("nome")]
    profs_html = "".join(html_prof(i, x) for i, x in enumerate(profs)) + html_prof(len(profs), {})

    def campo(ch, rot, ex, area=False):
        if area:
            return f'<label>{rot}</label><textarea name="{ch}" placeholder="{esc(ex)}">{esc(conf.get(ch, ""))}</textarea>'
        return f'<label>{rot}</label><input type="text" name="{ch}" value="{esc(conf.get(ch, ""))}" placeholder="{esc(ex)}">'

    aviso = '<div class="aviso">Salvo. O assistente já usa as novas informações na próxima mensagem.</div>' if salvo else ""
    corpo = f"""{aviso}<form method="post">
    <div class="card"><h2>A clínica</h2>
      <p style="color:var(--txt2);font-size:14px">Tudo o que estiver aqui o assistente usa para responder. O que não estiver, ele passa para a equipe.</p>
      <div class="grid2">
        <div>{campo('nome_assistente', 'Como o assistente se apresenta', 'Assistente da ' + c['nome'])}</div>
        <div>{campo('endereco', 'Endereço', 'Rua X, 123, sala 4, Porto Alegre')}</div>
        <div>{campo('funcionamento', 'Horário de funcionamento', 'Seg a sex 8h–19h; sáb 8h–12h')}</div>
        <div>{campo('pagamento', 'Formas de pagamento', 'Pix, cartão em até 6x')}</div>
      </div>
      {campo('convenios', 'Convênios aceitos pela clínica', 'Unimed Odonto, Amil Dental, OdontoPrev', True)}
    </div>
    <div class="card"><h2>Profissionais</h2>{profs_html}
      <p style="font-size:13px;color:var(--txt2)">Para incluir mais de um de cada vez, salve e preencha o novo espaço que aparece.</p></div>
    <div class="card"><h2>Regras</h2>
      {campo('encaminhar_quando', 'Quando passar para a equipe, além do padrão', 'Pedido de orçamento de implante; paciente pede a Dra. Ana direto', True)}
      {campo('extra', 'Outras informações e regras', 'Primeira consulta de avaliação é gratuita. Não informar preços de tratamento pelo WhatsApp.', True)}
      <p style="font-size:13px;color:var(--txt2);margin-top:8px">Por padrão o assistente já passa para a equipe quando o paciente pede uma pessoa,
      em urgência ou dor forte, reclamação, remarcação, cancelamento ou pergunta que não está aqui.</p>
    </div>
    <div class="linha" style="margin-bottom:16px"><button class="btn">Salvar</button></div>
    </form>
    <div class="card"><details><summary>Ver o texto que o assistente recebe</summary><pre>{esc(montar_prompt(c, conf))}</pre></details></div>
    <div class="card"><h2>Acesso</h2><p style="font-size:14px">Você entrou como <b>{esc(s['usuario']['email'])}</b>.
      <a href="/painel/senha">Trocar senha</a></p></div>"""
    return html_resp(pagina("Configurações", corpo, s, "config"))


@router.post("/painel/configuracoes")
async def painel_config_post(request: Request, s: dict = Depends(exigir_login)):
    slug = s["clinica"]["slug"]
    f = await request.form()
    conf = obter_conf(slug)
    for ch in ("nome_assistente", "endereco", "funcionamento", "pagamento", "convenios", "encaminhar_quando", "extra"):
        conf[ch] = str(f.get(ch, "")).strip()[:4000]
    profs = []
    for i in range(60):
        if f.get(f"prof_nome_{i}") is None:
            break
        if f.get(f"prof_remover_{i}"):
            continue
        x = {ch: str(f.get(f"prof_{ch}_{i}", "")).strip()[:500] for ch, _, _ in PROF_CAMPOS}
        if x["nome"]:
            profs.append(x)
    conf["profissionais"] = profs
    conf["atualizado_em"] = agora().isoformat(timespec="seconds")
    conf["atualizado_por"] = s["usuario"]["email"]
    salvar_conf(slug, conf)
    return RedirectResponse("/painel/configuracoes?salvo=1", status_code=303)


@router.get("/painel/senha", response_class=HTMLResponse)
def painel_senha(erro: str = "", ok: str = "", s: dict = Depends(exigir_login)):
    aviso = (f'<div class="aviso erro">{esc(erro)}</div>' if erro else "") + \
            ('<div class="aviso">Senha trocada.</div>' if ok else "")
    corpo = f"""{aviso}<form method="post" class="card" style="max-width:420px"><h2>Trocar senha</h2>
      <label>Senha atual</label><input type="password" name="atual" required autocomplete="current-password">
      <label>Nova senha (mínimo 8 caracteres)</label><input type="password" name="nova" required minlength="8" autocomplete="new-password">
      <div style="margin-top:14px"><button class="btn">Trocar</button></div></form>"""
    return html_resp(pagina("Senha", corpo, s, "config"))


@router.post("/painel/senha")
def painel_senha_post(atual: str = Form(""), nova: str = Form(""), s: dict = Depends(exigir_login)):
    u = obter_usuario(s["usuario"]["email"])
    if not conferir_senha(atual, u.get("hash", "")):
        return RedirectResponse("/painel/senha?erro=Senha+atual+incorreta.", status_code=303)
    if len(nova) < 8:
        return RedirectResponse("/painel/senha?erro=A+nova+senha+precisa+de+8+caracteres.", status_code=303)
    u["hash"] = hash_senha(nova)
    salvar_usuario(u)
    return RedirectResponse("/painel/senha?ok=1", status_code=303)


# ============================================================
# ADMIN DA A GENTE ASSUME — cadastro de clinicas
# ============================================================
def form_clinica(c: dict) -> str:
    def i(ch, rot, tipo="text", ex=""):
        return f'<label>{rot}</label><input type="{tipo}" name="{ch}" value="{esc(c.get(ch, ""))}" placeholder="{esc(ex)}">'
    return f"""<div class="grid2">
      <div>{i('nome', 'Nome da clínica', ex='Clínica do Ombro POA')}</div>
      <div>{i('recepcao_tel', 'WhatsApp da recepção (recebe os avisos)', 'tel', '51999998888')}</div>
      <div>{i('zapi_instance', 'Z-API Instance ID')}</div>
      <div>{i('zapi_token', 'Z-API Token')}</div>
      <div>{i('zapi_client_token', 'Z-API Client Token')}</div>
    </div>"""


@router.get("/admin/clinicas", response_class=HTMLResponse)
async def admin_clinicas(msg: str = "", admin: str = Depends(exigir_admin)):
    linhas = []
    for c in listar_clinicas():
        st = await status_conexao(c)
        n_users = R.scard(K(c["slug"], "usuarios"))
        linhas.append(f"""<tr><td><b><a href="/admin/clinicas/{c['slug']}">{esc(c['nome'])}</a></b><br>
          <span class="mono" style="font-size:12px">{esc(c['slug'])}</span></td>
          <td>{esc(st['estado'])}</td><td>{'ligado' if c.get('bot_ativo') else 'desligado'}</td>
          <td>{n_users}</td><td>{'desativada' if c.get('desativada') else esc(rotulo_situacao(c))}</td></tr>""")
    tabela = ("<table><tr><th>Clínica</th><th>WhatsApp</th><th>Assistente</th><th>Usuários</th><th>Situação</th></tr>"
              + "".join(linhas) + "</table>") if linhas else '<div class="vazio">Nenhuma clínica ainda.</div>'
    aviso = f'<div class="aviso">{esc(msg)}</div>' if msg else ""
    corpo = f"""<p style="margin-bottom:12px"><a href="/admin">Painel admin</a></p>{aviso}
    <div class="card"><h2>Clínicas</h2>{tabela}</div>
    {html_admin_plano()}
    <form method="post" action="/admin/clinicas/nova" class="card"><h2>Nova clínica (cadastro feito por vocês, sem pagamento)</h2>
      {form_clinica({})}
      <h3 style="font-size:15px;margin-top:16px">Primeiro acesso</h3>
      <div class="grid2">
        <div><label>Nome de quem vai usar</label><input type="text" name="u_nome" placeholder="Dra. Ana"></div>
        <div><label>E-mail de login</label><input type="email" name="u_email" required></div>
        <div><label>Senha inicial (mínimo 8)</label><input type="text" name="u_senha" required minlength="8"></div>
      </div>
      <p style="font-size:13px;color:var(--txt2);margin-top:10px">A clínica começa com o assistente desligado. Ligue depois de preencher as configurações.</p>
      <div style="margin-top:12px"><button class="btn">Criar clínica</button></div></form>"""
    return html_resp(pagina("Clínicas", corpo))


@router.post("/admin/clinicas/nova")
async def admin_clinica_nova(request: Request, admin: str = Depends(exigir_admin)):
    f = await request.form()
    nome = str(f.get("nome", "")).strip()
    email = str(f.get("u_email", "")).strip().lower()
    senha = str(f.get("u_senha", ""))
    if not nome or not email or len(senha) < 8:
        return RedirectResponse("/admin/clinicas?msg=Preencha+nome,+e-mail+e+senha+(8+caracteres).", status_code=303)
    if obter_usuario(email):
        return RedirectResponse("/admin/clinicas?msg=Esse+e-mail+ja+tem+acesso.", status_code=303)
    base = slugificar(nome)
    slug, n = base, 2
    while R.exists(K(slug, "cfg")):
        slug, n = f"{base}-{n}", n + 1
    c = {"slug": slug, "nome": nome, "bot_ativo": False, "webhook_chave": secrets.token_urlsafe(18),
         "criado_em": agora().isoformat(timespec="seconds")}
    for ch in ("recepcao_tel", "zapi_instance", "zapi_token", "zapi_client_token"):
        c[ch] = str(f.get(ch, "")).strip()
    salvar_clinica(c)
    salvar_usuario({"email": email, "nome": str(f.get("u_nome", "")).strip(), "slug": slug, "hash": hash_senha(senha)})
    return RedirectResponse(f"/admin/clinicas/{slug}?msg=Clinica+criada.", status_code=303)


@router.get("/admin/clinicas/{slug}", response_class=HTMLResponse)
async def admin_clinica(slug: str, msg: str = "", admin: str = Depends(exigir_admin)):
    c = obter_clinica(slug)
    if not c:
        raise HTTPException(status_code=404)
    st = await status_conexao(c)
    users = "".join(f"<li>{esc(u.get('nome') or '')} &lt;{esc(u['email'])}&gt;</li>" for u in usuarios_da_clinica(slug))
    aviso = "".join(f'<div class="aviso">{esc(x)}</div>' for x in msg.split("||") if x)
    corpo = f"""<p style="margin-bottom:12px"><a href="/admin/clinicas">Todas as clínicas</a></p>{aviso}
    <div class="card"><h2>{esc(c['nome'])}</h2>
      <p style="font-size:14px">WhatsApp: <b>{esc(st['estado'])}</b>, {esc(st['detalhe'])}<br>
      Assistente: <b>{'ligado' if c.get('bot_ativo') else 'desligado'}</b><br>
      Situação: <b>{esc(rotulo_situacao(c))}</b>{html_assinatura_admin(c)}</p>
      <label>Webhook "ao receber" desta clínica na Z-API</label>
      <input type="text" readonly value="{esc(url_webhook(c))}" onclick="this.select()">
      <p style="font-size:13px;color:var(--txt2);margin-top:6px">Na Z-API, ligue também "Notificar as enviadas por mim",
      para o painel ver quando a recepção responde pelo celular (e pausar o assistente naquela conversa).</p>
      <form method="post" action="/admin/clinicas/{slug}/webhook" style="margin-top:10px">
        <button class="btn sec peq">Configurar isso na Z-API automaticamente</button></form>
    </div>
    <form method="post" action="/admin/clinicas/{slug}" class="card"><h2>Dados</h2>{form_clinica(c)}
      <label>Situação</label><select name="situacao">{''.join(f'<option value="{v}" {"selected" if situacao(c) == v else ""}>{t}</option>' for v, t in SITUACOES.items())}</select>
      <label><input type="checkbox" name="desativada" value="1" {'checked' if c.get('desativada') else ''}> Clínica desativada (ninguém entra, o assistente para)</label>
      <div style="margin-top:12px"><button class="btn">Salvar</button></div></form>
    <form method="post" action="/admin/clinicas/{slug}/usuario" class="card"><h2>Acessos</h2>
      <ul style="margin:0 0 10px 18px;font-size:14px">{users or '<li>nenhum</li>'}</ul>
      <p style="font-size:13px;color:var(--txt2)">Para criar um acesso ou trocar a senha de um que já existe:</p>
      <div class="grid2"><div><label>Nome</label><input type="text" name="u_nome"></div>
        <div><label>E-mail</label><input type="email" name="u_email" required></div>
        <div><label>Senha (mínimo 8)</label><input type="text" name="u_senha" required minlength="8"></div></div>
      <div style="margin-top:12px"><button class="btn sec">Salvar acesso</button></div></form>"""
    return html_resp(pagina(c["nome"], corpo))


@router.post("/admin/clinicas/{slug}")
async def admin_clinica_salvar(slug: str, request: Request, admin: str = Depends(exigir_admin)):
    c = obter_clinica(slug)
    if not c:
        raise HTTPException(status_code=404)
    f = await request.form()
    for ch in ("nome", "recepcao_tel", "zapi_instance", "zapi_token", "zapi_client_token"):
        c[ch] = str(f.get(ch, "")).strip()
    c["desativada"] = bool(f.get("desativada"))
    if f.get("situacao") in SITUACOES:
        c["situacao"] = f.get("situacao")
    if c["desativada"]:
        c["bot_ativo"] = False
    salvar_clinica(c)
    R.delete(K(slug, "zapi_status"))
    return RedirectResponse(f"/admin/clinicas/{slug}?msg=Salvo.", status_code=303)


@router.post("/admin/clinicas/{slug}/usuario")
async def admin_clinica_usuario(slug: str, request: Request, admin: str = Depends(exigir_admin)):
    if not obter_clinica(slug):
        raise HTTPException(status_code=404)
    f = await request.form()
    email = str(f.get("u_email", "")).strip().lower()
    senha = str(f.get("u_senha", ""))
    existente = obter_usuario(email)
    if existente and existente["slug"] != slug:
        return RedirectResponse(f"/admin/clinicas/{slug}?msg=Esse+e-mail+e+de+outra+clinica.", status_code=303)
    if not email or len(senha) < 8:
        return RedirectResponse(f"/admin/clinicas/{slug}?msg=E-mail+e+senha+(8+caracteres).", status_code=303)
    salvar_usuario({"email": email, "nome": str(f.get("u_nome", "")).strip() or (existente or {}).get("nome", ""),
                    "slug": slug, "hash": hash_senha(senha)})
    return RedirectResponse(f"/admin/clinicas/{slug}?msg=Acesso+salvo.", status_code=303)


@router.post("/admin/clinicas/{slug}/webhook")
async def admin_clinica_webhook(slug: str, admin: str = Depends(exigir_admin)):
    c = obter_clinica(slug)
    if not c:
        raise HTTPException(status_code=404)
    if not zapi_configurada(c):
        return RedirectResponse(f"/admin/clinicas/{slug}?msg=Preencha+Instance+ID+e+Token+da+Z-API+primeiro.", status_code=303)
    try:
        res = await configurar_webhook_zapi(c)
    except Exception as e:
        res = [f"erro: {e}"]
    from urllib.parse import quote
    return RedirectResponse(f"/admin/clinicas/{slug}?msg=" + quote("||".join(res)), status_code=303)


# ============================================================
# CONTA PROPRIA: criar conta, esqueci a senha, assinatura (Mercado Pago)
# ============================================================
# Variaveis:
#   EMAIL_FROM             remetente (padrao: nao-responda@agenteassume.com.br)
#   RESEND_API_KEY         envia e-mail pela API do Resend (funciona no plano Hobby do Railway)
#   BREVO_API_KEY          ou pela API da Brevo
#   SMTP_HOST/PORT/USER/PASS  ou por SMTP (no Railway, so no plano Pro: Hobby bloqueia SMTP)
#   CLINICA_MP_ACCESS_TOKEN   Access Token do Mercado Pago (padrao: MP_ACCESS_TOKEN)
#   AVISO_EMAIL            e-mail de voces, recebe "clinica nova pagou, criar Z-API"
#   AVISO_WHATSAPP         WhatsApp de voces, recebe o mesmo aviso (pelo numero da prospeccao)
import smtplib
import ssl
from email.message import EmailMessage
from urllib.parse import quote

EMAIL_FROM     = os.environ.get("EMAIL_FROM", "nao-responda@agenteassume.com.br")
EMAIL_NOME     = os.environ.get("EMAIL_NOME", "a gente assume")
RESEND_KEY     = os.environ.get("RESEND_API_KEY", "")
BREVO_KEY      = os.environ.get("BREVO_API_KEY", "")
SMTP_HOST      = os.environ.get("SMTP_HOST", "")
SMTP_PORT      = int(os.environ.get("SMTP_PORT", "465"))
SMTP_USER      = os.environ.get("SMTP_USER", EMAIL_FROM)
SMTP_PASS      = os.environ.get("SMTP_PASS", "")
MP_TOKEN       = os.environ.get("CLINICA_MP_ACCESS_TOKEN") or os.environ.get("MP_ACCESS_TOKEN", "")
AVISO_EMAIL    = os.environ.get("AVISO_EMAIL", "")
AVISO_WHATSAPP = os.environ.get("AVISO_WHATSAPP", "")
REVERIFICAR_H  = 12          # de quanto em quanto tempo o painel confere a assinatura no MP

SITUACOES = {"ativa": "Ativa", "pendente": "Aguardando pagamento", "bloqueada": "Pagamento parado ou cancelado"}


def rotulo_situacao(c: dict) -> str:
    return SITUACOES.get(situacao(c), situacao(c))


def html_assinatura_admin(c: dict) -> str:
    a = c.get("assinatura") or {}
    if not a:
        return ""
    return (f'<br>Assinatura MP: <span class="mono">{esc(a.get("preapproval_id", ""))}</span>, '
            f'status <b>{esc(a.get("status_mp", ""))}</b>, valor R$ {esc(a.get("valor", ""))}')


# ---------- e-mail ----------
def _enviar_email_sync(para: str, assunto: str, texto: str) -> bool:
    try:
        if RESEND_KEY:
            r_ = httpx.post("https://api.resend.com/emails", timeout=20,
                            headers={"Authorization": f"Bearer {RESEND_KEY}"},
                            json={"from": f"{EMAIL_NOME} <{EMAIL_FROM}>", "to": [para],
                                  "subject": assunto, "text": texto})
            print(f"EMAIL resend {r_.status_code}")
            return r_.status_code < 300
        if BREVO_KEY:
            r_ = httpx.post("https://api.brevo.com/v3/smtp/email", timeout=20,
                            headers={"api-key": BREVO_KEY},
                            json={"sender": {"name": EMAIL_NOME, "email": EMAIL_FROM}, "to": [{"email": para}],
                                  "subject": assunto, "textContent": texto})
            print(f"EMAIL brevo {r_.status_code}")
            return r_.status_code < 300
        if SMTP_HOST:
            m = EmailMessage()
            m["From"] = f"{EMAIL_NOME} <{EMAIL_FROM}>"
            m["To"] = para
            m["Subject"] = assunto
            m.set_content(texto)
            if SMTP_PORT == 465:
                with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ssl.create_default_context(), timeout=20) as srv:
                    srv.login(SMTP_USER, SMTP_PASS)
                    srv.send_message(m)
            else:
                with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as srv:
                    srv.starttls(context=ssl.create_default_context())
                    srv.login(SMTP_USER, SMTP_PASS)
                    srv.send_message(m)
            print("EMAIL smtp ok")
            return True
        print(f"EMAIL nao configurado: '{assunto}' para {para} nao foi enviado")
    except Exception as e:
        print(f"EMAIL erro: {type(e).__name__}: {e}")
    return False


async def enviar_email(para: str, assunto: str, texto: str) -> bool:
    return await asyncio.to_thread(_enviar_email_sync, para, assunto, texto)


async def avisar_equipe_aga(texto: str, assunto: str = "Aviso da plataforma"):
    if AVISO_EMAIL:
        await enviar_email(AVISO_EMAIL, assunto, texto)
    if AVISO_WHATSAPP:
        try:
            from main import enviar_whatsapp
            await enviar_whatsapp(so_digitos(AVISO_WHATSAPP), texto)
        except Exception as e:
            print(f"AVISO whatsapp erro: {e}")


# ---------- plano ----------
def obter_plano() -> dict:
    v = R.get("cl:plano")
    return json.loads(v) if v else {"nome": "a gente assume", "valor": ""}


def valor_plano() -> float | None:
    try:
        v = float(str(obter_plano().get("valor", "")).replace(",", "."))
        return v if v > 0 else None
    except ValueError:
        return None


def fmt_reais(v: float) -> str:
    return f"R$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def html_admin_plano() -> str:
    p = obter_plano()
    falta = "" if MP_TOKEN else ('<div class="aviso erro">Falta a variável CLINICA_MP_ACCESS_TOKEN (ou MP_ACCESS_TOKEN) '
                                 'no Railway. Sem ela ninguém consegue pagar.</div>')
    return f"""<form method="post" action="/admin/plano" class="card"><h2>Plano (o que a clínica paga ao criar a conta)</h2>{falta}
      <div class="grid2"><div><label>Nome que aparece no Mercado Pago</label><input type="text" name="nome" value="{esc(p.get('nome', ''))}"></div>
      <div><label>Mensalidade em R$ (tudo incluído)</label><input type="text" name="valor" value="{esc(p.get('valor', ''))}" placeholder="ex.: 497"></div></div>
      <p style="font-size:13px;color:var(--txt2);margin-top:8px">Vale para as próximas assinaturas. Quem já assinou continua no valor que assinou.</p>
      <div style="margin-top:10px"><button class="btn sec">Salvar plano</button></div></form>"""


@router.post("/admin/plano")
async def admin_plano(request: Request, admin: str = Depends(exigir_admin)):
    f = await request.form()
    R.set("cl:plano", json.dumps({"nome": str(f.get("nome", "")).strip()[:120] or "a gente assume",
                                  "valor": str(f.get("valor", "")).strip().replace("R$", "").strip()}))
    return RedirectResponse("/admin/clinicas?msg=Plano+salvo.", status_code=303)


# ---------- telas publicas: criar conta, esqueci, redefinir ----------
def tela_publica(titulo: str, sub: str, miolo: str, erro: str = "", ok: str = "") -> HTMLResponse:
    aviso = (f'<div class="aviso erro">{esc(erro)}</div>' if erro else "") + (f'<div class="aviso">{esc(ok)}</div>' if ok else "")
    corpo = f"""<div class="login">
      <p class="mono" style="font-size:12px;color:var(--txt2);margin-bottom:8px"><a href="https://agenteassume.com.br" style="text-decoration:none">a gente assume</a></p>
      <h1>{titulo}</h1><p style="color:var(--txt2);margin-bottom:18px">{sub}</p>{aviso}{miolo}</div>"""
    return html_resp(pagina(titulo, corpo))


@router.get("/", include_in_schema=False)
def raiz(request: Request):
    # no app.agenteassume.com.br a raiz leva ao login; no endereco do Railway continua o status de antes
    if request.headers.get("host", "").startswith("app."):
        return RedirectResponse("/entrar", status_code=303)
    return {"status": f"{os.environ.get('AGENT_NAME', 'PrimeiraMente')} online"}


@router.get("/criar-conta", response_class=HTMLResponse)
def criar_conta_get(request: Request, erro: str = ""):
    if sessao_atual(request):
        return RedirectResponse("/painel", status_code=303)
    v = valor_plano()
    preco = f"{fmt_reais(v)} por mês, tudo incluído. " if v else ""
    miolo = f"""<form method="post" class="card">
      <label>Nome da clínica</label><input type="text" name="clinica" required maxlength="120">
      <label>Seu nome</label><input type="text" name="nome" required maxlength="80">
      <label>WhatsApp para avisos da equipe</label><input type="tel" name="whatsapp" required placeholder="(51) 99999-8888">
      <label>E-mail</label><input type="email" name="email" required autocomplete="username">
      <label>Senha (mínimo 8 caracteres)</label><input type="password" name="senha" required minlength="8" autocomplete="new-password">
      <div style="margin-top:16px"><button class="btn" style="width:100%">Continuar para o pagamento</button></div>
    </form>
    <p style="font-size:14px">Já tem conta? <a href="/entrar">Entrar</a></p>"""
    return tela_publica("Criar conta", f"{preco}Depois do pagamento, a gente prepara o WhatsApp da sua clínica em até 24 horas.", miolo, erro)


@router.post("/criar-conta")
def criar_conta_post(request: Request, clinica: str = Form(""), nome: str = Form(""), whatsapp: str = Form(""),
                     email: str = Form(""), senha: str = Form("")):
    email = email.strip().lower()
    clinica = clinica.strip()[:120]
    ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "").split(",")[0].strip()
    lim = f"cl:cadastros:{ip}"
    if int(R.get(lim) or 0) >= 5:
        return RedirectResponse("/criar-conta?erro=Muitas+contas+criadas+daqui.+Tente+mais+tarde.", status_code=303)
    if not clinica or not nome.strip() or not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return RedirectResponse("/criar-conta?erro=Preencha+todos+os+campos.", status_code=303)
    if len(so_digitos(whatsapp)) < 10:
        return RedirectResponse("/criar-conta?erro=Confira+o+WhatsApp,+com+DDD.", status_code=303)
    if len(senha) < 8:
        return RedirectResponse("/criar-conta?erro=A+senha+precisa+de+8+caracteres.", status_code=303)
    if obter_usuario(email):
        return RedirectResponse("/criar-conta?erro=Esse+e-mail+ja+tem+conta.+Entre+ou+use+Esqueci+a+senha.", status_code=303)
    R.incr(lim)
    R.expire(lim, 3600)
    base = slugificar(clinica)
    slug, n = base, 2
    while R.exists(K(slug, "cfg")):
        slug, n = f"{base}-{n}", n + 1
    salvar_clinica({"slug": slug, "nome": clinica, "bot_ativo": False, "webhook_chave": secrets.token_urlsafe(18),
                    "criado_em": agora().isoformat(timespec="seconds"), "situacao": "pendente",
                    "recepcao_tel": so_digitos(whatsapp), "origem": "cadastro"})
    salvar_usuario({"email": email, "nome": nome.strip()[:80], "slug": slug, "hash": hash_senha(senha)})
    resp = RedirectResponse("/assinar", status_code=303)
    resp.set_cookie(COOKIE, criar_sessao(email), max_age=SESSAO_DIAS * 86400, httponly=True,
                    secure=BASE_URL.startswith("https"), samesite="lax")
    return resp


@router.get("/esqueci", response_class=HTMLResponse)
def esqueci_get(ok: str = ""):
    miolo = """<form method="post" class="card"><label>E-mail da conta</label>
      <input type="email" name="email" required autofocus>
      <div style="margin-top:16px"><button class="btn" style="width:100%">Enviar link</button></div></form>
      <p style="font-size:14px"><a href="/entrar">Voltar para Entrar</a></p>"""
    msg = "Se esse e-mail tiver conta, o link chegou. Ele vale por 1 hora. Olhe também no spam." if ok else ""
    return tela_publica("Esqueci a senha", "A gente manda um link para você criar uma senha nova.", miolo, ok=msg)


@router.post("/esqueci")
async def esqueci_post(request: Request, email: str = Form("")):
    email = email.strip().lower()
    ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "").split(",")[0].strip()
    lim = f"cl:esqueci:{email}:{ip}"
    u = obter_usuario(email)
    if u and int(R.get(lim) or 0) < 3:
        R.incr(lim)
        R.expire(lim, 3600)
        token = secrets.token_urlsafe(32)
        R.set(f"cl:reset:{token}", email, ex=3600)
        link = f"{BASE_URL}/redefinir?t={token}"
        await enviar_email(email, "Sua senha nova na a gente assume", (
            f"Oi, {u.get('nome') or ''}.\n\nPara criar uma senha nova, abra este link (vale por 1 hora):\n{link}\n\n"
            "Se não foi você que pediu, é só ignorar este e-mail. Sua senha continua a mesma.\n\na gente assume"))
    return RedirectResponse("/esqueci?ok=1", status_code=303)


@router.get("/redefinir", response_class=HTMLResponse)
def redefinir_get(t: str = "", erro: str = ""):
    if not R.get(f"cl:reset:{t}"):
        return tela_publica("Link vencido", "Esse link já foi usado ou passou de 1 hora.",
                            '<p><a class="btn" href="/esqueci">Pedir outro link</a></p>')
    miolo = f"""<form method="post" class="card"><input type="hidden" name="t" value="{esc(t)}">
      <label>Senha nova (mínimo 8 caracteres)</label><input type="password" name="senha" required minlength="8" autocomplete="new-password" autofocus>
      <div style="margin-top:16px"><button class="btn" style="width:100%">Salvar e entrar</button></div></form>"""
    return tela_publica("Senha nova", "Escolha a senha nova da sua conta.", miolo, erro)


@router.post("/redefinir")
def redefinir_post(t: str = Form(""), senha: str = Form("")):
    email = R.get(f"cl:reset:{t}")
    if not email:
        return RedirectResponse(f"/redefinir?t={quote(t)}", status_code=303)
    if len(senha) < 8:
        return RedirectResponse(f"/redefinir?t={quote(t)}&erro=A+senha+precisa+de+8+caracteres.", status_code=303)
    u = obter_usuario(email)
    if not u:
        return RedirectResponse("/entrar", status_code=303)
    R.delete(f"cl:reset:{t}")
    u["hash"] = hash_senha(senha)
    salvar_usuario(u)
    encerrar_sessoes(email)
    resp = RedirectResponse("/painel", status_code=303)
    resp.set_cookie(COOKIE, criar_sessao(email), max_age=SESSAO_DIAS * 86400, httponly=True,
                    secure=BASE_URL.startswith("https"), samesite="lax")
    return resp


# ---------- assinatura no Mercado Pago (preapproval sem plano) ----------
async def mp(metodo: str, caminho: str, corpo: dict | None = None) -> tuple[int, dict]:
    async with httpx.AsyncClient(timeout=20) as http:
        r_ = await http.request(metodo, f"https://api.mercadopago.com{caminho}", json=corpo,
                                headers={"Authorization": f"Bearer {MP_TOKEN}"})
    try:
        return r_.status_code, r_.json()
    except Exception:
        return r_.status_code, {"texto": r_.text[:300]}


async def aplicar_assinatura(slug: str, dados: dict) -> dict | None:
    """Atualiza a clinica a partir de uma preapproval do MP. Devolve a clinica atualizada."""
    c = obter_clinica(slug)
    if not c:
        return None
    a = c.get("assinatura") or {}
    status_mp = dados.get("status", "")
    a.update({"preapproval_id": dados.get("id", a.get("preapproval_id")), "status_mp": status_mp,
              "verificado_em": agora().isoformat(timespec="seconds"),
              "proximo_debito": dados.get("next_payment_date", ""),
              "valor": (dados.get("auto_recurring") or {}).get("transaction_amount", a.get("valor"))})
    c["assinatura"] = a
    antes = situacao(c)
    if status_mp == "authorized":
        c["situacao"] = "ativa"
    elif status_mp in ("paused", "cancelled"):
        c["situacao"] = "bloqueada"
        c["bot_ativo"] = False
    salvar_clinica(c)
    if antes != "ativa" and c["situacao"] == "ativa" and not c.get("aviso_pago_enviado"):
        c["aviso_pago_enviado"] = True
        salvar_clinica(c)
        donos = ", ".join(u["email"] for u in usuarios_da_clinica(slug))
        await avisar_equipe_aga(
            f"Clínica nova pagou: {c['nome']}\nContato: {donos} / {fmt_tel(c.get('recepcao_tel', ''))}\n"
            f"Criar a instância Z-API dela e preencher em {BASE_URL}/admin/clinicas/{slug} (prazo prometido: 24h).",
            "Clínica nova pagou: criar o WhatsApp dela")
    elif antes == "ativa" and c["situacao"] == "bloqueada":
        await avisar_equipe_aga(f"A assinatura da {c['nome']} ficou {status_mp} no Mercado Pago. O assistente dela foi desligado.",
                                "Assinatura parada")
    return c


async def reverificar_assinatura(c: dict) -> dict:
    a = c.get("assinatura") or {}
    pid = a.get("preapproval_id")
    if not pid or not MP_TOKEN:
        return c
    try:
        if (agora() - datetime.fromisoformat(a.get("verificado_em", "2000-01-01T00:00:00-03:00"))).total_seconds() < REVERIFICAR_H * 3600:
            return c
        code, dados = await mp("GET", f"/preapproval/{pid}")
        if code == 200:
            return await aplicar_assinatura(c["slug"], dados) or c
    except Exception as e:
        print(f"MP reverificar {c['slug']}: {e}")
    return c


async def tratar_webhook_mp(tipo: str, id_mp: str) -> bool:
    """Chamado pelo /webhook/mercadopago do main.py. Devolve True se o aviso era de uma clinica."""
    if not MP_TOKEN or not id_mp:
        return False
    try:
        if "preapproval" in tipo or "subscription" in tipo:
            code, d = await mp("GET", f"/preapproval/{id_mp}")
            if code != 200 and "authorized_payment" in tipo:
                code, d = await mp("GET", f"/authorized_payments/{id_mp}")
                if code == 200 and d.get("preapproval_id"):
                    code, d = await mp("GET", f"/preapproval/{d['preapproval_id']}")
        elif "payment" in tipo:
            code, d = await mp("GET", f"/v1/payments/{id_mp}")
            pid = (d.get("metadata") or {}).get("preapproval_id") or d.get("preapproval_id") if code == 200 else None
            if not str(d.get("external_reference", "")).startswith("clinica:"):
                return False
            if pid:
                code, d = await mp("GET", f"/preapproval/{pid}")
            else:
                return True
        else:
            return False
        ref = str(d.get("external_reference", "")) if code == 200 else ""
        if not ref.startswith("clinica:"):
            return False
        await aplicar_assinatura(ref.split(":", 1)[1], d)
        return True
    except Exception as e:
        print(f"MP webhook clinica erro: {e}")
        return False


@router.get("/assinar", response_class=HTMLResponse)
async def assinar(request: Request, erro: str = "", s: dict = Depends(exigir_login)):
    c = await reverificar_assinatura(s["clinica"])
    if situacao(c) == "ativa":
        return RedirectResponse("/painel", status_code=303)
    v = valor_plano()
    bloqueada = situacao(c) == "bloqueada"
    if not v or not MP_TOKEN:
        miolo = '<div class="card"><p>O pagamento ainda não está disponível. A gente avisa você pelo WhatsApp.</p></div>'
    else:
        miolo = f"""<div class="card">
          <p class="mono" style="font-size:28px;font-weight:600">{fmt_reais(v)}<span style="font-size:14px;color:var(--txt2)"> por mês</span></p>
          <p style="color:var(--txt2);font-size:14px;margin:6px 0 14px">Tudo incluído: o assistente no WhatsApp da clínica, o painel e o número.
          Cobrança mensal no cartão pelo Mercado Pago. Você cancela quando quiser.</p>
          <form method="post" action="/assinar"><button class="btn" style="width:100%">Pagar com Mercado Pago</button></form></div>"""
    sub = ("O pagamento da sua assinatura parou. Para voltar a usar, assine de novo." if bloqueada
           else f"Falta só o pagamento para liberar o painel da {esc(c['nome'])}.")
    miolo += '<p style="font-size:14px"><a href="/sair">Sair</a></p>'
    return tela_publica("Assinatura", sub, miolo, erro)


@router.post("/assinar")
async def assinar_post(s: dict = Depends(exigir_login)):
    c = s["clinica"]
    v = valor_plano()
    if not v or not MP_TOKEN:
        return RedirectResponse("/assinar", status_code=303)
    corpo = {"reason": obter_plano().get("nome") or "a gente assume", "external_reference": f"clinica:{c['slug']}",
             "payer_email": s["usuario"]["email"], "back_url": f"{BASE_URL}/assinar/retorno", "status": "pending",
             "auto_recurring": {"frequency": 1, "frequency_type": "months", "transaction_amount": v, "currency_id": "BRL"}}
    code, d = await mp("POST", "/preapproval", corpo)
    if code >= 300 or not d.get("init_point"):
        print(f"MP criar assinatura {c['slug']}: {code} {str(d)[:300]}")
        return RedirectResponse("/assinar?erro=Nao+deu+para+abrir+o+pagamento.+Tente+de+novo+em+instantes.", status_code=303)
    c = obter_clinica(c["slug"])
    c["assinatura"] = {"preapproval_id": d["id"], "status_mp": d.get("status", "pending"), "valor": v,
                       "verificado_em": agora().isoformat(timespec="seconds")}
    salvar_clinica(c)
    return RedirectResponse(d["init_point"], status_code=303)


@router.get("/assinar/retorno")
async def assinar_retorno(request: Request, preapproval_id: str = ""):
    s = sessao_atual(request)
    if not s:
        return RedirectResponse("/entrar", status_code=303)
    c = s["clinica"]
    pid = preapproval_id or (c.get("assinatura") or {}).get("preapproval_id")
    if pid and MP_TOKEN:
        code, d = await mp("GET", f"/preapproval/{pid}")
        if code == 200 and d.get("external_reference") == f"clinica:{c['slug']}":
            c = await aplicar_assinatura(c["slug"], d) or c
    if situacao(c) == "ativa":
        return RedirectResponse("/painel?msg=Pagamento+confirmado.+Bem-vindo!", status_code=303)
    return RedirectResponse("/assinar?erro=O+pagamento+ainda+nao+foi+confirmado.+Se+voce+acabou+de+pagar,+atualize+em+1+minuto.",
                            status_code=303)


# ---------- conectar o WhatsApp (QR Code da Z-API) ----------
@router.get("/painel/whatsapp", response_class=HTMLResponse)
async def painel_whatsapp(s: dict = Depends(exigir_login)):
    c = s["clinica"]
    R.delete(K(c["slug"], "zapi_status"))
    st = await status_conexao(c)
    if st["estado"] == "conectado":
        miolo = '<p><b>Pronto, o WhatsApp da clínica está conectado.</b></p><p style="margin-top:12px"><a class="btn" href="/painel">Ir para o painel</a></p>'
    elif st["estado"] == "sem_config":
        miolo = f'<p>{esc(st["detalhe"])}</p>'
    else:
        img = ""
        try:
            url, hd = _zapi(c, "qr-code/image")
            async with httpx.AsyncClient(timeout=15) as http:
                rq = await http.get(url, headers=hd)
            val = rq.json().get("value", "") if rq.status_code < 300 else ""
            if str(val).startswith("data:image"):
                img = f'<img src="{esc(val)}" alt="QR Code" style="width:280px;max-width:100%;display:block;margin:12px auto">'
        except Exception as e:
            print(f"QR {c['slug']}: {e}")
        miolo = (f"""<ol style="margin:0 0 0 18px;font-size:14px;line-height:1.8">
          <li>No celular da clínica, abra o WhatsApp.</li><li>Toque em Mais opções (ou Configurações) e depois em Dispositivos conectados.</li>
          <li>Toque em Conectar um dispositivo e aponte a câmera para o código abaixo.</li></ol>{img or '<p class="vazio">Não deu para gerar o código agora. Esta página tenta de novo sozinha.</p>'}
          <p style="font-size:13px;color:var(--txt2);text-align:center">O código muda a cada poucos segundos. Esta página se atualiza sozinha.</p>""")
    corpo = f'<div class="card" style="max-width:520px"><h2>Conectar o WhatsApp da clínica</h2>{miolo}</div>'
    head = '<meta http-equiv="refresh" content="20">' if st["estado"] != "conectado" else ""
    return html_resp(pagina("WhatsApp", corpo, s, "painel", head))
