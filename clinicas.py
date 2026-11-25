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
  /painel/agenda                    agenda propria (dia/semana): o assistente marca; a equipe marca, move, cancela, bloqueia
  /painel/agendamentos              pedidos que o assistente anotou (profissionais sem horarios na agenda)
  /painel/profissionais             profissionais e horarios (cards; adicionar/editar num painel lateral)
  /painel/configuracoes             "Sua clinica": endereco, convenios, regras -> prompt
  /painel/experimentar              "Simular": a clinica conversa com o assistente fazendo o papel de paciente
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
import base64
import hashlib
import secrets
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote

import httpx
import redis
import agenda as AG
from anthropic import Anthropic
from fastapi import APIRouter, Request, Depends, HTTPException, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials

router = APIRouter()

R = redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379"), decode_responses=True)

BASE_URL      = os.environ.get("BASE_URL", "https://SEU-DOMINIO.up.railway.app").rstrip("/")
ADMIN_USER    = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS    = os.environ.get("ADMIN_PASS", "admin123")
MODELO        = os.environ.get("CLINICA_MODEL", os.environ.get("AGENT_MODEL", "claude-haiku-4-5-20251001"))
_MODELO_NOVO = bool(re.match(r"claude-(haiku|sonnet|opus)-5", MODELO))
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


def tel_paciente(v: str) -> str:
    """Telefone digitado pela equipe no formato do WhatsApp (55 + DDD + numero); '' se nao parece telefone."""
    d = so_digitos(v)
    if len(d) in (10, 11):
        d = "55" + d
    return d if len(d) in (12, 13) and d.startswith("55") else ""


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
    campos = ("respostas", "agendamentos", "remarcacoes", "cancelamentos", "encaminhados")
    tot = {"conversas": len(pacientes), **{c: 0 for c in campos}}
    for d in datas:
        h = R.hgetall(K(slug, "stats", d))
        for campo in campos:
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
    # cada profissional tem um id fixo (a agenda usa o id; o nome pode mudar)
    sem_id = [x for x in d["profissionais"] if not x.get("id")]
    for x in sem_id:
        x["id"] = secrets.token_hex(4)
    if sem_id and v:
        salvar_conf(slug, d)
    return d


def salvar_conf(slug: str, conf: dict):
    R.set(K(slug, "conf"), json.dumps(conf, ensure_ascii=False))


# periodos do dia que o paciente pode pedir (minutos desde 00:00, inicio inclusive, fim exclusivo)
PERIODOS = {"cedo": (0, 10 * 60), "manha": (0, 12 * 60), "almoco": (11 * 60, 14 * 60), "tarde": (12 * 60, 18 * 60),
            "fim_do_dia": (16 * 60, 24 * 60), "noite": (18 * 60, 24 * 60)}


_TITULOS = {"dr": "Dr.", "dr.": "Dr.", "dra": "Dra.", "dra.": "Dra.", "doutor": "Dr.", "doutora": "Dra."}
_NAO_NOME = {"sobrenome", "nome", "xxx", "teste", "fulano", "-"}


def nome_conversa(nome: str) -> str:
    """Como a recepcionista chama o profissional no WhatsApp: título + primeiro nome, com maiúscula certa.
    'Dr. Marco tonding' -> 'Dr. Marco'; 'dra ana souza' -> 'Dra. Ana'; 'Fabio Sobrenome' -> 'Fabio'."""
    partes = [x for x in (nome or "").split() if x.lower() not in _NAO_NOME]
    if not partes:
        return (nome or "").strip()
    titulo = _TITULOS.get(partes[0].lower())
    resto = partes[1:] if titulo else partes
    primeiro = resto[0][:1].upper() + resto[0][1:].lower() if resto else ""
    return " ".join(x for x in (titulo, primeiro) if x) or nome.strip()


def montar_prompt(c: dict, conf: dict) -> str:
    nome_cl = c.get("nome", "a clínica")
    nome_ass = conf.get("nome_assistente") or f"assistente da {nome_cl}"
    p = [
        f"Você é {nome_ass}, e atende pelo WhatsApp os pacientes da {nome_cl}.",
        "Fale em português do Brasil, com educação e calor humano, como uma boa recepcionista.",
        "No WhatsApp, seja breve: uma ou duas frases por mensagem, sem listas longas e sem emoji.",
        "Nunca diga que é um robô ou um bot. Se perguntarem, diga que é o assistente virtual da clínica.",
        "Escreva como uma boa recepcionista de verdade escreveria no WhatsApp: frases curtas, naturais e calorosas, "
        "do jeito que se fala. Nunca copie dados do cadastro para a mensagem: nada de listas de especialidades ou convênios, "
        "nem informações entre parênteses. Use só a informação que responde ao que o paciente precisa agora.",
        "Chame cada profissional como está em \"Como chamar na conversa\" (ex.: \"a Dra. Ana\", \"o Dr. Fabio\"), "
        "nunca pelo nome completo do cadastro.",
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
    com_agenda = AG.profs_com_agenda(conf)
    if profs:
        p += ["", "=== PROFISSIONAIS ==="]
        for x in profs:
            p.append(f"- {x['nome']}")
            p.append(f"  Como chamar na conversa: {nome_conversa(x['nome'])}")
            if AG.tem_agenda(x):
                p.append(f"  Atende (agenda): {AG.resumo_semana(x)}")
            for rot, ch in (("Especialidades", "especialidades"), ("Dias e horários de atendimento", "horarios"),
                            ("Convênios", "convenios"), ("Observações", "obs")):
                if x.get(ch):
                    p.append(f"  {rot}: {x[ch]}")
    if conf.get("extra"):
        p += ["", "=== OUTRAS INFORMAÇÕES E REGRAS DA CLÍNICA ===", conf["extra"]]
    p += [
        "",
        "=== COMO CONDUZIR A CONVERSA ===",
        "Antes de tudo, responda o que o paciente perguntou: se perguntou de um convênio, diga se atende ou não; "
        "se pediu um horário que já passou, diga que já passou; se pediu um dia sem atendimento, diga isso. "
        "Só depois ofereça as alternativas.",
        "Faça uma pergunta por vez: cada mensagem sua tem no máximo uma pergunta (um ponto de interrogação) e pede uma coisa só. "
        "Espere a resposta antes de pedir a próxima coisa. Isso vale também para pedidos sem ponto de interrogação: "
        "quando oferecer horários, termine só com \"Qual prefere?\" e deixe nome e convênio para as mensagens seguintes, um de cada vez. "
        "Não pergunte \"tudo bem?\" na saudação. Depois de oferecer opções de horário, pergunte só \"Qual prefere?\".",
        "Exemplos de primeira resposta: a \"quero marcar uma consulta\": \"Claro! Me conta, é para quê?\"; "
        "a \"estou com dor no ombro\": \"Poxa, sinto muito. Para ombro quem atende é o Dr. Marco. Qual dia e período fica melhor para você?\". "
        "Só isso: uma coisa por mensagem.",
        "Nunca pergunte se pode consultar a agenda (\"posso verificar?\", \"quer que eu veja os horários?\"): "
        "consulte e já responda com as opções, ou pergunte o dia e período se ainda não sabe. "
        "Nunca peça confirmação para marcar (\"posso marcar?\", \"quer que eu marque?\"). Se o paciente pergunta se tem um horário "
        "e já deu o nome e se é particular ou convênio, isso é um pedido para marcar: se o horário está livre, chame marcar_consulta "
        "e responda já confirmando (ex.: \"Pronto, Marcos! Marquei quinta 26/11 às 17h com a Dra. Ana, na Rua das Flores, 100.\").",
        "Para marcar consulta, descubra nesta ordem, pulando o que o paciente já disse: "
        "1) o que ele precisa (o motivo, ou onde é a dor), só quando ele ainda não escolheu o profissional e os profissionais "
        "atendem coisas diferentes (se ele já disse com quem quer marcar, não pergunte o motivo: vá para o dia e o período); "
        "2) o profissional: sugira quem atende aquilo; se mais de um atende, pergunte se tem preferência por algum; "
        "3) o dia e o período que prefere; depois ofereça as opções de horário; só então 4) o nome; 5) se é particular ou convênio (qual).",
        "Sugira só profissionais cujas especialidades cobrem o que o paciente precisa (dor, limpeza, avaliação = clínico geral; "
        "aparelho = ortodontia; implante = implante). Não diga que outro profissional \"também pode ver\" se a especialidade dele não cobre.",
        "Cumprimentar não é obrigatório. Se cumprimentar com bom dia/boa tarde/boa noite, use o do horário de agora "
        "(bom dia até 12h, boa tarde até 18h, boa noite depois), mesmo que o paciente use outro.",
        "Se o paciente contar um problema (dor, incômodo), acolha em poucas palavras, sem exagero, e siga. "
        "Se o caso for de urgência ou dor forte, siga a regra de encaminhar.",
        "Quando perguntar a preferência de profissional, cite só quem atende o caso dele, pelo nome de conversa "
        "(ex.: \"Para isso atendem a Dra. Ana e o Dr. Fabio. Tem preferência por algum?\"). "
        "Se o paciente disser que tanto faz, siga com qualquer um que tenha horário. "
        "Se o paciente já disse o dia e o período, você pode mostrar as opções de cada profissional e perguntar só \"Qual prefere?\": "
        "isso já cobre a preferência de profissional e de horário numa pergunta só.",
        "Quando perguntar a preferência de horário, pergunte de forma aberta (ex.: \"Qual dia e período fica melhor para você?\"). "
        "Se o paciente já disse um dia ou período e ele não tem horário (profissional não atende, feriado, cheio), "
        "não devolva a pergunta: consulte a agenda e já ofereça as opções reais mais próximas.",
        "Se o paciente pedir um profissional que não está na lista acima, diga que não há ninguém com esse nome na clínica, "
        "e pergunte o que ele precisa, para indicar quem atende. Não encaminhe para a equipe por isso.",
        "Entenda o período que o paciente diz: cedo = os primeiros horários da manhã (período cedo); de manhã = antes das 12h; "
        "hora do almoço = 11h às 14h; à tarde ou depois do almoço = 12h às 18h; "
        "final do dia, fim da tarde ou depois do trabalho = a partir das 16h (os últimos horários do dia); "
        "à noite = a partir das 18h. \"Qualquer horário\" ou \"tanto faz\" = sem preferência. "
        "Ofereça só horários dentro do período que ele disse; se não houver, diga isso e ofereça o mais próximo.",
    ]
    if com_agenda:
        nomes = ", ".join(x["nome"] for x in com_agenda)
        p += [
            "",
            "=== AGENDAMENTO (você tem acesso à agenda da clínica) ===",
            f"Você marca consultas de verdade na agenda para: {nomes}.",
            "Nunca invente nem prometa um horário: antes de oferecer, consulte com a ferramenta ver_horarios_livres. "
            "Sempre que o paciente falar de um dia ou horário, chame ver_horarios_livres antes de responder, "
            "mesmo que você já saiba que o dia não tem atendimento, para já oferecer as alternativas reais.",
            "Nunca ofereça um profissional para um dia em que ele não atende. Se o paciente pede um dia sem dizer o profissional, "
            "consulte ver_horarios_livres sem profissional e, ao perguntar a preferência de profissional, cite só quem tem horário livre nesse dia. "
            "Se o profissional que ele quer não atende nesse dia, diga isso e ofereça os dias em que ele atende "
            "ou outro profissional que atende no dia pedido.",
            "A agenda já escreve cada dia do jeito certo (ex.: \"amanhã, terça 24/11\", \"segunda 28/12\"): copie essa escrita "
            "e nunca calcule dia da semana ou \"amanhã\" por conta própria. Não diga \"esta semana\", \"semana que vem\" "
            "nem \"próxima semana\" ao oferecer horários: diga só o dia e a data.",
            "Quando a agenda trouxer a linha SUGESTÃO PARA OFERECER, ofereça esses horários (são no máximo 3), "
            "a não ser que o paciente tenha pedido uma hora específica.",
            "Ofereça duas ou três opções de horário de cada vez, no total (não por dia nem por profissional), com dia da semana, data e hora (ex.: quarta 26/11 às 9h), "
            "de preferência no dia e período que o paciente pediu. Se não houver, ofereça o mais próximo. "
            "Nunca responda só que \"tem horários\" ou \"posso ver outro dia?\": cite sempre as opções concretas "
            "(ex.: \"Amanhã, terça 24/11, tenho 14h ou 15h com a Dra. Ana, ou 16h com o Dr. Fabio. Qual prefere?\"). "
            "Ofereça os horários antes de pedir nome ou convênio.",
            "Se o dia pedido não tem atendimento (feriado, dia em que o profissional não atende, agenda cheia), "
            "consulte ver_horarios_livres a partir do dia seguinte e já ofereça as opções mais próximas.",
            "Se o paciente já disse o profissional, o dia e a hora, e esse horário está livre, e você já sabe o nome dele "
            "e se é particular ou convênio, marque direto com marcar_consulta na mesma resposta, sem perguntar mais nada. "
            "Pergunte o motivo só quando ele decide qual profissional atende (os profissionais fazem coisas diferentes); "
            "se o paciente contou, anote em observacao.",
            "Se o paciente perguntar por um convênio que não está na lista da clínica ou do profissional, comece dizendo "
            "com clareza que não atende esse convênio (nunca comece com \"claro\" ou \"sim\"), e ofereça particular. "
            "Você não atende ninguém: fale do profissional na terceira pessoa (\"o Dr. Fabio atende só particular\").",
            "Antes de marcar, tenha o nome do paciente e saiba se é particular ou convênio (qual). "
            "Se for convênio, confira se o profissional atende esse convênio (veja os convênios de cada um acima); "
            "se não atender, diga isso e ofereça o mesmo profissional como particular, ou outro profissional da mesma especialidade "
            "que atenda o convênio. Particular não é convênio: não o liste entre os convênios. "
            "Quando o paciente escolher um dos horários, marque com marcar_consulta e só depois confirme para ele: "
            "dia, data, hora, profissional e endereço.",
            "Se a ferramenta disser que o horário não está mais livre, peça desculpas e ofereça outros.",
            "Para saber se o paciente já tem consulta marcada, use consultas_do_paciente.",
            "Remarcar: use consultas_do_paciente para achar a consulta (se houver mais de uma, pergunte qual), "
            "pergunte o dia e período que prefere, consulte ver_horarios_livres, ofereça as opções e, quando ele escolher, "
            "chame remarcar_consulta e confirme o novo dia, data, hora e profissional. Não peça nome nem convênio de novo.",
            "Cancelar: use consultas_do_paciente; se só uma consulta bate com o que o paciente disse, cancele direto, sem pedir "
            "confirmação; se houver mais de uma, pergunte qual (dia e hora). Chame "
            "cancelar_consulta e confirme o cancelamento; depois ofereça remarcar para outro dia, sem insistir.",
            f"Pelo WhatsApp só dá para remarcar ou cancelar até {AG.antecedencia_mudar(conf)} horas antes da consulta. "
            "Se faltar menos, diga isso com gentileza e passe para a equipe.",
        ]
        outros = [x for x in profs if not AG.tem_agenda(x)]
        if outros:
            p += [f"Para {', '.join(x['nome'] for x in outros)} ainda não há agenda aqui: anote o pedido como abaixo, "
                  "respeitando os dias e horários escritos para cada um (não sugira dia em que não atende).",
                  "Nesse caso, inclua no fim da sua mensagem, numa linha separada, exatamente:",
                  "[AGENDAMENTO: nome do paciente | profissional ou especialidade | dia e horário desejado | particular ou convênio | observação]",
                  "Essa linha não aparece para o paciente."]
    p += [] if com_agenda else [
        "",
        "=== AGENDAMENTO ===",
        "Você ainda não tem acesso à agenda, então não confirme horário como certo. "
        "Descubra o nome do paciente, o profissional ou a especialidade, o dia e período que prefere e se é particular ou convênio (qual). "
        "Só sugira um profissional para um dia em que ele atende, conforme as informações acima. "
        "Quando tiver isso, diga que vai passar o pedido para a recepção confirmar o horário, "
        "e inclua no fim da sua mensagem, numa linha separada, exatamente:",
        "[AGENDAMENTO: nome do paciente | profissional ou especialidade | dia e horário desejado | particular ou convênio | observação]",
        "Essa linha não aparece para o paciente.",
    ]
    p += [
        "",
        "=== PASSAR PARA A EQUIPE ===",
        "Encaminhe para a equipe quando: o paciente pedir para falar com uma pessoa; houver urgência, dor forte ou reclamação; "
        "a pergunta for sobre algo que não está nas informações acima; ou for remarcar ou cancelar consulta "
        "sem acesso à agenda ou em cima da hora.",
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


DIAS_SEMANA = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"]


def contexto_tempo(conf: dict) -> str:
    """Data de agora e o calendario dos proximos dias (para a IA nao errar dia da semana nem data relativa)."""
    dt = agora()
    hoje = dt.date()
    txt = f"\n\nAGORA: {DIAS_SEMANA[dt.weekday()]}, {dt.strftime('%d/%m/%Y %H:%M')} (horário de Brasília)."
    if AG.profs_com_agenda(conf):
        _, jan = AG.regras(conf)
        n = min(jan, 60) + 1
        prox = [hoje + timedelta(days=i) for i in range(n)]
        txt += "\nCalendário (até onde a agenda marca): " + "; ".join(
            f"{AG.DIAS_CURTOS[d.weekday()]} {d.strftime('%d/%m')}={d.isoformat()}" for d in prox)
        seg_que_vem = hoje + timedelta(days=7 - hoje.weekday())
        dom_hoje = seg_que_vem - timedelta(days=1)
        f = lambda d: f"{AG.DIAS_CURTOS[d.weekday()]} {d.strftime('%d/%m')}"
        txt += (f"\nDatas relativas: hoje = {f(hoje)}; amanhã = {f(hoje + timedelta(days=1))}; "
                f"depois de amanhã = {f(hoje + timedelta(days=2))}; esta semana = até {f(dom_hoje)}; "
                f"semana que vem = de {f(seg_que_vem)} a {f(seg_que_vem + timedelta(days=6))}. "
                "Se o paciente disser só o dia do mês (ex.: dia 25), é o próximo dia 25 a partir de hoje. "
                "Se disser só o dia da semana (ex.: quinta), é a próxima quinta a partir de hoje.")
        txt += "\nQuem atende em cada dia da semana (pela agenda): " + "; ".join(
            f"{AG.DIAS_LONGOS[w]}: " + (", ".join(x["nome"] for x in AG.profs_com_agenda(conf) if AG.intervalos_do_dia(x, w))
                                       or "ninguém") for w in range(7))
        fer = [(d, AG.feriado(d)) for d in prox if AG.feriado(d)]
        if fer:
            lista = ", ".join(f"{f(d)} ({nome})" for d, nome in fer)
            txt += (f"\nFeriados nacionais nesse período: {lista}. "
                    + ("A clínica atende normalmente nesses dias." if not AG.fecha_no_feriado(conf, fer[0][0])
                       else "A clínica não atende nesses dias."))
    return txt


def ferramentas_agenda(conf: dict) -> list:
    nomes = [x["nome"] for x in AG.profs_com_agenda(conf)]
    if not nomes:
        return []
    prof = {"type": "string", "enum": nomes, "description": "Nome do profissional, exatamente como na lista."}
    return [
        {"name": "ver_horarios_livres",
         "description": "Mostra os horários livres de verdade na agenda da clínica, por profissional. "
                        "Use sempre antes de oferecer horários ou um profissional ao paciente. "
                        "Sem profissional, mostra todos os que atendem no período.",
         "input_schema": {"type": "object", "properties": {
             "profissional": {**prof, "description": "Nome do profissional, exatamente como na lista. "
                              "Deixe vazio para ver todos os profissionais que atendem no período."},
             "data_inicial": {"type": "string", "description": "Primeiro dia a olhar, AAAA-MM-DD. Se não souber, deixe vazio (hoje)."},
             "dias": {"type": "integer", "description": "Quantos dias olhar a partir da data inicial (1 a 14). Padrão 7."},
             "periodo": {"type": "string", "enum": [*PERIODOS, "qualquer"],
                         "description": "Período que o paciente prefere: cedo = primeiros horários da manhã (antes das 10h), "
                                        "manha = antes das 12h, "
                                        "almoco = 11h às 14h, tarde = 12h às 18h (depois do almoço também), "
                                        "fim_do_dia = a partir das 16h (final do dia, fim da tarde, depois do trabalho), "
                                        "noite = a partir das 18h. Se o paciente pediu uma hora exata, use qualquer. Padrão qualquer."}},
             "required": []}},
        {"name": "marcar_consulta",
         "description": "Marca a consulta na agenda da clínica, num horário que apareceu como livre e que o paciente escolheu.",
         "input_schema": {"type": "object", "properties": {
             "profissional": prof,
             "data": {"type": "string", "description": "AAAA-MM-DD"},
             "hora": {"type": "string", "description": "HH:MM, um dos horários livres"},
             "nome_paciente": {"type": "string"},
             "convenio": {"type": "string", "description": "Particular ou o nome do convênio"},
             "observacao": {"type": "string", "description": "Opcional. Motivo ou observação, só se o paciente contou (não pergunte)."}},
             "required": ["profissional", "data", "hora", "nome_paciente"]}},
        {"name": "consultas_do_paciente",
         "description": "Lista as próximas consultas já marcadas para este paciente (deste número de WhatsApp), com o código de cada uma. "
                        "Use antes de remarcar ou cancelar.",
         "input_schema": {"type": "object", "properties": {}}},
        {"name": "remarcar_consulta",
         "description": "Muda uma consulta já marcada deste paciente para outro dia/horário que apareceu como livre e que o paciente escolheu. "
                        "O horário antigo fica livre na hora.",
         "input_schema": {"type": "object", "properties": {
             "consulta": {"type": "string", "description": "Código da consulta, como aparece em consultas_do_paciente."},
             "profissional": {**prof, "description": "Profissional da nova consulta (normalmente o mesmo)."},
             "data": {"type": "string", "description": "Novo dia, AAAA-MM-DD"},
             "hora": {"type": "string", "description": "Nova hora, HH:MM, um dos horários livres"}},
             "required": ["consulta", "profissional", "data", "hora"]}},
        {"name": "cancelar_consulta",
         "description": "Cancela uma consulta já marcada deste paciente, quando ele pediu para cancelar e confirmou qual.",
         "input_schema": {"type": "object", "properties": {
             "consulta": {"type": "string", "description": "Código da consulta, como aparece em consultas_do_paciente."},
             "motivo": {"type": "string", "description": "Opcional. O motivo, só se o paciente contou (não pergunte)."}},
             "required": ["consulta"]}},
    ]


def _data_param(v: str):
    """'2026-11-25', '25/11/2026' ou '25/11' (o proximo 25/11) -> date."""
    v = (v or "").strip()
    try:
        return datetime.strptime(v[:10], "%Y-%m-%d").date()
    except ValueError:
        pass
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?", v)
    if not m:
        return None
    hoje = agora().date()
    try:
        if m.group(3):
            ano = int(m.group(3))
            return datetime(ano + 2000 if ano < 100 else ano, int(m.group(2)), int(m.group(1))).date()
        d = datetime(hoje.year, int(m.group(2)), int(m.group(1))).date()
        return d if d >= hoje else datetime(hoje.year + 1, d.month, d.day).date()
    except ValueError:
        return None


def _hora_param(v: str) -> str:
    """'9:00', '09:00', '9h', '9h30', '14h00', '14:00:00' -> 'HH:MM' (ou '' se nao entender)."""
    m = re.fullmatch(r"(\d{1,2})\s*(?:[:h]\s*(\d{2})?)?(?::\d{2})?\s*h?", (v or "").strip().lower())
    if not m or int(m.group(1)) > 23 or int(m.group(2) or 0) > 59:
        return ""
    return f"{int(m.group(1)):02d}:{int(m.group(2) or 0):02d}"


DIAS_FALA = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]


def dia_falado(d, hoje=None) -> str:
    """Como o dia aparece para o paciente, calculado pelo sistema: 'amanhã, terça 24/11', 'quinta 26/11'."""
    hoje = hoje or agora().date()
    base = f"{DIAS_FALA[d.weekday()]} {d.strftime('%d/%m')}"
    if d == hoje:
        return "hoje, " + base
    if d == hoje + timedelta(days=1):
        return "amanhã, " + base
    return base


def hora_falada(h: str) -> str:
    """'09:00' -> '9h', '14:30' -> '14h30'."""
    hh, mm = h.split(":")
    return f"{int(hh)}h" + ("" if mm == "00" else mm)


def _espalhar(hs: list, n: int) -> list:
    """Escolhe n horarios espalhados (inicio, meio, fim) de uma lista ordenada."""
    if len(hs) <= n:
        return list(hs)
    idx = sorted({round(i * (len(hs) - 1) / (n - 1)) for i in range(n)}) if n > 1 else [0]
    return [hs[i] for i in idx]


def _escolher(hs: list, n: int, periodo: str = "") -> list:
    """Quem pede 'cedo' quer os primeiros; quem pede 'fim do dia'/'noite' quer os ultimos; senao, espalhados."""
    if periodo == "cedo":
        return hs[:n]
    if periodo in ("fim_do_dia", "noite"):
        return hs[-n:]
    return _espalhar(hs, n)


def sugestao_txt(por_prof: list, maximo: int = 3, periodo: str = "") -> str:
    """por_prof: [(prof, {data: [horas]})]. Monta no maximo 'maximo' opcoes no total, ja escritas para o paciente."""
    com = [(p, d, hs) for p, res in por_prof for d, hs in list(res.items())[:1] if hs]
    if not com:
        return ""
    cota = [maximo // len(com) + (1 if i < maximo % len(com) else 0) for i in range(len(com))][:maximo]
    partes = []
    for (p, d, hs), n in zip(com, cota):
        if n:
            escolha = [hora_falada(h) for h in _escolher(hs, n, periodo)]
            horas = escolha[0] if len(escolha) == 1 else ", ".join(escolha[:-1]) + " ou " + escolha[-1]
            partes.append(f"{dia_falado(d)} às {horas} com {nome_conversa(p['nome'])}")
    return ("SUGESTÃO PARA OFERECER (no máximo 3 horários no total; escreva o dia exatamente assim): "
            + "; ".join(partes) + ".")


def _motivos(slug: str, conf: dict, prof: dict, datas: list, sim: bool, periodo: str = "") -> list:
    """[(de, ate, motivo)] para os dias sem horario, agrupando dias seguidos com o mesmo motivo."""
    motivos = []
    for d in datas:
        if periodo and AG.livres_no_dia(slug, conf, prof, d, sim):
            m = "sem horário livre nesse período do dia"
        else:
            m = AG.motivo_sem_horario(slug, conf, prof, d, sim)
        rot = dia_falado(d)
        if motivos and motivos[-1][2] == m and motivos[-1][3] == d - timedelta(days=1):
            motivos[-1][1], motivos[-1][3] = rot, d
        else:
            motivos.append([rot, rot, m, d])
    return [(a, b, m) for a, b, m, _ in motivos]


def _txt_motivos(motivos: list) -> str:
    return "; ".join((a if a == b else f"{a} a {b}") + f": {m}" for a, b, m in motivos)


def _sem_horario(slug: str, conf: dict, prof: dict, ini, dias: int, sim: bool, periodo: str) -> str:
    """Explica por que nao ha horario, dia a dia (agrupando dias com o mesmo motivo)."""
    motivos = _motivos(slug, conf, prof, [ini + timedelta(days=i) for i in range(dias)], sim, periodo)
    partes = [_txt_motivos(motivos)]
    fim = ini + timedelta(days=dias - 1)
    txt = (f"Sem horário livre para {prof['nome']} de {ini.strftime('%d/%m')} a {fim.strftime('%d/%m')}. "
           + "; ".join(partes) + f". {prof['nome']} atende: {AG.resumo_semana(prof)}.")
    if any("não atende à" in m or "não atende ao" in m for _, _, m in motivos):
        txt += f" Não ofereça {prof['nome']} em dia da semana em que ele não atende."
    prox = AG.livres(slug, conf, prof, max(ini + timedelta(days=dias), agora().date()), 14, sim)
    if prox:
        txt += " Próximos horários livres de " + prof["nome"] + ": " + "; ".join(
            f"{dia_falado(d)} [{d.isoformat()}]: " + ", ".join(hs[:8])
            for d, hs in list(prox.items())[:3]) + "."
        txt += "\n" + sugestao_txt([(prof, prox)])
    return txt + "\nExplique o motivo certo ao paciente em uma frase simples e já ofereça essas opções (ou outro profissional)."


def executar_ferramenta(nome: str, ent: dict, ctx: dict) -> str:
    """ctx: slug, conf, tel, sim, reservas (lista onde entram as consultas marcadas nesta resposta)."""
    slug, conf, sim = ctx["slug"], ctx["conf"], ctx["sim"]
    if nome == "consultas_do_paciente":
        itens = AG.consultas_do_tel(slug, ctx["tel"], sim)
        if not itens:
            return "Nenhuma consulta marcada para este paciente."
        hoje = agora().date()
        linhas = []
        for x in itens:
            trava_txt = AG.pode_mudar(conf, x)
            linhas.append(f"- código {x['id']}: {dia_falado(date.fromisoformat(x['data']), hoje)} às {hora_falada(x['inicio'])} "
                          f"com {nome_conversa(x['prof_nome'])}, paciente {x.get('paciente') or 'sem nome'}"
                          + (f" [não pode remarcar/cancelar pelo WhatsApp: {trava_txt}]" if trava_txt else ""))
        return "\n".join(linhas)
    if nome in ("remarcar_consulta", "cancelar_consulta"):
        return _mudar_consulta(nome, ent, ctx)
    if nome == "ver_horarios_livres" and not (ent.get("profissional") or "").strip():
        ctx["_achados"] = []
        partes = [executar_ferramenta(nome, {**ent, "profissional": x["id"]}, ctx) for x in AG.profs_com_agenda(conf)]
        com = [x for x in partes if not x.startswith("Sem horário")]
        sug = sugestao_txt(ctx.pop("_achados", []), periodo=ent.get("periodo") or "")
        tira = lambda t: "\n".join(l for l in t.split("\n") if not l.startswith("SUGESTÃO PARA OFERECER"))
        return "\n\n".join(tira(x) for x in (com or partes)) + ("\n\n" + sug if com and sug else "")
    prof = AG.achar_prof(conf, ent.get("profissional", ""))
    if not prof or not AG.tem_agenda(prof):
        return "Profissional não encontrado na agenda. Use um destes: " + ", ".join(x["nome"] for x in AG.profs_com_agenda(conf))
    hoje = agora().date()
    if nome == "ver_horarios_livres":
        ini = max(_data_param(ent.get("data_inicial")) or hoje, hoje)
        try:
            dias = max(1, min(14, int(ent.get("dias") or 7)))
        except (TypeError, ValueError):
            dias = 7
        faixa = PERIODOS.get(ent.get("periodo") or "", (0, 24 * 60))
        res = AG.livres(slug, conf, prof, ini, dias, sim)
        linhas, achados = [], {}
        for d, hs in res.items():
            hs = [h for h in hs if faixa[0] <= AG.minutos(h) < faixa[1]]
            if not hs:
                continue
            achados[d] = hs
            if dias > 3 and len(hs) > 16:
                mostra = ", ".join(hs[:16]) + f" (e mais {len(hs) - 16} horários até {hs[-1]}; consulte só esse dia para ver todos)"
            else:
                mostra = ", ".join(hs)
            linhas.append(f"{dia_falado(d)} [{d.isoformat()}]: " + mostra)
        if not linhas and res:
            # nada no periodo pedido, mas o dia tem horario: mostra o dia todo (a IA as vezes erra o periodo)
            linhas = [f"Nenhum horário no período pedido (manhã = antes das 12h, almoço = 11h às 14h, tarde = 12h às 18h, "
                      "fim do dia = a partir das 16h, noite = a partir das 18h). Diga isso ao paciente e ofereça o mais próximo. "
                      "Horários livres nesses dias:"]
            linhas += [f"{dia_falado(d)} [{d.isoformat()}]: " + ", ".join(hs) for d, hs in res.items()]
            achados = dict(res)
        if not linhas:
            periodo = ent.get("periodo") if ent.get("periodo") not in (None, "", "qualquer") else ""
            return _sem_horario(slug, conf, prof, ini, dias, sim, periodo)
        faltam = [ini + timedelta(days=i) for i in range(dias)]
        faltam = [d for d in faltam if d not in res and AG.intervalos_do_dia(prof, d.weekday())]
        if faltam:
            linhas.append("Sem horário (dias em que atende): " + _txt_motivos(_motivos(slug, conf, prof, faltam, sim)))
        if ini == hoje:
            linhas.append(f"Agora são {agora().strftime('%H:%M')}: horários de hoje antes disso já passaram "
                          "(não estão ocupados, só passaram). Se o paciente pediu um horário de hoje que já passou, diga isso.")
        if "_achados" in ctx:
            ctx["_achados"].append((prof, achados))
        linhas.append(sugestao_txt([(prof, achados)], periodo=ent.get("periodo") or ""))
        return f"Horários livres de {prof['nome']} (consulta de {AG.duracao(prof)} min):\n" + "\n".join(linhas)
    if nome == "marcar_consulta":
        d = _data_param(ent.get("data"))
        hora = _hora_param(ent.get("hora"))
        if not d or not hora:
            return "Data ou hora em formato inválido. Use AAAA-MM-DD e HH:MM."
        if not (ent.get("nome_paciente") or "").strip():
            return "Falta o nome do paciente. Pergunte antes de marcar."
        ok, r = AG.reservar(slug, conf, prof, d, hora, {
            "paciente": ent.get("nome_paciente"), "tel": ctx["tel"], "convenio": ent.get("convenio"),
            "obs": ent.get("observacao")}, sim=sim, por="assistente", origem="assistente")
        if not ok:
            if not AG.livres_no_dia(slug, conf, prof, d, sim):
                r = f"{r} Motivo: {AG.motivo_sem_horario(slug, conf, prof, d, sim)}."
            alt = AG.livres(slug, conf, prof, max(d, hoje), 3, sim)
            sug = "; ".join(f"{AG.DIAS_CURTOS[x.weekday()]} {x.strftime('%d/%m')}: " + ", ".join(hs[:6]) for x, hs in alt.items())
            return f"Não marcado: {r}" + (f" Livres perto disso: {sug}" if sug else "")
        ctx["reservas"].append(r)
        return f"Consulta marcada: {AG.quando_txt(r)} com {r['prof_nome']}, paciente {r['paciente']}."
    return "Ferramenta desconhecida."


def _consulta_do_paciente(ctx: dict, codigo: str):
    codigo = (codigo or "").strip()
    for x in AG.consultas_do_tel(ctx["slug"], ctx["tel"], ctx["sim"]):
        if x["id"] == codigo:
            return x
    return None


def _mudar_consulta(nome: str, ent: dict, ctx: dict) -> str:
    slug, conf, sim = ctx["slug"], ctx["conf"], ctx["sim"]
    it = _consulta_do_paciente(ctx, ent.get("consulta"))
    if not it:
        return "Consulta não encontrada para este paciente. Use consultas_do_paciente para ver os códigos."
    trava_txt = AG.pode_mudar(conf, it)
    if trava_txt:
        return (f"Não alterado: {trava_txt} Diga isso ao paciente com gentileza e passe para a equipe "
                "(inclua a linha [ENCAMINHAR: ...]).")
    antes = f"{AG.quando_txt(it)} com {it['prof_nome']}"
    if nome == "cancelar_consulta":
        novo = AG.cancelar(slug, it["id"], por="assistente")
        if not novo:
            return "Não foi possível cancelar agora. Passe para a equipe."
        if (ent.get("motivo") or "").strip():
            novo["motivo_cancelamento"] = ent["motivo"].strip()[:300]
            AG._gravar(slug, novo)
        ctx.setdefault("mudancas", []).append({"acao": "cancelada", "item": novo, "antes": antes, "motivo": ent.get("motivo") or ""})
        return f"Consulta cancelada: {antes}. Confirme ao paciente e, se fizer sentido, ofereça remarcar."
    prof = AG.achar_prof(conf, ent.get("profissional", "")) or AG.achar_prof(conf, it["prof_id"])
    if not prof or not AG.tem_agenda(prof):
        return "Profissional não encontrado na agenda. Use um destes: " + ", ".join(x["nome"] for x in AG.profs_com_agenda(conf))
    d = _data_param(ent.get("data"))
    hora = _hora_param(ent.get("hora"))
    if not d or not hora:
        return "Data ou hora em formato inválido. Use AAAA-MM-DD e HH:MM."
    ok, r = AG.mover(slug, conf, it["id"], prof, d, hora, por="assistente")
    if not ok:
        alt = AG.livres(slug, conf, prof, max(d, agora().date()), 3, sim)
        sug = "; ".join(f"{dia_falado(x)}: " + ", ".join(hs[:6]) for x, hs in alt.items())
        return f"Não remarcado: {r}" + (f" Livres perto disso: {sug}" if sug else "")
    ctx.setdefault("mudancas", []).append({"acao": "remarcada", "item": r, "antes": antes})
    return (f"Consulta remarcada: era {antes}; agora {dia_falado(d)} às {hora_falada(r['inicio'])} com "
            f"{nome_conversa(r['prof_nome'])}. O horário antigo foi liberado. Confirme ao paciente.")


def _bloco_para_param(b) -> dict:
    if getattr(b, "type", "") == "tool_use":
        return {"type": "tool_use", "id": b.id, "name": b.name, "input": b.input}
    return {"type": "text", "text": getattr(b, "text", "")}


# ------------------------------------------------------------
# Revisao automatica da resposta antes de enviar
# ------------------------------------------------------------
_RE_DIA_DATA = re.compile(r"\b(segunda|ter[çc]a|quarta|quinta|sexta|s[áa]bado|domingo)(-feira)?(,?\s+(?:dia\s+)?)(\d{1,2})/(\d{1,2})\b", re.I)
_RE_DIA_REPETIDO = re.compile(r"\b(segunda|ter[çc]a|quarta|quinta|sexta|s[áa]bado|domingo)(-feira)?,?\s+\1(-feira)?\b", re.I)
_RE_DATA = re.compile(r"\b(\d{1,2})/(\d{1,2})\b")
_RE_HORA = re.compile(r"\b([01]?\d|2[0-3])(?:h(\d{2})?|:([0-5]\d))(?![\d/])")
_RE_RELATIVO = re.compile(r"\b(depois de amanh[ãa]|amanh[ãa]|hoje)\b", re.I)
_RE_SEMANA = re.compile(r"\b(semana que vem|pr[óo]xima semana|n?esta semana|n?essa semana)\b", re.I)
_RE_META = re.compile(r"(pela revis[ãa]o|revis[ãa]o (autom[áa]tica|interna)|nota interna|mensagem anterior|desculp\w* (pela|a) confus[ãa]o|corrigindo|o limite [ée]|a regra de|ferramenta|marcar_consulta|ver_horarios)", re.I)
_RE_LICENCA = re.compile(r"\b(posso (?:te |lhe )?(marcar|remarcar|cancelar|desmarcar|agendar|verificar|ver|confirmar|consultar|oferecer|sugerir|passar)|quer que eu|deseja que eu|gostaria que eu)\b", re.I)


def _data_de(dd: int, mm: int, hoje):
    """DD/MM -> date, no ano que deixa a data mais perto de hoje (dezembro/janeiro)."""
    try:
        cands = [date(hoje.year + k, mm, dd) for k in (-1, 0, 1)]
    except ValueError:
        return None
    return min(cands, key=lambda d: abs((d - hoje).days))


_RE_SAUDACAO = re.compile(r"\b(bom dia|boa tarde|boa noite)\b", re.I)
_DIA_IDX = {"segunda": 0, "terça": 1, "terca": 1, "quarta": 2, "quinta": 3, "sexta": 4, "sábado": 5, "sabado": 5, "domingo": 6}
_RE_DIA = re.compile(r"\b(segunda|ter[çc]a|quarta|quinta|sexta|s[áa]bado|domingo)s?(?:-feiras?)?\b", re.I)
_RE_FAIXA_DIAS = re.compile(r"\bde (segunda|ter[çc]a|quarta|quinta|sexta|s[áa]bado|domingo)(?:-feira)? a "
                            r"(segunda|ter[çc]a|quarta|quinta|sexta|s[áa]bado|domingo)(?:-feira)?\b", re.I)


def saudacao_certa(hora: int) -> str:
    return "bom dia" if hora < 12 else ("boa tarde" if hora < 18 else "boa noite")


def _dias_ditos(trecho: str) -> set:
    dias = set()
    for a, b in _RE_FAIXA_DIAS.findall(trecho):
        i, f = _DIA_IDX[a.lower()], _DIA_IDX[b.lower()]
        dias |= set(range(i, f + 1)) if i <= f else set()
    sem_faixa = _RE_FAIXA_DIAS.sub(" ", trecho)
    dias |= {_DIA_IDX[m.lower()] for m in _RE_DIA.findall(sem_faixa)}
    return dias


def _confere_dias_de_atendimento(fala: str, conf: dict) -> list:
    """'o Dr. Fabio atende de segunda a quinta' quando ele atende seg, ter e qui -> problema."""
    profs = [(nome_conversa(x["nome"]), x) for x in AG.profs_com_agenda(conf)]
    problemas, ultimo = [], None
    for trecho in re.split(r"(?<!Dr)(?<!Dra)[.!?](?=\s|$)|[;\n]|,\s+(?=(?:e |mas )?(?:ele|ela|o dr|a dra))", fala):
        citados = [(n, p) for n, p in profs if n and n.lower() in trecho.lower()]
        if citados:
            ultimo = citados[-1]
        if not re.search(r"\batende\b", trecho, re.I) or re.search(r"\bn[ãa]o atende\b", trecho, re.I) or not ultimo:
            continue
        nome, p = ultimo
        dias = _dias_ditos(trecho)
        fora = sorted(d for d in dias if not AG.intervalos_do_dia(p, d))
        if fora:
            problemas.append(f"disse que {nome} atende {DIAS_FALA[fora[0]]}, mas {nome} atende só: {AG.resumo_semana(p)}; "
                             "escreva os dias exatamente")
    return problemas


def revisar_resposta(texto: str, hoje=None, conf=None, hora=None, ja_marcadas=()) -> tuple:
    """Confere a resposta antes de mandar ao paciente.
    Corrige sozinho o que da para corrigir com certeza (dia da semana errado ao lado de uma data) e
    devolve (texto_corrigido, problemas) com o que a IA precisa reescrever."""
    hoje = hoje or agora().date()
    linhas = texto.split("\n")
    fala = "\n".join(l for l in linhas if not l.strip().startswith("["))   # as linhas [AGENDAMENTO]/[ENCAMINHAR] nao vao ao paciente

    def corrige_dia(m):
        d = _data_de(int(m.group(4)), int(m.group(5)), hoje)
        if not d:
            return m.group(0)
        certo = DIAS_FALA[d.weekday()]
        escrito = m.group(1)
        if escrito.lower().replace("c", "ç").replace("sabado", "sábado") == certo or escrito.lower() == certo:
            return m.group(0)
        certo = certo[0].upper() + certo[1:] if escrito[0].isupper() else certo
        return certo + (m.group(2) or "") + m.group(3) + m.group(4) + "/" + m.group(5)
    texto = _RE_DIA_DATA.sub(corrige_dia, texto)
    certa = saudacao_certa(agora().hour if hora is None else hora)   # cumprimenta pelo relogio, nao pelo paciente
    texto = _RE_SAUDACAO.sub(lambda m: certa[0].upper() + certa[1:] if m.group(1)[0].isupper() else certa, texto)
    for x in (conf or {}).get("profissionais", []):   # nome do cadastro -> nome de conversa ("Dra. Ana Souza" -> "Dra. Ana")
        curto = nome_conversa(x.get("nome", ""))
        if curto and x.get("nome") and curto != x["nome"].strip():
            texto = re.sub(re.escape(x["nome"].strip()), curto, texto, flags=re.I)
    texto = _RE_DIA_REPETIDO.sub(lambda m: m.group(1) + (m.group(2) or ""), texto)   # "quarta, quarta 25/11" -> "quarta 25/11"
    fala = "\n".join(l for l in texto.split("\n") if not l.strip().startswith("["))

    problemas = []
    for frase in re.split(r"(?<!Dr\.)(?<!Dra\.)(?<=[.!?])\s+|\n", fala):
        datas = [d for d in (_data_de(int(a), int(b), hoje) for a, b in _RE_DATA.findall(frase)) if d]
        for m in _RE_RELATIVO.finditer(frase):
            palavra = m.group(1).lower().replace("amanha", "amanhã")
            alvo = {"hoje": 0, "amanhã": 1, "depois de amanhã": 2}[palavra]
            perto = [d for d in (_data_de(int(a), int(b), hoje) for a, b in _RE_DATA.findall(frase[m.end():m.end() + 40])) if d]
            if perto and perto[0] != hoje + timedelta(days=alvo):
                problemas.append(f"chamou {perto[0].strftime('%d/%m')} de \"{palavra}\", mas {palavra} é "
                                 f"{dia_falado(hoje + timedelta(days=alvo), hoje).split(', ')[-1]}")
        for m in _RE_SEMANA.finditer(frase):
            seg = hoje - timedelta(days=hoje.weekday())
            ini, fim = (seg + timedelta(days=7), seg + timedelta(days=13)) if "vem" in m.group(1).lower() or "próx" in m.group(1).lower() \
                else (hoje, seg + timedelta(days=6))
            fora = [d for d in datas if not ini <= d <= fim]
            if not datas:
                problemas.append(f"disse \"{m.group(1)}\" sem data; diga o dia e a data, sem falar de semana")
            elif fora:
                problemas.append(f"disse \"{m.group(1)}\" para {fora[0].strftime('%d/%m')}, que não é {m.group(1)} "
                                 f"({ini.strftime('%d/%m')} a {fim.strftime('%d/%m')}); escreva só o dia e a data")
    if conf:
        problemas += _confere_dias_de_atendimento(fala, conf)
    horas = {(int(a), int(b or c or 0)) for a, b, c in _RE_HORA.findall(fala)} - set(ja_marcadas)
    if len(horas) > 3:   # o horario da consulta que o paciente ja tem (remarcar) nao conta como oferta
        problemas.append(f"ofereceu {len(horas)} horários; ofereça no máximo 3 no total")
    if fala.count("?") > 1:
        problemas.append("fez mais de uma pergunta; deixe só uma")
    if "(" in fala or re.search(r"\bsobrenome\b", fala, re.I):
        problemas.append("copiou dados do cadastro (parênteses ou 'Sobrenome'); escreva como numa conversa")
    if _RE_LICENCA.search(fala):
        problemas.append("pediu licença ou confirmação (\"posso...\", \"quer que eu...\"); faça direto: consulte, ofereça ou marque")
    for frase in re.split(r"(?<!Dr\.)(?<!Dra\.)(?<=[.!?])\s+|\n", fala):
        if _RE_HORA.search(frase) and re.search(r"\b(hoje|amanh[ãa])\b", frase, re.I) and not _RE_DATA.search(frase):
            problemas.append("ofereceu horário de hoje/amanhã sem a data; escreva o dia como a agenda escreve (ex.: hoje, segunda 23/11)")
            break
    if not RE_ENC.search(texto) and not RE_AG.search(texto) and re.search(
            r"\b(vou passar|vou anotar|anotei|passei)\b.{0,80}\b(recep[çc][ãa]o|equipe)\b", fala, re.I):
        problemas.append("disse que vai passar para a recepção/equipe mas faltou a linha [AGENDAMENTO: ...] "
                         "(pedido de horário) ou [ENCAMINHAR: ...] no fim da mensagem")
    if RE_ENC.search(texto):
        if "?" in fala:
            problemas.append("ao passar para a equipe, não faça perguntas nem siga o atendimento: só avise que a equipe continua")
        if not re.search(r"\bpor aqui\b", fala, re.I):
            # aviso obrigatorio ao encaminhar: entra sozinho, antes da linha [ENCAMINHAR]
            m = RE_ENC.search(texto)
            texto = (texto[:m.start()].rstrip() + "\nAlguém da equipe continua a conversa com você por aqui.\n"
                     + texto[m.start():])
    return texto, problemas


# ============================================================
# ETAPAS DA CONVERSA E CONFERENTE
# O sistema decide qual e o proximo dado que falta (uma coisa por vez, sempre na mesma ordem)
# e uma segunda IA, barata, confere a resposta antes de ela sair.
# ============================================================
MODELO_CONFERENTE = os.environ.get("CLINICA_MODELO_CONFERENTE", "claude-haiku-5-5")
CONFERENTE_LIGADO = os.environ.get("CLINICA_CONFERENTE", "1") != "0"

# (campo, o que e, o que pedir)
ETAPAS = [
    ("motivo", "o que o paciente precisa",
     "pergunte o que ele precisa, de forma aberta (ex.: \"Claro! Me conta, é para quê?\"); não ofereça horários nesta mensagem"),
    ("profissional", "o profissional",
     "diga quem atende o caso dele (pelo nome de conversa, só quem a especialidade cobre); se for mais de um, pergunte se tem "
     "preferência; se for só um, não pergunte preferência: diga quem é e pergunte qual dia e período fica melhor"),
    ("quando", "o dia e o período",
     "pergunte qual dia e período fica melhor para ele"),
    ("horario", "o horário",
     "consulte a agenda (ver_horarios_livres) e ofereça até 3 opções concretas, com dia, data e hora; pergunte qual prefere"),
    ("nome", "o nome do paciente",
     "pergunte o nome completo dele"),
    ("convenio", "particular ou convênio",
     "pergunte se é particular ou por convênio (e qual)"),
]


def _texto_msg(m: dict) -> str:
    c = m.get("content")
    if isinstance(c, str):
        return c
    return " ".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")


def _json_de(txt: str) -> dict:
    m = re.search(r"\{.*\}", txt or "", re.S)
    if not m:
        return {}
    try:
        v = json.loads(m.group(0))
        return v if isinstance(v, dict) else {}
    except ValueError:
        return {}


def _ia_aux(pedido: str, chave_tokens: str, max_tokens: int = 400) -> dict:
    kw = {"extra_body": {"thinking": {"type": "disabled"}}} if re.match(r"claude-(haiku|sonnet|opus)-5", MODELO_CONFERENTE) else {}
    resp = claude().messages.create(model=MODELO_CONFERENTE, max_tokens=max_tokens,
                                    messages=[{"role": "user", "content": pedido}], **kw)
    try:
        R.hincrby(chave_tokens, "aux_entrada", resp.usage.input_tokens)
        R.hincrby(chave_tokens, "aux_saida", resp.usage.output_tokens)
    except Exception:
        pass
    return _json_de("".join(getattr(b, "text", "") for b in resp.content))


def _conversa_txt(msgs: list, n: int = 14) -> str:
    linhas = []
    for m in msgs[-n:]:
        t = _texto_msg(m).strip()
        if t:
            linhas.append(("Paciente: " if m["role"] == "user" else "Clínica: ") + t)
    return "\n".join(linhas)


def ler_estado(msgs: list, conf: dict, chave_tokens: str) -> dict:
    """Le a conversa e diz o que o paciente ja informou e o que perguntou na ultima mensagem."""
    profs = ", ".join(x["nome"] for x in conf.get("profissionais", []))
    pedido = (
        "[ETAPAS] Você lê uma conversa de WhatsApp entre um paciente e a recepção de uma clínica e anota o que o paciente "
        f"já informou para marcar uma consulta. Profissionais da clínica: {profs or '-'}.\n\n"
        f"CONVERSA:\n{_conversa_txt(msgs)}\n\n"
        "Responda só com JSON, assim:\n"
        '{"motivo": "...", "profissional": "...", "quando": "...", "horario": "...", "horario_confirmado": false, "nome": "...", "convenio": "...", '
        '"perguntas": ["..."], "fora_do_agendamento": false, "ja_marcado": false}\n'
        "- Use null no que o paciente ainda não disse. Só anote o que o PACIENTE disse ou aceitou, não o que a clínica ofereceu.\n"
        "- motivo: o que ele precisa (dor, limpeza, aparelho...). profissional: o nome que ele escolheu, ou \"tanto faz\".\n"
        "- quando: o dia e/ou período que ele prefere (ex.: \"amanhã à tarde\", \"quinta\", \"qualquer dia\").\n"
        "- horario: só se ele escolheu ou pediu um horário exato (ex.: \"14h\"). horario_confirmado: true só se a clínica "
        "já disse que esse horário está livre (ou o ofereceu) e ele escolheu; false se ele só pediu.\n"
        "- convenio: \"particular\" ou o nome do convênio que ele disse que vai usar.\n"
        "- perguntas: o que a ÚLTIMA mensagem do paciente pergunta ou pede que precisa de resposta direta "
        "(ex.: \"atende Amil?\", \"tem às 9h hoje?\", \"quanto custa?\"). Lista vazia se não perguntou nada.\n"
        "- fora_do_agendamento: true se ele quer outra coisa que não marcar uma consulta nova (remarcar, cancelar, falar com uma pessoa, "
        "urgência, reclamação, só uma informação).\n"
        "- ja_marcado: true se a clínica já confirmou a consulta marcada nesta conversa e ele não pediu outra.")
    return _ia_aux(pedido, chave_tokens)


def _tem(v) -> bool:
    return bool(v) and str(v).strip().lower() not in ("null", "none", "-", "")


def _prof_citado(nome: str, conf: dict):
    """Acha o profissional que o paciente citou. Devolve o dict, None (nao existe na clinica) ou "" (tanto faz)."""
    n = slugificar(str(nome)).replace("-", " ")
    if re.search(r"\b(tanto faz|qualquer|nenhum|sem preferencia)\b", n):
        return ""
    palavras = {w for w in n.split() if w not in ("dr", "dra", "doutor", "doutora") and len(w) > 2}
    for x in conf.get("profissionais", []):
        if palavras & set(slugificar(x["nome"]).split("-")):
            return x
    return None


def proxima_etapa(estado: dict, conf: dict) -> str:
    """Decide o que esta resposta deve fazer. Devolve o texto que vai para a IA (vazio se nao ha o que dirigir)."""
    if not estado:
        return ""
    com_agenda = AG.profs_com_agenda(conf)
    profs = conf.get("profissionais", [])
    perguntas = [str(x).strip() for x in (estado.get("perguntas") or []) if str(x).strip()]
    linhas = []
    if perguntas:
        linhas.append("1) Antes de tudo, responda com clareza o que o paciente perguntou: " + "; ".join(perguntas)
                      + ". (Se é convênio que não é aceito, diga que não atende; se é horário que já passou, diga que já passou; "
                      "se é dia sem atendimento, diga isso.)")
    prof = None
    if _tem(estado.get("profissional")):
        prof = _prof_citado(estado["profissional"], conf)
        if prof is None and profs:
            linhas.append(f"{len(linhas) + 1}) O paciente pediu \"{estado['profissional']}\", que não trabalha na clínica: "
                          "diga que não há ninguém com esse nome aqui.")
            estado = {**estado, "profissional": None}
    if estado.get("fora_do_agendamento") or estado.get("ja_marcado"):
        if not linhas:
            return ""
        return "\n\nETAPA DESTA RESPOSTA (decidida pelo sistema):\n" + "\n".join(linhas)
    falta = None
    for campo, _, pedir in ETAPAS:
        if campo in ("motivo", "profissional") and len(profs) <= 1:
            continue
        if campo == "motivo" and _tem(estado.get("profissional")):
            continue
        if campo == "horario" and (not com_agenda or (prof and not AG.tem_agenda(prof))):
            continue
        if campo == "horario" and _tem(estado.get("horario")) and not estado.get("horario_confirmado"):
            falta = (campo, f"consulte a agenda para o horário que ele pediu ({estado['horario']}): se estiver livre, siga "
                            "(e se já tiver nome e particular/convênio, marque direto com marcar_consulta); se não estiver "
                            "(ocupado, já passou, dia sem atendimento), diga isso e ofereça até 3 opções concretas, "
                            "com dia, data e hora; pergunte qual prefere")
            break
        if not _tem(estado.get(campo)):
            falta = (campo, pedir)
            break
    n = len(linhas) + 1
    if falta is None:
        if com_agenda and not (prof and not AG.tem_agenda(prof)):
            linhas.append(f"{n}) O paciente já deu tudo: chame marcar_consulta agora (se o horário estiver livre) e confirme "
                          "dia, data, hora, profissional e endereço. Não pergunte mais nada.")
        else:
            linhas.append(f"{n}) O paciente já deu tudo: diga que vai passar o pedido para a recepção confirmar o horário "
                          "e inclua a linha [AGENDAMENTO: ...].")
    else:
        campo, pedir = falta
        if campo == "profissional" and _tem(estado.get("quando")) and com_agenda:
            pedir = ("consulte a agenda no dia e período que ele pediu e ofereça até 3 horários no total entre os profissionais "
                     "que atendem o caso dele; pergunte só \"Qual prefere?\"")
        linhas.append(f"{n}) Depois, {pedir}. Esta é a única pergunta da mensagem: não peça mais nada.")
    ja = [f"{rot}: {estado[c]}" for c, rot, _ in ETAPAS if _tem(estado.get(c))]
    return ("\n\nETAPA DESTA RESPOSTA (decidida pelo sistema; siga):\n" + "\n".join(linhas)
            + ("\nO paciente já informou: " + "; ".join(ja) + ". Não pergunte isso de novo." if ja else ""))


def conferir_resposta(etapa: str, msgs: list, agenda: list, rascunho: str, chave_tokens: str) -> list:
    """Segunda IA, barata: confere se a resposta faz o que a etapa pediu e nao contradiz a agenda."""
    pedido = (
        "[CONFERENTE] Você confere a mensagem que a recepção de uma clínica vai mandar a um paciente no WhatsApp, "
        "antes de ela sair. Aponte só falhas claras; na dúvida, não aponte.\n\n"
        f"CONVERSA ATÉ AGORA:\n{_conversa_txt(msgs, 8)}\n\n"
        + (f"O QUE A AGENDA RESPONDEU AGORA:\n" + "\n".join(agenda)[:12000] + "\n\n" if agenda else "")
        + (f"O QUE ESTA MENSAGEM DEVIA FAZER:{etapa}\n\n" if etapa else "")
        + f"MENSAGEM A CONFERIR:\n{rascunho}\n\n"
        "Confira:\n"
        "1) Se o paciente perguntou algo na última mensagem (um convênio, um horário, um dia), a mensagem responde isso com clareza? "
        "(Convênio não aceito: tem que dizer que não atende. Horário que já passou: tem que dizer que já passou. "
        "Dia sem atendimento: tem que dizer.)\n"
        "2) A mensagem faz o passo pedido acima, sem pular para outro e sem pedir coisa que o paciente já disse?\n"
        "3) Algum dia, data, hora, profissional ou convênio contradiz o que a agenda respondeu?\n"
        "Passar para a equipe (linha [ENCAMINHAR: ...]) o que não está nas informações da clínica, como preço, já é responder: "
        "não peça para dar a informação. "
        "Só aponte contradição com a agenda quando a agenda disser o contrário; se a agenda não fala do assunto, não aponte. "
        "Nunca peça para tirar uma informação verdadeira nem a resposta a algo que o paciente disse ou perguntou "
        "(ex.: dizer que um profissional não existe na clínica, que um horário já passou, que não há agenda para alguém).\n"
        "Responda só com JSON: {\"problemas\": [\"...\"]} (lista vazia se está bom). Cada problema em uma frase curta, "
        "dizendo o que corrigir.")
    v = _ia_aux(pedido, chave_tokens, 300)
    return [str(x) for x in (v.get("problemas") or []) if str(x).strip()]


def _horas_ja_marcadas(ctx: dict, agenda: list) -> set:
    """Horarios das consultas que o paciente ja tem (ou tinha, antes de remarcar/cancelar): citar nao e oferecer."""
    out = set()
    try:
        for x in AG.consultas_do_tel(ctx["slug"], ctx.get("tel", ""), ctx.get("sim", False)):
            out.add((AG.minutos(x["inicio"]) // 60, AG.minutos(x["inicio"]) % 60))
    except Exception:
        pass
    for h, m in re.findall(r"às (\d{1,2})(?:h|:)(\d{2})?", "\n".join(
            x for x in agenda if x.startswith(("remarcar_consulta", "cancelar_consulta")))):
        out.add((int(h), int(m or 0)))
    return out


async def conversar_ia(system: str, msgs: list, ctx: dict, chave_tokens: str) -> str:
    """Chama a IA com as ferramentas da agenda (se a clinica tiver agenda) e devolve o texto final."""
    tools = ferramentas_agenda(ctx["conf"])
    msgs_orig, msgs = msgs, list(msgs)
    textos, revisoes, rascunho, agenda = [], 0, "", []
    etapa = ""
    if CONFERENTE_LIGADO:
        try:
            ctx["estado"] = await asyncio.to_thread(ler_estado, msgs, ctx["conf"], chave_tokens)
            etapa = proxima_etapa(ctx["estado"], ctx["conf"])
        except Exception as e:
            print(f"CLINICA {ctx['slug']} etapas erro: {e}")
    ctx["etapa"] = etapa
    system = system + etapa
    for _ in range(8):
        kw = {"tools": tools} if tools else {}
        if _MODELO_NOVO:
            # modelos da familia 5 pensam por padrao; no WhatsApp a resposta e curta, entao desligamos
            kw["extra_body"] = {"thinking": {"type": "disabled"}}
        resp = await asyncio.to_thread(
            claude().messages.create, model=MODELO, max_tokens=700, system=system, messages=msgs, **kw)
        try:
            R.hincrby(chave_tokens, "entrada", resp.usage.input_tokens)
            R.hincrby(chave_tokens, "saida", resp.usage.output_tokens)
        except Exception:
            pass
        texto = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text").strip()
        usos = [b for b in resp.content if getattr(b, "type", "") == "tool_use"]
        if resp.stop_reason != "tool_use" or not usos:
            final = texto or "\n".join(textos)
            if revisoes and (_RE_META.search(final) or not final):
                # a reescrita falou do sistema em vez de falar com o paciente: fica com o rascunho anterior
                print(f"CLINICA {ctx['slug']} reescrita descartada: {final[:120]!r}")
                return rascunho
            if rascunho and RE_ENC.search(rascunho) and not RE_ENC.search(final):
                # a reescrita perdeu o encaminhamento para a equipe: fica com o rascunho que encaminhava
                print(f"CLINICA {ctx['slug']} reescrita sem encaminhamento descartada: {final[:120]!r}")
                return rascunho
            marcadas = _horas_ja_marcadas(ctx, agenda)
            final, problemas = revisar_resposta(final, conf=ctx.get("conf"), ja_marcadas=marcadas)
            rascunho = final
            if not problemas and CONFERENTE_LIGADO and revisoes < 2 and final:
                try:
                    problemas = await asyncio.to_thread(conferir_resposta, etapa, msgs_orig, agenda, final, chave_tokens)
                except Exception as e:
                    print(f"CLINICA {ctx['slug']} conferente erro: {e}")
            if problemas and revisoes < 2 and final:
                revisoes += 1
                print(f"CLINICA {ctx['slug']} revisao {revisoes}: {problemas}")
                ctx.setdefault("revisoes", []).append(problemas)
                msgs.append({"role": "assistant", "content": resp.content and [_bloco_para_param(b) for b in resp.content] or final})
                msgs.append({"role": "user", "content": "[nota interna do sistema, o paciente não vê isto] Sua última mensagem não pode ser "
                             "enviada assim: " + "; ".join(problemas) + ". Corrija: se o paciente já deu o horário, o nome e se é particular ou "
                             "convênio, chame marcar_consulta agora e confirme; se precisar, consulte a agenda de novo. Depois escreva "
                             "a mensagem inteira para o paciente, corrigida, mantendo o que estava certo (e as linhas "
                             "[AGENDAMENTO]/[ENCAMINHAR], se houver). Responda só com a mensagem para o paciente: não comente esta nota, "
                             "não fale de regras, revisão nem limite, e não peça desculpas nem diga que corrigiu: o paciente "
                             "não viu a versão anterior."})
                textos = []
                continue
            return final
        if texto:
            textos.append(texto)
        msgs.append({"role": "assistant", "content": [_bloco_para_param(b) for b in resp.content]})
        resultados = []
        for b in usos:
            try:
                out = executar_ferramenta(b.name, b.input or {}, ctx)
            except Exception as e:
                print(f"CLINICA {ctx['slug']} ferramenta {b.name} erro: {e}")
                out = "Erro ao consultar a agenda agora. Diga ao paciente que a equipe vai confirmar o horário e encaminhe."
            agenda.append(f"{b.name}({json.dumps(b.input or {}, ensure_ascii=False)}) -> {out}")
            resultados.append({"type": "tool_result", "tool_use_id": b.id, "content": out})
        msgs.append({"role": "user", "content": resultados})
    return "\n".join(textos)


def aviso_consulta(it: dict, tel: str) -> str:
    return (f"Consulta marcada pelo assistente.\nPaciente: {it.get('paciente') or fmt_tel(tel)} ({fmt_tel(tel)})\n"
            f"Com: {it['prof_nome']}\nQuando: {AG.quando_txt(it)}\nConvênio: {it.get('convenio') or '-'}\n"
            f"Ver na agenda: {BASE_URL}/painel/agenda?d={it['data']}")


def aviso_mudanca(m: dict, tel: str) -> str:
    it = m["item"]
    if m["acao"] == "remarcada":
        return (f"Consulta remarcada pelo assistente.\nPaciente: {it.get('paciente') or fmt_tel(tel)} ({fmt_tel(tel)})\n"
                f"Era: {m['antes']}\nAgora: {AG.quando_txt(it)} com {it['prof_nome']}\n"
                f"Ver na agenda: {BASE_URL}/painel/agenda?d={it['data']}")
    return (f"Consulta cancelada pelo assistente.\nPaciente: {it.get('paciente') or fmt_tel(tel)} ({fmt_tel(tel)})\n"
            f"Era: {m['antes']}" + (f"\nMotivo: {m['motivo']}" if m.get("motivo") else "")
            + "\nO horário ficou livre na agenda.")


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
    conf = obter_conf(slug)
    system = montar_prompt(c, conf) + contexto_tempo(conf) + (
        f"\nNome do paciente no WhatsApp: {info['nome']}" if info.get("nome") else "")
    ctx = {"slug": slug, "conf": conf, "tel": tel, "sim": False, "reservas": []}
    bruto = await conversar_ia(system, msgs, ctx, K(slug, "tokens", dt.strftime("%Y-%m")))
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
    for it in ctx["reservas"]:
        contar(slug, "agendamentos")
        await avisar_recepcao(c, aviso_consulta(it, tel))
    for m in ctx.get("mudancas", []):
        contar(slug, "remarcacoes" if m["acao"] == "remarcada" else "cancelamentos")
        await avisar_recepcao(c, aviso_mudanca(m, tel))
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
:root{--tinta:#10203A;--tinta2:#2B4470;--lilas:#C7B8FF;--lilas2:#EDE8FF;--papel:#F4F6F9;--txt2:#4A5568;--linha:#D7DDE6;--linha2:#E6EAF0;--branco:#fff;--alerta:#B42318;--alerta-bg:#FDECEA;--ok:#1F4E8C;--ok-bg:#E6EEF8;
  --raio:16px;--sombra:0 1px 2px rgba(16,32,58,.05),0 4px 16px rgba(16,32,58,.05);--lateral:232px;--baixo:64px}
*{box-sizing:border-box;margin:0;padding:0}
html{-webkit-text-size-adjust:100%}
body{font-family:'Instrument Sans',system-ui,sans-serif;background:var(--papel);color:var(--tinta);font-size:15px;line-height:1.5}
a{color:var(--tinta)}a:hover{color:var(--tinta2)}
h1,h2,h3{font-family:'Bricolage Grotesque',system-ui,sans-serif;letter-spacing:-.015em;line-height:1.15}
.mono{font-family:'JetBrains Mono',ui-monospace,monospace}
.rot{font-family:'JetBrains Mono',ui-monospace,monospace;font-size:11.5px;letter-spacing:.06em;text-transform:uppercase;color:var(--txt2)}
.logo{display:flex;align-items:center;gap:10px;text-decoration:none;color:inherit;font-family:'Bricolage Grotesque',sans-serif;font-size:19px;letter-spacing:-.03em;line-height:1}
.logo b{font-weight:800}.logo span{font-weight:500}
/* ---------- estrutura: lateral no computador, barra embaixo no celular ---------- */
.lateral{position:fixed;inset:0 auto 0 0;width:var(--lateral);background:var(--tinta);color:#fff;display:flex;flex-direction:column;padding:22px 14px;gap:22px;z-index:20}
.lateral .clin{display:flex;align-items:center;gap:12px;padding:10px;border-radius:14px;background:rgba(255,255,255,.06);text-decoration:none;color:#fff}
.lateral .clin b{font-size:14.5px;line-height:1.25;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.clogo{width:44px;height:44px;border-radius:12px;flex:none;background:#fff;display:flex;align-items:center;justify-content:center;overflow:hidden;font-family:'Bricolage Grotesque',sans-serif;font-weight:700;font-size:16px;color:var(--tinta)}
.clogo img{width:100%;height:100%;object-fit:contain}
.clogo.sem{background:var(--lilas)}
.logo-prev .clogo{width:72px;height:72px;border-radius:18px;font-size:24px;border:1px solid var(--linha)}
.lateral .assina{display:flex;align-items:center;gap:8px;padding:0 10px;font-size:12px;color:#8F9BB0;text-decoration:none}
.lateral .assina b{color:#C9D2E0;font-weight:600}
.lateral nav{display:flex;flex-direction:column;gap:2px;flex:1}
.lateral nav a{display:flex;align-items:center;gap:12px;color:#C9D2E0;text-decoration:none;padding:9px 10px;border-radius:10px;font-size:14.5px}
.lateral nav a:hover{background:rgba(255,255,255,.06);color:#fff}
.lateral nav a.ativo{background:rgba(199,184,255,.14);color:#fff}
.lateral nav a.ativo svg{color:var(--lilas)}
.lateral nav .sep{flex:1}
.lateral svg,.baixo svg{width:20px;height:20px;flex:none}
.topo{display:none}
.baixo{display:none}
.app main{margin-left:var(--lateral);padding:32px 40px 48px;max-width:calc(var(--lateral) + 1040px)}
.sem-app main{max-width:980px;margin:24px auto;padding:0 16px}
@media(max-width:899px){
  .lateral{display:none}
  .topo{display:flex;align-items:center;gap:10px;position:sticky;top:0;z-index:20;background:var(--tinta);color:#fff;padding:12px 16px}
  .topo .clin{display:flex;align-items:center;gap:10px;color:#fff;text-decoration:none;min-width:0;flex:1}
  .topo .clin b{font-size:15px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .topo .clogo{width:34px;height:34px;border-radius:9px;font-size:13px}
  .topo .marca{flex:none;opacity:.9;display:flex}
  .baixo{display:grid;grid-template-columns:repeat(5,1fr);position:fixed;left:0;right:0;bottom:0;z-index:30;height:calc(var(--baixo) + env(safe-area-inset-bottom));padding-bottom:env(safe-area-inset-bottom);background:#fff;border-top:1px solid var(--linha)}
  .baixo>a,.baixo>details>summary{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:3px;font-size:11.5px;color:var(--txt2);text-decoration:none;height:100%;cursor:pointer;list-style:none}
  .baixo>details>summary::-webkit-details-marker{display:none}
  .baixo .ativo{color:var(--tinta);font-weight:600}
  .baixo .ativo svg{color:var(--tinta);background:var(--lilas2);border-radius:20px;padding:2px 14px;width:50px;height:26px}
  .baixo details{position:relative}
  .baixo details[open]>div{position:fixed;right:10px;bottom:calc(var(--baixo) + 8px + env(safe-area-inset-bottom));background:#fff;border:1px solid var(--linha);border-radius:14px;box-shadow:0 12px 32px rgba(16,32,58,.18);min-width:210px;padding:6px;display:flex;flex-direction:column}
  .baixo details>div a{padding:11px 12px;border-radius:10px;text-decoration:none;font-size:15px}
  .baixo details>div a:hover{background:var(--papel)}
  .app main{margin:0;padding:18px 16px calc(var(--baixo) + 28px + env(safe-area-inset-bottom))}
}
/* ---------- cabeçalho da página ---------- */
.cab{display:flex;align-items:flex-end;justify-content:space-between;gap:16px;flex-wrap:wrap;margin-bottom:20px}
.cab h1{font-size:30px;font-weight:800}
.cab p{color:var(--txt2);font-size:14.5px;margin-top:4px;max-width:62ch}
@media(max-width:899px){.cab h1{font-size:25px}}
/* ---------- base ---------- */
.card{background:var(--branco);border:1px solid var(--linha);border-radius:var(--raio);padding:20px 22px;margin-bottom:16px}
.card h2{font-size:18px;margin-bottom:12px}
@media(max-width:899px){.card{padding:16px}}
.linha{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.status{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px}
.status .card{margin-bottom:0}
@media(max-width:700px){.status{grid-template-columns:1fr}}
.ponto{width:10px;height:10px;border-radius:50%;display:inline-block;flex:none}
.p-ok{background:#2E7DDB}.p-mal{background:var(--alerta)}.p-off{background:#A0AEC0}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:16px}
@media(max-width:700px){.stats{grid-template-columns:repeat(2,1fr)}}
.stat{background:var(--branco);border:1px solid var(--linha);border-radius:var(--raio);padding:14px 16px}
.stat .n{font-family:'JetBrains Mono',monospace;font-size:28px;font-weight:600}
.stat .r{font-size:13px;color:var(--txt2)}
.periodo{display:flex;gap:6px;margin-bottom:12px}
.periodo a{font-size:13px;padding:4px 12px;border-radius:20px;border:1px solid var(--linha);text-decoration:none;background:#fff}
.periodo a.ativo{background:var(--tinta);color:#fff;border-color:var(--tinta)}
.item{display:flex;gap:12px;align-items:center;padding:12px 0;border-top:1px solid var(--linha);flex-wrap:wrap}
.item:first-of-type{border-top:none}
.item .q{flex:1;min-width:0}
.item .q b{display:block}
.item .q small{color:var(--txt2);display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.item .t{font-size:12px;color:var(--txt2);white-space:nowrap}
.tag{display:inline-block;font-size:12px;padding:2px 9px;border-radius:20px;background:var(--papel);color:var(--txt2);border:1px solid var(--linha);white-space:nowrap}
.tag.ag{background:var(--alerta-bg);color:var(--alerta);border-color:#F5C2BD}
.tag.hu{background:var(--lilas2);color:var(--tinta);border-color:var(--lilas)}
.tag.ok{background:var(--ok-bg);color:var(--ok);border-color:#C3D5EC}
.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;font:inherit;font-size:14.5px;font-weight:600;padding:9px 16px;border-radius:999px;border:1px solid var(--tinta);background:var(--tinta);color:#fff;text-decoration:none;cursor:pointer;white-space:nowrap}
.btn:hover{background:var(--tinta2);color:#fff}
.btn.sec{background:#fff;color:var(--tinta);border-color:var(--linha)}
.btn.sec:hover{border-color:var(--tinta)}
.btn.lil{background:var(--lilas);color:var(--tinta);border-color:var(--lilas)}
.btn.lil:hover{background:#B9A7FF}
.btn.peq{font-size:13px;padding:6px 12px}
.btn.perigo{background:var(--alerta);border-color:var(--alerta)}
.btn svg{width:18px;height:18px}
form.in{display:inline}
.vazio{color:var(--txt2);font-size:14px;padding:8px 0}
.aviso{padding:10px 14px;border-radius:12px;margin-bottom:16px;font-size:14px;background:var(--ok-bg);color:var(--ok)}
.aviso.erro{background:var(--alerta-bg);color:var(--alerta)}
label{display:block;font-size:13px;color:var(--txt2);margin:12px 0 5px}
input[type=text],input[type=email],input[type=password],input[type=tel],input[type=number],input[type=date],input[type=time],textarea,select{width:100%;font:inherit;font-size:16px;padding:10px 12px;border:1px solid var(--linha);border-radius:12px;background:#fff;color:var(--tinta)}
@media(min-width:900px){input[type=text],input[type=email],input[type=password],input[type=tel],input[type=number],input[type=date],input[type=time],textarea,select{font-size:15px}}
textarea{min-height:70px;resize:vertical}
input:focus,textarea:focus,select:focus{outline:2px solid var(--lilas);outline-offset:0;border-color:var(--lilas)}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:0 14px}
@media(max-width:700px){.grid2{grid-template-columns:1fr}}
.semana{display:grid;grid-template-columns:repeat(4,1fr);gap:0 10px}
@media(max-width:700px){.semana{grid-template-columns:repeat(2,1fr)}}
.chat{display:flex;flex-direction:column;gap:8px}
.msg{max-width:78%;padding:8px 12px;border-radius:16px;font-size:14.5px;white-space:pre-wrap;word-wrap:break-word}
.msg small{display:block;font-size:11px;color:var(--txt2);margin-top:2px;text-align:right}
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
/* ---------- faixa fina de aviso (fecha no x) ---------- */
.faixa{display:flex;align-items:center;gap:12px;padding:9px 10px 9px 14px;border-radius:12px;margin-bottom:14px;font-size:14px;background:var(--lilas2);border:1px solid var(--lilas);color:var(--tinta)}
.faixa.erro{background:var(--alerta-bg);border-color:#F5C2BD;color:var(--alerta)}
.faixa .tx{flex:1;min-width:0}
.faixa .tx a{font-weight:600;white-space:nowrap}
.faixa .x{flex:none;border:0;background:none;color:inherit;opacity:.6;cursor:pointer;width:28px;height:28px;border-radius:8px;font-size:20px;line-height:1}
.faixa .x:hover{opacity:1;background:rgba(16,32,58,.06)}
.faixa.fechada{display:none}
/* ---------- card de item com menu "..." ---------- */
.lista{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:14px}
@media(max-width:700px){.lista{grid-template-columns:1fr}}
.icard{position:relative;background:#fff;border:1px solid var(--linha);border-radius:var(--raio);padding:18px 18px 16px;display:flex;flex-direction:column;gap:12px;transition:box-shadow .15s,border-color .15s}
.icard:hover{box-shadow:var(--sombra);border-color:#C8D0DC}
.icard .top{display:flex;align-items:flex-start;gap:12px}
.av{width:44px;height:44px;border-radius:50%;background:var(--tinta);color:#fff;display:flex;align-items:center;justify-content:center;font-family:'Bricolage Grotesque',sans-serif;font-weight:700;font-size:16px;flex:none}
.icard h3{font-size:17px}
.icard .sub{font-size:13.5px;color:var(--txt2)}
.icard .meta{display:flex;flex-wrap:wrap;gap:6px}
.dias{display:grid;grid-template-columns:repeat(7,1fr);gap:4px}
.dias span{font-family:'JetBrains Mono',monospace;font-size:11px;text-align:center;padding:5px 0;border-radius:8px;background:var(--papel);color:#A0AEC0}
.dias span.on{background:var(--tinta);color:#fff}
.icard .nota{font-size:13px;color:var(--txt2)}
.mais{position:relative;margin-left:auto;flex:none}
.mais>summary{list-style:none;width:34px;height:34px;border-radius:10px;display:flex;align-items:center;justify-content:center;color:var(--txt2)}
.mais>summary::-webkit-details-marker{display:none}
.mais>summary:hover,.mais[open]>summary{background:var(--papel);color:var(--tinta)}
.mais>div{position:absolute;right:0;top:38px;z-index:5;background:#fff;border:1px solid var(--linha);border-radius:12px;box-shadow:0 12px 32px rgba(16,32,58,.14);min-width:180px;padding:6px;display:flex;flex-direction:column}
.mais>div a,.mais>div button{display:block;text-align:left;font:inherit;font-size:14.5px;padding:9px 12px;border-radius:8px;text-decoration:none;color:var(--tinta);background:none;border:0;cursor:pointer;width:100%}
.mais>div a:hover,.mais>div button:hover{background:var(--papel)}
.mais>div .perigo{color:var(--alerta)}
.icard-novo{border:1.5px dashed var(--linha);background:transparent;align-items:center;justify-content:center;text-align:center;color:var(--txt2);text-decoration:none;min-height:150px;font-weight:600}
.icard-novo:hover{border-color:var(--tinta);color:var(--tinta);box-shadow:none}
.icard-novo i{font-style:normal;font-size:26px;line-height:1;width:44px;height:44px;border-radius:50%;background:#fff;border:1px solid var(--linha);display:flex;align-items:center;justify-content:center}
/* ---------- lista vazia ---------- */
.vazia{background:#fff;border:1.5px dashed var(--linha);border-radius:var(--raio);padding:40px 20px;text-align:center;display:flex;flex-direction:column;align-items:center;gap:10px}
.vazia h2{font-size:20px}
.vazia p{color:var(--txt2);font-size:14.5px;max-width:46ch}
.vazia .btn{margin-top:6px}
/* ---------- painel lateral (computador) / folha de baixo (celular) ---------- */
.folha-fundo{position:fixed;inset:0;background:rgba(16,32,58,.42);z-index:40}
.folha{position:fixed;z-index:41;background:#fff;display:flex;flex-direction:column;top:0;right:0;bottom:0;width:min(520px,100%);box-shadow:-12px 0 40px rgba(16,32,58,.18);animation:entra-lado .18s ease-out}
.folha .fcab{display:flex;align-items:center;gap:12px;padding:18px 22px;border-bottom:1px solid var(--linha2)}
.folha .fcab h2{font-size:20px;flex:1}
.folha .fcorpo{flex:1;overflow-y:auto;padding:6px 22px 22px}
.folha .fpe{display:flex;gap:10px;justify-content:flex-end;padding:14px 22px calc(14px + env(safe-area-inset-bottom));border-top:1px solid var(--linha2);background:#fff}
.fechar{width:36px;height:36px;border-radius:10px;display:flex;align-items:center;justify-content:center;text-decoration:none;color:var(--txt2);font-size:22px;line-height:1}
.fechar:hover{background:var(--papel);color:var(--tinta)}
.folha .bloco{margin-top:18px}
.folha .bloco>.rot{display:block;margin-bottom:2px}
@keyframes entra-lado{from{transform:translateX(40px);opacity:0}to{transform:none;opacity:1}}
@keyframes entra-baixo{from{transform:translateY(60px);opacity:0}to{transform:none;opacity:1}}
@media(max-width:899px){
  .folha{top:auto;left:0;width:100%;max-height:92vh;border-radius:20px 20px 0 0;box-shadow:0 -12px 40px rgba(16,32,58,.18);animation:entra-baixo .2s ease-out}
  .folha:before{content:"";display:block;width:40px;height:4px;border-radius:4px;background:var(--linha);margin:8px auto 0}
  .folha .fcab{padding:10px 16px 12px}.folha .fcorpo{padding:4px 16px 18px}.folha .fpe{padding:12px 16px calc(12px + env(safe-area-inset-bottom))}
  .folha .fpe .btn{flex:1}
}
body.travado{overflow:hidden}
"""

FONTES = ('<link rel="preconnect" href="https://fonts.googleapis.com">'
          '<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,500;12..96,600;12..96,700;12..96,800'
          '&family=Instrument+Sans:wght@400;500;600&family=JetBrains+Mono:wght@500;600&display=swap" rel="stylesheet">')


def simbolo(tam: int = 28, escuro: bool = True) -> str:
    """Os dois balões da marca. escuro=True: versão para fundo escuro (balão branco)."""
    b, l = ("#FFFFFF", "#10203A") if escuro else ("#10203A", "#FFFFFF")
    return (f'<svg width="{tam}" height="{round(tam * 58 / 64)}" viewBox="0 0 64 58" aria-hidden="true">'
            f'<circle cx="24" cy="22" r="18" fill="{b}"/><path d="M12 33L7 44l13-4.5z" fill="{b}"/>'
            f'<path d="M16.5 15h14M16.5 22h11M16.5 29h7" stroke="{l}" stroke-width="3.2" stroke-linecap="round" fill="none"/>'
            f'<circle cx="42" cy="34" r="16" fill="#C7B8FF" stroke="{l}" stroke-width="5"/>'
            f'<path d="M50 47l8 5-2-13z" fill="#C7B8FF" stroke="{l}" stroke-width="5" stroke-linejoin="round"/>'
            f'<circle cx="42" cy="34" r="16" fill="#C7B8FF"/><path d="M50 47l8 5-2-13z" fill="#C7B8FF"/>'
            f'<circle cx="35" cy="34" r="2.6" fill="#10203A"/><circle cx="42" cy="34" r="2.6" fill="#10203A"/>'
            f'<circle cx="49" cy="34" r="2.6" fill="#10203A"/></svg>')


def logo(tam: int = 28) -> str:
    return f'<a class="logo" href="/painel">{simbolo(tam)}<span>a gente <b>assume</b></span></a>'


_ICONES = {
    "painel": '<path d="M4 11.5 12 5l8 6.5V20a1 1 0 0 1-1 1h-4.5v-5.5h-5V21H5a1 1 0 0 1-1-1z"/>',
    "conversas": '<path d="M20 12a7.5 7.5 0 0 1-11 6.6L4.5 20l1.3-4.2A7.5 7.5 0 1 1 20 12z"/>',
    "agenda": '<rect x="4" y="5.5" width="16" height="15" rx="2.5"/><path d="M4 10h16M8.5 3.5v4M15.5 3.5v4"/>',
    "profs": '<circle cx="9" cy="8.5" r="3.2"/><path d="M3.5 19.5c.6-3.2 2.8-5 5.5-5s4.9 1.8 5.5 5"/><circle cx="17" cy="9.5" r="2.4"/><path d="M16.5 14.6c2.2.2 3.6 1.7 4 4.4"/>',
    "experimentar": '<path d="M5 18.5V7a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2H8.5z"/><path d="M9 10.5h.01M12 10.5h.01M15 10.5h.01" stroke-width="2.6"/>',
    "config": '<path d="M5 20V8.5L12 4l7 4.5V20z"/><path d="M12 9.5v5M9.5 12h5M3.5 20h17"/>',
    "mais": '<circle cx="6" cy="12" r="1.3"/><circle cx="12" cy="12" r="1.3"/><circle cx="18" cy="12" r="1.3"/>',
    "sair": '<path d="M14 5h4a1 1 0 0 1 1 1v12a1 1 0 0 1-1 1h-4M10 16l-4-4 4-4M6 12h9"/>',
}


def icone(nome: str) -> str:
    return (f'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" '
            f'stroke-linejoin="round" aria-hidden="true">{_ICONES[nome]}</svg>')


# (chave, rótulo, endereço). "Simular": a clínica conversa com o assistente fazendo o papel do paciente.
MENU = [("painel", "Início", "/painel"), ("conversas", "Conversas", "/painel/conversas"),
        ("agenda", "Agenda", "/painel/agenda"), ("experimentar", "Simular", "/painel/experimentar"),
        ("profs", "Profissionais", "/painel/profissionais"), ("config", "Sua clínica", "/painel/configuracoes")]
MENU_BAIXO = ("painel", "conversas", "agenda", "experimentar")

JS_BASE = """<script>
(function(){
function guardar(k,v){try{sessionStorage.setItem(k,v)}catch(e){}}
function ler(k){try{return sessionStorage.getItem(k)}catch(e){return null}}
document.querySelectorAll('.faixa[data-id]').forEach(function(f){
  if(ler('faixa:'+f.dataset.id))f.classList.add('fechada');
  var x=f.querySelector('.x');if(x)x.addEventListener('click',function(){f.classList.add('fechada');guardar('faixa:'+f.dataset.id,'1')});});
document.addEventListener('click',function(e){document.querySelectorAll('details.mais[open],.baixo details[open]').forEach(function(d){if(!d.contains(e.target))d.removeAttribute('open')})});
var fo=document.querySelector('.folha');
if(fo){document.body.classList.add('travado');var sai=fo.querySelector('.fechar');
  document.addEventListener('keydown',function(e){if(e.key==='Escape'&&sai)location.href=sai.href});
  var bg=document.querySelector('.folha-fundo');if(bg&&sai)bg.addEventListener('click',function(){location.href=sai.href});}
})();
</script>"""


def faixa(id_: str, html_txt: str, erro: bool = False) -> str:
    """Aviso fino que a pessoa fecha no x (fica fechado até fechar o navegador)."""
    return (f'<div class="faixa{" erro" if erro else ""}" data-id="{esc(id_)}" role="status"><div class="tx">{html_txt}</div>'
            f'<button class="x" type="button" aria-label="Fechar aviso">&times;</button></div>')


LOGO_MAX_BYTES = 500 * 1024
_LOGO_TIPOS = ((b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"))


def tipo_imagem(dados: bytes) -> str | None:
    """Só PNG, JPG e WEBP (SVG fica de fora: pode carregar script)."""
    for assinatura, tipo in _LOGO_TIPOS:
        if dados.startswith(assinatura):
            return tipo
    if dados[:4] == b"RIFF" and dados[8:12] == b"WEBP":
        return "image/webp"
    return None


def html_logo_clinica(c: dict) -> str:
    """Logo que a clínica enviou; sem logo, as iniciais do nome."""
    if c.get("logo_v"):
        return f'<span class="clogo"><img src="/painel/logo?v={esc(c["logo_v"])}" alt=""></span>'
    return f'<span class="clogo sem">{esc(iniciais(c.get("nome", "")))}</span>'


def pagina(titulo: str, corpo: str, s: dict | None = None, ativa: str = "", extra_head: str = "", classe: str = "") -> str:
    head = (f'<!DOCTYPE html><html lang="pt-br"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
            f'<meta name="theme-color" content="#10203A">'
            f'<title>{esc(titulo)} · a gente assume</title>{FONTES}<style>{CSS}</style>{extra_head}</head>')
    if not s:
        return f'{head}<body class="sem-app {classe}"><main>{corpo}</main>{JS_BASE}</body></html>'
    nome_cl = esc(s["clinica"]["nome"])
    clogo = html_logo_clinica(s["clinica"])

    def link(k, t, u, com_icone=True):
        return f'<a href="{u}" class="{"ativo" if k == ativa else ""}">{icone(k) if com_icone else ""}<span>{t}</span></a>'
    lateral = (f'<aside class="lateral"><a class="clin" href="/painel">{clogo}<b>{nome_cl}</b></a><nav>'
               + "".join(link(k, t, u) for k, t, u in MENU)
               + f'<span class="sep"></span><a href="/painel/senha" class="{"ativo" if ativa == "senha" else ""}">'
                 f'<span style="width:20px"></span><span>Trocar senha</span></a>'
               + f'<a href="/sair">{icone("sair")}<span>Sair</span></a></nav>'
               + f'<a class="assina" href="/painel">{simbolo(18)}<span>com <b>a gente assume</b></span></a></aside>')
    topo = (f'<header class="topo"><a class="clin" href="/painel">{clogo}<b>{nome_cl}</b></a>'
            f'<span class="marca" title="a gente assume">{simbolo(24)}</span></header>')
    resto = [(k, t, u) for k, t, u in MENU if k not in MENU_BAIXO]
    mais_ativo = ativa in [k for k, _, _ in resto] or ativa == "senha"
    baixo = ('<nav class="baixo">'
             + "".join(link(k, t, u) for k, t, u in MENU if k in MENU_BAIXO)
             + f'<details><summary class="{"ativo" if mais_ativo else ""}">{icone("mais")}<span>Mais</span></summary><div>'
             + "".join(f'<a href="{u}">{t}</a>' for _, t, u in resto)
             + '<a href="/painel/senha">Trocar senha</a><a href="/sair">Sair</a></div></details></nav>')
    return (f'{head}<body class="app {classe}">{lateral}{topo}<main>{corpo}</main>{baixo}{JS_BASE}</body></html>')


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
    slug = c["slug"]
    p = p if p in (1, 7, 30) else 7
    st = await status_conexao(c) if situacao(c) == "ativa" else {
        "estado": "sem_config", "detalhe": "Assim que você assinar, a gente prepara o número da sua clínica (em até 24 horas)."}
    est = estatisticas(slug, p)
    ponto = {"conectado": "p-ok", "desconectado": "p-mal", "erro": "p-mal"}.get(st["estado"], "p-off")
    titulo_con = {"conectado": "Conectado", "desconectado": "Desconectado",
                  "erro": "Sem resposta", "sem_config": "Em preparação" if situacao(c) == "ativa" else "Aguardando assinatura"}[st["estado"]]
    ligado = bool(c.get("bot_ativo"))
    aviso = f'<div class="aviso">{esc(msg)}</div>' if msg else ""
    periodos = "".join(f'<a href="/painel?p={d}" class="{"ativo" if d == p else ""}">{t}</a>'
                       for d, t in ((1, "Hoje"), (7, "7 dias"), (30, "30 dias")))
    n_fila = R.zcard(K(slug, "fila"))
    corpo = f"""{aviso}{html_faixa_assinatura(c)}
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
      <div class="stat"><div class="n">{est['agendamentos']}</div><div class="r">agendamentos</div></div>
      <div class="stat"><div class="n">{est['remarcacoes']}</div><div class="r">remarcadas pelo assistente</div></div>
      <div class="stat"><div class="n">{est['cancelamentos']}</div><div class="r">canceladas pelo assistente</div></div>
      <div class="stat"><div class="n">{est['encaminhados']}</div><div class="r">passados para a equipe</div></div>
    </div>
    <div class="card"><h2>Esperando a equipe <span class="tag {'ag' if n_fila else ''}">{n_fila}</span></h2>{html_fila(slug)}</div>
    <div class="card"><h2>Últimas conversas</h2>{html_lista_conversas(slug, 0, 9)}
      <p style="margin-top:10px"><a href="/painel/conversas">Ver todas</a></p></div>"""
    return html_resp(pagina("Painel", corpo, s, "painel", '<meta http-equiv="refresh" content="60">'))


@router.post("/painel/assistente")
def painel_assistente(ligar: str = Form("0"), s: dict = Depends(exigir_login)):
    c = obter_clinica(s["clinica"]["slug"])
    if ligar == "1" and situacao(c) != "ativa":
        return RedirectResponse("/painel?msg=Para+ligar+o+assistente,+assine+primeiro.", status_code=303)
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
    consultas = AG.consultas_do_tel(slug, tel)
    lista_c = "".join(
        f'<p style="font-size:14px;margin:6px 0"><b>{esc(AG.quando_txt(x))}</b> com {esc(x["prof_nome"])} · '
        f'<a href="/painel/agenda/item/{esc(x["id"])}">abrir</a> · <a href="/painel/agenda/item/{esc(x["id"])}#remarcar">remarcar</a> · '
        f'<a href="/painel/agenda/item/{esc(x["id"])}#cancelar">cancelar</a></p>' for x in consultas)
    card_c = f'<div class="card"><h2>Próximas consultas</h2>{lista_c or "<div class=vazio>Nenhuma consulta marcada.</div>"}</div>'
    corpo = f"""<p style="margin-bottom:12px"><a href="/painel">Voltar ao painel</a></p>
    <div class="card"><div class="linha"><h2 style="margin:0">{nome_ou_tel(info)}</h2>
      <span class="mono" style="color:var(--txt2);font-size:13px">{fmt_tel(tel)}</span>{tag_estado(info)}</div>
      <p style="color:var(--txt2);font-size:14px;margin:8px 0 12px">{explica}</p>
      <div class="linha">{acoes}</div></div>
    {card_c}
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
    return html_resp(pagina("Agendamentos", corpo, s, "agenda"))


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
# AGENDA (painel): dia/semana, marcar, mover, cancelar, bloquear
# ============================================================
AG_CSS = """<style>
.ag-ctl{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:14px}
.ag-ctl form{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.ag-ctl select,.ag-ctl input{width:auto}
.ag-lin{display:flex;gap:10px;align-items:center;padding:7px 0;border-top:1px solid var(--linha);font-size:14px}
.ag-lin:first-of-type{border-top:none}
.ag-lin .h{font-family:'JetBrains Mono',monospace;font-size:13px;width:92px;flex:none;color:var(--txt2)}
.ag-lin .q{flex:1;min-width:0}
.ag-livre{color:var(--txt2)}
.ag-livre a{font-size:13px}
.ag-cons{background:var(--lilas2);border-radius:8px;padding:4px 10px}
.ag-cons.sim{background:#fff;border:1px dashed var(--lilas)}
.ag-bloq{background:var(--papel);border-radius:8px;padding:4px 10px;color:var(--txt2)}
.ag-sem{overflow-x:auto}
.ag-sem table{min-width:760px;table-layout:fixed}
.ag-sem td{font-size:13px;padding:6px 4px}
.ag-sem th{font-size:13px;padding:6px 4px}
.ag-sem .it{display:block;border-radius:6px;padding:3px 6px;margin-bottom:4px;background:var(--lilas2);text-decoration:none}
.ag-sem .it.sim{background:#fff;border:1px dashed var(--lilas)}
.ag-sem .it.bl{background:var(--papel);color:var(--txt2)}
.ag-sem .hoje{background:var(--ok-bg)}
</style>"""


def _quem(s: dict) -> str:
    return s["usuario"].get("nome") or s["usuario"]["email"]


def _dia(v: str):
    try:
        return datetime.strptime((v or "")[:10], "%Y-%m-%d").date()
    except ValueError:
        return agora().date()


def _link_ag(d, v="dia", prof="", sim=False) -> str:
    q = f"/painel/agenda?d={d.isoformat()}&v={v}"
    if prof:
        q += f"&prof={prof}"
    if sim:
        q += "&sim=1"
    return q


def _opts_prof(profs: list, sel: str = "", todos: str = "") -> str:
    o = f'<option value="">{esc(todos)}</option>' if todos else ""
    return o + "".join(f'<option value="{esc(p["id"])}" {"selected" if p["id"] == sel else ""}>{esc(p["nome"])}</option>'
                       for p in profs)


def _rotulo_item(x: dict) -> str:
    if x.get("tipo") == "bloqueio":
        return "Bloqueado" + (f": {esc(x['motivo'])}" if x.get("motivo") else "")
    tags = ""
    if x.get("sim"):
        tags += ' <span class="tag">simulação</span>'
    elif x.get("origem") == "assistente":
        tags += ' <span class="tag hu">pelo assistente</span>'
    if x.get("encaixe"):
        tags += ' <span class="tag">encaixe</span>'
    conv_ = f' · {esc(x["convenio"])}' if x.get("convenio") else ""
    tel_ = "" if x.get("sim") or not x.get("tel") else f' · <span class="mono">{esc(fmt_tel(x["tel"]))}</span>'
    return f'<b>{esc(x.get("paciente") or "sem nome")}</b>{tel_}{conv_}{tags}'


def html_ag_dia(slug: str, conf: dict, profs: list, d, sim: bool) -> str:
    itens = AG.listar(slug, d, d, sim)
    gerais = [x for x in itens if x.get("tipo") == "bloqueio" and not x.get("prof_id")]
    blocos = []
    fer = AG.fecha_no_feriado(conf, d)
    if fer:
        blocos.append(f'<div class="aviso" style="background:var(--papel);color:var(--txt2)">Feriado: {esc(fer)}. '
                      'O assistente não oferece horário nesse dia; a equipe ainda pode marcar aqui. '
                      'Se a clínica atende em feriado, marque isso em Sua clínica.</div>')
    for x in gerais:
        hr = "dia todo" if x.get("dia_todo") else f"{x['inicio']}–{x['fim']}"
        blocos.append(f'<div class="aviso" style="background:var(--papel);color:var(--txt2)">Todos bloqueados, {hr}'
                      f'{(": " + esc(x["motivo"])) if x.get("motivo") else ""} '
                      f'<form class="in" method="post" action="/painel/agenda/item/{esc(x["id"])}/desbloquear">'
                      f'<button class="btn sec peq">Desbloquear</button></form></div>')
    for p in profs:
        meus = [x for x in itens if x.get("prof_id") == p["id"]]
        livres = AG.livres_no_dia(slug, conf, p, d, sim, respeitar_regras=False)
        agora_ = agora().replace(tzinfo=None)
        linhas = []
        for x in meus:
            hr = "dia todo" if x.get("dia_todo") else f"{x['inicio']}–{x['fim']}"
            if x.get("tipo") == "bloqueio":
                q = (f'<span class="ag-bloq">{_rotulo_item(x)}</span> '
                     f'<form class="in" method="post" action="/painel/agenda/item/{esc(x["id"])}/desbloquear">'
                     f'<button class="btn sec peq">Desbloquear</button></form>')
            else:
                q = (f'<a class="ag-cons {"sim" if x.get("sim") else ""}" href="/painel/agenda/item/{esc(x["id"])}" '
                     f'style="text-decoration:none;display:inline-block">{_rotulo_item(x)}</a>')
            linhas.append((x["inicio"], f'<div class="ag-lin"><span class="h">{hr}</span><div class="q">{q}</div></div>'))
        for h in livres:
            passou = datetime.combine(d, datetime.min.time()) + timedelta(minutes=AG.minutos(h)) < agora_
            marcar = "" if passou else (f' · <a href="/painel/agenda/nova?prof={p["id"]}&d={d.isoformat()}&h={h}">marcar</a>')
            linhas.append((h, f'<div class="ag-lin ag-livre"><span class="h">{h}</span><div class="q">livre{marcar}</div></div>'))
        linhas.sort(key=lambda t: t[0])
        corpo = "".join(l for _, l in linhas) or '<div class="vazio">Não atende neste dia.</div>'
        blocos.append(f'<div class="card"><h2>{esc(p["nome"])} <span class="mono" style="font-size:12px;color:var(--txt2);font-weight:400">'
                      f'{esc(AG.resumo_semana(p))}</span></h2>{corpo}</div>')
    return "".join(blocos)


def html_ag_semana(slug: str, conf: dict, profs: list, d, sim: bool, prof_sel: str) -> str:
    ini = d - timedelta(days=d.weekday())
    dias = [ini + timedelta(days=i) for i in range(7)]
    itens = AG.listar(slug, ini, dias[-1], sim)
    ids = {p["id"] for p in profs}
    hoje = agora().date()
    cab = "".join(f'<th class="{"hoje" if x == hoje else ""}"><a href="{_link_ag(x, "dia", prof_sel, sim)}">'
                  f'{AG.DIAS_CURTOS[x.weekday()]} {x.strftime("%d/%m")}</a>'
                  + (f'<br><small style="font-weight:400">{esc(AG.fecha_no_feriado(conf, x))}</small>' if AG.fecha_no_feriado(conf, x) else "")
                  + '</th>' for x in dias)
    cels = []
    for x in dias:
        dd = x.isoformat()
        conteudo = []
        for it in itens:
            if it["data"] != dd or (it.get("prof_id") and it["prof_id"] not in ids):
                continue
            quem = "" if len(profs) == 1 else f' · {esc(it.get("prof_nome", ""))}'
            if it.get("tipo") == "bloqueio":
                hr = "dia todo" if it.get("dia_todo") else f"{it['inicio']}–{it['fim']}"
                conteudo.append(f'<span class="it bl">{hr} bloqueado{quem}</span>')
            else:
                dica = "" if it.get("sim") or not it.get("tel") else f' title="{esc(fmt_tel(it["tel"]))}"'
                conteudo.append(f'<a class="it {"sim" if it.get("sim") else ""}" href="/painel/agenda/item/{esc(it["id"])}"{dica}>'
                                f'<span class="mono">{it["inicio"]}</span> {esc(it.get("paciente") or "sem nome")}{quem}</a>')
        n_livres = sum(len(AG.livres_no_dia(slug, conf, p, x, sim, respeitar_regras=False)) for p in profs) if x >= hoje else 0
        if n_livres:
            conteudo.append(f'<a href="{_link_ag(x, "dia", prof_sel, sim)}" style="font-size:12px;color:var(--txt2)">{n_livres} livres</a>')
        cels.append(f'<td class="{"hoje" if x == hoje else ""}">{"".join(conteudo)}</td>')
    return (f'<div class="card ag-sem"><table><tr>{cab}</tr><tr>{"".join(cels)}</tr></table></div>')


@router.get("/painel/agenda", response_class=HTMLResponse)
def painel_agenda(d: str = "", v: str = "dia", prof: str = "", sim: int = 0, msg: str = "", erro: str = "",
                  s: dict = Depends(exigir_login)):
    slug = s["clinica"]["slug"]
    conf = obter_conf(slug)
    todos = AG.profs_com_agenda(conf)
    dia = _dia(d)
    v = "semana" if v == "semana" else "dia"
    simb = bool(sim)
    profs = [p for p in todos if not prof or p["id"] == prof]
    aviso = (f'<div class="aviso">{esc(msg)}</div>' if msg else "") + (f'<div class="aviso erro">{esc(erro)}</div>' if erro else "")
    if not todos:
        corpo = f"""{aviso}<div class="card"><h2>Agenda</h2>
          <p style="font-size:15px;margin-bottom:8px">Para o assistente marcar consultas direto aqui, diga os horários de cada profissional.</p>
          <p style="color:var(--txt2);font-size:14px;margin-bottom:12px">Em <b>Profissionais</b>, abra cada um e preencha os dias e horários
          (ex.: <span class="mono">8h-12h, 14h-18h</span>) e a duração da consulta. A partir daí o assistente oferece só horários livres
          e a recepção vê tudo aqui: move, cancela e bloqueia.</p>
          <a class="btn lil" href="/painel/profissionais">Preencher os horários</a></div>{html_ag_pedidos_link(slug)}"""
        return html_resp(pagina("Agenda", corpo, s, "agenda", AG_CSS))
    passo = 7 if v == "semana" else 1
    ant, prox = dia - timedelta(days=passo), dia + timedelta(days=passo)
    if v == "semana":
        ini = dia - timedelta(days=dia.weekday())
        titulo = f"Semana de {ini.strftime('%d/%m')} a {(ini + timedelta(days=6)).strftime('%d/%m')}"
    else:
        titulo = AG.quando_txt(dia).capitalize()
    controles = f"""<div class="ag-ctl">
      <a class="btn sec peq" href="{_link_ag(ant, v, prof, simb)}">‹</a>
      <a class="btn sec peq" href="{_link_ag(agora().date(), v, prof, simb)}">Hoje</a>
      <a class="btn sec peq" href="{_link_ag(prox, v, prof, simb)}">›</a>
      <h2 style="font-size:18px;margin:0 6px">{esc(titulo)}</h2>
      <form method="get" action="/painel/agenda" style="margin-left:auto">
        <input type="date" name="d" value="{dia.isoformat()}" onchange="this.form.submit()">
        <select name="v" onchange="this.form.submit()"><option value="dia">Dia</option>
          <option value="semana" {"selected" if v == "semana" else ""}>Semana</option></select>
        {'<select name="prof" onchange="this.form.submit()">' + _opts_prof(todos, prof, "Todos os profissionais") + '</select>' if len(todos) > 1 else ''}
        <label style="margin:0;font-size:13px"><input type="checkbox" name="sim" value="1" {"checked" if simb else ""}
          onchange="this.form.submit()"> mostrar simulação</label>
      </form></div>
    <div class="ag-ctl">
      <a class="btn" href="/painel/agenda/nova?d={dia.isoformat()}{'&prof=' + prof if prof else ''}">Marcar consulta</a>
      <a class="btn sec" href="#bloquear" onclick="document.getElementById('bloquear').open=true">Bloquear horário</a>
    </div>"""
    miolo = html_ag_semana(slug, conf, profs, dia, simb, prof) if v == "semana" else html_ag_dia(slug, conf, profs, dia, simb)
    bloquear = f"""<details class="card" id="bloquear"><summary><b>Bloquear horário</b> (folga, feriado, compromisso)</summary>
      <form method="post" action="/painel/agenda/bloquear" style="margin-top:8px">
        <div class="grid2">
          <div><label>Profissional</label><select name="prof">{_opts_prof(todos, prof, "Todos (a clínica inteira)")}</select></div>
          <div><label>Dia</label><input type="date" name="d" value="{dia.isoformat()}" required></div>
          <div><label>Das (vazio = dia todo)</label><input type="time" name="ini"></div>
          <div><label>Até</label><input type="time" name="fim"></div>
        </div>
        <label>Motivo (só a equipe vê)</label><input type="text" name="motivo" placeholder="Feriado, curso, consulta externa">
        <div style="margin-top:12px"><button class="btn">Bloquear</button></div>
      </form></details>"""
    sim_ctl = ""
    if simb:
        sim_ctl = ('<div class="card"><p style="font-size:14px;color:var(--txt2)">As reservas com borda tracejada foram feitas na aba '
                   'Simular. Elas não ocupam a agenda de verdade: nenhum paciente perde horário por causa delas.</p>'
                   '<form method="post" action="/painel/agenda/simulacao/apagar" style="margin-top:8px">'
                   '<button class="btn sec peq">Apagar reservas de simulação</button></form></div>')
    corpo = f"{aviso}{controles}{miolo}{sim_ctl}{bloquear}{html_ag_pedidos_link(slug)}"
    return html_resp(pagina("Agenda", corpo, s, "agenda", AG_CSS + '<meta http-equiv="refresh" content="120">'))


def html_ag_pedidos_link(slug: str) -> str:
    n = sum(1 for p in listar_pedidos(slug) if p.get("status", "novo") == "novo")
    if not n:
        return '<p style="font-size:13px;margin-top:6px"><a href="/painel/agendamentos">Pedidos anotados pelo assistente</a></p>'
    return (f'<div class="card"><h2>Pedidos anotados para confirmar <span class="tag ag">{n}</span></h2>'
            f'<p style="font-size:14px;color:var(--txt2)">Pedidos de profissionais que ainda não têm horários na agenda. '
            f'<a href="/painel/agendamentos">Ver os pedidos</a></p></div>')


@router.get("/painel/agenda/nova", response_class=HTMLResponse)
def painel_agenda_nova(prof: str = "", d: str = "", h: str = "", erro: str = "", s: dict = Depends(exigir_login)):
    slug = s["clinica"]["slug"]
    conf = obter_conf(slug)
    todos = AG.profs_com_agenda(conf)
    if not todos:
        return RedirectResponse("/painel/agenda", status_code=303)
    dia = _dia(d)
    aviso = f'<div class="aviso erro">{esc(erro)}</div>' if erro else ""
    corpo = f"""<p style="margin-bottom:12px"><a href="{_link_ag(dia)}">Voltar à agenda</a></p>{aviso}
    <form method="post" action="/painel/agenda/nova" class="card" style="max-width:560px"><h2>Marcar consulta</h2>
      <div class="grid2">
        <div><label>Profissional</label><select name="prof">{_opts_prof(todos, prof)}</select></div>
        <div><label>Dia</label><input type="date" name="d" value="{dia.isoformat()}" required></div>
        <div><label>Hora</label><input type="time" name="h" value="{esc(h)}" required></div>
        <div><label>Paciente</label><input type="text" name="paciente" required></div>
        <div><label>WhatsApp do paciente</label><input type="tel" name="tel" required placeholder="(11) 99999-8888"></div>
        <div><label>Particular ou convênio</label><input type="text" name="convenio"></div>
      </div>
      <label>Observação</label><input type="text" name="obs">
      <label style="margin-top:12px"><input type="checkbox" name="encaixe" value="1"> Encaixe (fora da grade de horários)</label>
      <div style="margin-top:14px"><button class="btn">Marcar</button></div></form>"""
    return html_resp(pagina("Marcar consulta", corpo, s, "agenda", AG_CSS))


@router.post("/painel/agenda/nova")
async def painel_agenda_nova_post(request: Request, s: dict = Depends(exigir_login)):
    slug = s["clinica"]["slug"]
    conf = obter_conf(slug)
    f = await request.form()
    p = AG.achar_prof(conf, str(f.get("prof", "")))
    dia, h = _dia(str(f.get("d", ""))), str(f.get("h", ""))[:5]
    if not p:
        return RedirectResponse("/painel/agenda/nova?erro=" + quote("Escolha o profissional."), status_code=303)
    tel = tel_paciente(str(f.get("tel", "")))
    if not str(f.get("paciente", "")).strip() or not tel:
        return RedirectResponse(f"/painel/agenda/nova?prof={p['id']}&d={dia.isoformat()}&h={quote(h)}&erro="
                                + quote("Preencha o nome e o WhatsApp do paciente (com DDD)."), status_code=303)
    ok, r = AG.reservar(slug, conf, p, dia, h, {
        "paciente": str(f.get("paciente", "")), "tel": tel, "convenio": str(f.get("convenio", "")),
        "obs": str(f.get("obs", ""))}, encaixe=bool(f.get("encaixe")), por=_quem(s), origem="equipe")
    if not ok:
        return RedirectResponse(f"/painel/agenda/nova?prof={p['id']}&d={dia.isoformat()}&h={quote(h)}&erro="
                                + quote(r + " Se for um encaixe, marque a opção Encaixe."), status_code=303)
    return RedirectResponse(_link_ag(dia) + "&msg=" + quote(f"Consulta marcada: {AG.quando_txt(r)}."), status_code=303)


@router.get("/painel/agenda/item/{iid}", response_class=HTMLResponse)
def painel_agenda_item(iid: str, erro: str = "", msg: str = "", s: dict = Depends(exigir_login)):
    slug = s["clinica"]["slug"]
    conf = obter_conf(slug)
    it = AG.obter(slug, iid)
    if not it or it.get("tipo") != "consulta":
        raise HTTPException(status_code=404)
    dia = _dia(it["data"])
    aviso = (f'<div class="aviso erro">{esc(erro)}</div>' if erro else "") + (f'<div class="aviso">{esc(msg)}</div>' if msg else "")
    tel = it.get("tel", "")
    conversa = ""
    if tel and not it.get("sim") and R.exists(K(slug, "conv", tel)):
        conversa = f' · <a href="/painel/conversa/{tel}">ver conversa</a>'
    if tel and not it.get("sim") and not conversa:
        conversa = f' · <a href="https://wa.me/{tel}" target="_blank" rel="noopener">abrir no WhatsApp</a>'
    contato = (f'<span class="mono">{fmt_tel(tel)}</span>{conversa}' if tel else
               ("simulação (aba Simular)" if it.get("sim") else "sem WhatsApp"))
    mud = "".join(f'<li>{esc(m.get("acao"))} por {esc(m.get("por") or "-")} {ha_quanto(m.get("em", "") + "-03:00")}'
                  f'{(" (antes: " + esc(m["antes"]) + ")") if m.get("antes") else ""}</li>' for m in it.get("mudancas", []))
    cancelada = it.get("status") == "cancelada"
    pode_avisar = bool(tel) and not it.get("sim")
    avisar = ('<label style="margin-top:10px"><input type="checkbox" name="avisar" value="1" checked> '
              'Avisar o paciente pelo WhatsApp</label>') if pode_avisar else ""
    acoes = "" if cancelada else f"""
    <form id="remarcar" method="post" action="/painel/agenda/item/{esc(iid)}/mover" class="card" style="max-width:560px"><h2>Remarcar (mudar dia ou horário)</h2>
      <div class="grid2">
        <div><label>Profissional</label><select name="prof">{_opts_prof(AG.profs_com_agenda(conf), it.get("prof_id", ""))}</select></div>
        <div><label>Dia</label><input type="date" name="d" value="{it['data']}" required></div>
        <div><label>Hora</label><input type="time" name="h" value="{it['inicio']}" required></div>
      </div>
      <label style="margin-top:10px"><input type="checkbox" name="encaixe" value="1"> Encaixe (fora da grade de horários)</label>
      {avisar}
      <div style="margin-top:12px"><button class="btn">Mudar</button></div></form>
    <form id="cancelar" method="post" action="/painel/agenda/item/{esc(iid)}/cancelar" class="card" style="max-width:560px"><h2>Cancelar consulta</h2>
      {avisar}
      <div style="margin-top:12px"><button class="btn sec">Cancelar consulta</button></div></form>"""
    corpo = f"""<p style="margin-bottom:12px"><a href="{_link_ag(dia, sim=it.get('sim', False))}">Voltar à agenda</a></p>{aviso}
    <div class="card"><div class="linha"><h2 style="margin:0">{esc(it.get('paciente') or 'sem nome')}</h2>
      {'<span class="tag">cancelada</span>' if cancelada else '<span class="tag ok">marcada</span>'}
      {'<span class="tag">simulação</span>' if it.get('sim') else ''}</div>
      <p style="margin-top:8px"><b>{esc(AG.quando_txt(it))}</b> até {it['fim']} com {esc(it.get('prof_nome'))}</p>
      <p style="font-size:14px;color:var(--txt2);margin-top:4px">{contato}
        {(' · ' + esc(it['convenio'])) if it.get('convenio') else ''}{(' · ' + esc(it['obs'])) if it.get('obs') else ''}</p>
      <p style="font-size:13px;color:var(--txt2);margin-top:4px">Marcada {'pelo assistente' if it.get('origem') == 'assistente' else 'por ' + esc(it.get('criado_por') or 'equipe')}
        {ha_quanto(it.get('criado_em', '') + '-03:00')}</p>
      {('<ul style="font-size:13px;color:var(--txt2);margin:6px 0 0 18px">' + mud + '</ul>') if mud else ''}</div>{acoes}"""
    return html_resp(pagina("Consulta", corpo, s, "agenda", AG_CSS))


async def _avisar_paciente(s: dict, it: dict, texto: str) -> str:
    """Manda a mensagem ao paciente e registra na conversa. Devolve um complemento para o aviso do painel."""
    c = obter_clinica(s["clinica"]["slug"])
    tel = it.get("tel", "")
    if not tel or it.get("sim"):
        return ""
    if not await enviar(c, tel, texto):
        return " Não deu para avisar o paciente pelo WhatsApp: avise por outro meio."
    registrar_msg(c["slug"], tel, "equipe", texto, autor=_quem(s))
    return " Paciente avisado pelo WhatsApp."


@router.post("/painel/agenda/item/{iid}/mover")
async def painel_agenda_mover(iid: str, request: Request, s: dict = Depends(exigir_login)):
    slug = s["clinica"]["slug"]
    conf = obter_conf(slug)
    f = await request.form()
    p = AG.achar_prof(conf, str(f.get("prof", "")))
    antes = AG.obter(slug, iid)
    if not p or not antes:
        return RedirectResponse(f"/painel/agenda/item/{iid}?erro=" + quote("Escolha o profissional."), status_code=303)
    ok, r = AG.mover(slug, conf, iid, p, _dia(str(f.get("d", ""))), str(f.get("h", ""))[:5],
                     encaixe=bool(f.get("encaixe")), por=_quem(s))
    if not ok:
        return RedirectResponse(f"/painel/agenda/item/{iid}?erro=" + quote(r + " Se for um encaixe, marque a opção Encaixe."),
                                status_code=303)
    extra = ""
    if f.get("avisar"):
        nome = (r.get("paciente") or "").split(" ")[0]
        extra = await _avisar_paciente(s, r, (
            f"Olá{', ' + nome if nome else ''}. Sua consulta com {r['prof_nome']} foi remarcada para "
            f"{AG.quando_txt(r)}. Se esse horário não der, é só responder aqui."))
    return RedirectResponse(f"/painel/agenda/item/{iid}?msg=" + quote(f"Consulta movida para {AG.quando_txt(r)}." + extra),
                            status_code=303)


@router.post("/painel/agenda/item/{iid}/cancelar")
async def painel_agenda_cancelar(iid: str, request: Request, s: dict = Depends(exigir_login)):
    slug = s["clinica"]["slug"]
    f = await request.form()
    it = AG.cancelar(slug, iid, por=_quem(s))
    if not it:
        raise HTTPException(status_code=404)
    extra = ""
    if f.get("avisar"):
        nome = (it.get("paciente") or "").split(" ")[0]
        extra = await _avisar_paciente(s, it, (
            f"Olá{', ' + nome if nome else ''}. Sua consulta com {it['prof_nome']} de {AG.quando_txt(it)} foi cancelada. "
            "Se quiser marcar outro horário, é só responder aqui."))
    return RedirectResponse(_link_ag(_dia(it["data"]), sim=it.get("sim", False)) + "&msg=" + quote("Consulta cancelada." + extra),
                            status_code=303)


@router.post("/painel/agenda/bloquear")
async def painel_agenda_bloquear(request: Request, s: dict = Depends(exigir_login)):
    slug = s["clinica"]["slug"]
    conf = obter_conf(slug)
    f = await request.form()
    ref = str(f.get("prof", ""))
    p = AG.achar_prof(conf, ref) if ref else None
    dia = _dia(str(f.get("d", "")))
    ok, r = AG.bloquear(slug, p, dia, str(f.get("ini", ""))[:5], str(f.get("fim", ""))[:5], str(f.get("motivo", "")), por=_quem(s))
    if not ok:
        return RedirectResponse(_link_ag(dia) + "&erro=" + quote(r), status_code=303)
    conflitos = [x for x in AG.listar(slug, dia, dia) if x.get("tipo") == "consulta" and (not p or x.get("prof_id") == p["id"])
                 and AG.minutos(x["inicio"]) < AG.minutos(r["fim"]) and AG.minutos(r["inicio"]) < AG.minutos(x["fim"])]
    m = "Horário bloqueado. O assistente não oferece mais esse período."
    if conflitos:
        m += f" Atenção: {len(conflitos)} consulta(s) já marcada(s) nesse período continuam na agenda. Mova ou cancele."
    return RedirectResponse(_link_ag(dia) + "&msg=" + quote(m), status_code=303)


@router.post("/painel/agenda/item/{iid}/desbloquear")
def painel_agenda_desbloquear(iid: str, s: dict = Depends(exigir_login)):
    slug = s["clinica"]["slug"]
    it = AG.obter(slug, iid)
    AG.desbloquear(slug, iid)
    dia = _dia(it["data"]) if it else agora().date()
    return RedirectResponse(_link_ag(dia) + "&msg=" + quote("Desbloqueado."), status_code=303)


@router.post("/painel/agenda/simulacao/apagar")
def painel_agenda_sim_apagar(s: dict = Depends(exigir_login)):
    AG.apagar_simulacao(s["clinica"]["slug"])
    return RedirectResponse("/painel/agenda?msg=" + quote("Reservas de simulação apagadas."), status_code=303)


# ============================================================
# PROFISSIONAIS (cada um vira um card; adicionar e editar abrem um painel ao lado / folha de baixo no celular)
# ============================================================
PROF_CAMPOS = (("nome", "Nome", "Dra. Ana Souza"),
               ("especialidades", "Especialidades", "Ortodontia, clareamento"),
               ("convenios", "Convênios que atende", "Unimed Odonto, Amil Dental, particular"),
               ("horarios", "Observação sobre horários (opcional)", "Avaliação só às segundas"),
               ("obs", "Outras observações (opcional)", "Não atende criança"))


def iniciais(nome: str) -> str:
    partes = [p for p in (nome or "").split() if p.lower().rstrip(".") not in ("dr", "dra", "doutor", "doutora")]
    if len(partes) > 1:
        partes = [partes[0], partes[-1]]
    return "".join(p[0] for p in partes).upper() or "?"


def html_prof_card(x: dict) -> str:
    pid = esc(x.get("id", ""))
    dias = "".join(f'<span class="{"on" if AG.intervalos_do_dia(x, d) else ""}" title="{AG.DIAS_LONGOS[d]}">'
                   f'{AG.DIAS_CURTOS[d][:3].capitalize()}</span>' for d in range(7))
    resumo = AG.resumo_semana(x)
    convs = [c.strip() for c in (x.get("convenios") or "").split(",") if c.strip()]
    tags = "".join(f'<span class="tag">{esc(c)}</span>' for c in convs[:4])
    if len(convs) > 4:
        tags += f'<span class="tag">+{len(convs) - 4}</span>'
    agenda_txt = (f'<p class="nota mono" style="font-size:12px">{esc(resumo)}</p>' if resumo else
                  '<p class="nota">Sem horários: o assistente anota o pedido e a recepção confirma. '
                  f'<a href="/painel/profissionais?editar={pid}">Preencher horários</a></p>')
    return f"""<div class="icard">
      <div class="top"><span class="av">{esc(iniciais(x.get("nome", "")))}</span>
        <div style="min-width:0;flex:1"><h3>{esc(x.get("nome", ""))}</h3>
          <div class="sub">{esc(x.get("especialidades") or "Sem especialidade informada")}</div></div>
        <details class="mais"><summary aria-label="Mais opções">{icone("mais")}</summary><div>
          <a href="/painel/profissionais?editar={pid}">Editar</a>
          <a href="/painel/agenda?v=semana&prof={pid}">Ver na agenda</a>
          <a class="perigo" href="/painel/profissionais?remover={pid}">Remover</a></div></details></div>
      <div class="dias">{dias}</div>{agenda_txt}
      {f'<div class="meta">{tags}</div>' if tags else ''}</div>"""


def html_prof_form(x: dict) -> str:
    novo = not x.get("id")

    def campo(ch, rot, ex):
        return (f'<label for="f-{ch}">{rot}</label><input id="f-{ch}" type="text" name="prof_{ch}" '
                f'value="{esc(x.get(ch, ""))}" placeholder="{esc(ex)}"{" required" if ch == "nome" else ""}>')
    c = {ch: campo(ch, rot, ex) for ch, rot, ex in PROF_CAMPOS}
    ag = x.get("agenda") or {}
    dias = ag.get("dias") or {}
    linhas = "".join(
        f'<div class="hdia"><label class="sw"><input type="checkbox" data-dia="{d}"{" checked" if dias.get(str(d), "").strip() else ""}>'
        f'<span>{AG.DIAS_LONGOS[d].split("-")[0].capitalize()}</span></label>'
        f'<input type="text" class="mono" name="prof_ag{d}" value="{esc(dias.get(str(d), ""))}" '
        f'placeholder="{"8h-12h, 14h-18h" if d < 5 else "não atende"}" aria-label="Horário de {AG.DIAS_LONGOS[d]}"></div>'
        for d in range(7))
    titulo = "Novo profissional" if novo else "Editar profissional"
    return f"""<div class="folha-fundo"></div>
    <form class="folha" method="post" action="/painel/profissionais" role="dialog" aria-modal="true" aria-label="{titulo}">
      <div class="fcab"><h2>{titulo}</h2><a class="fechar" href="/painel/profissionais" aria-label="Fechar">&times;</a></div>
      <div class="fcorpo">
        <input type="hidden" name="prof_id" value="{esc(x.get("id", ""))}">
        <div class="bloco"><span class="rot">Quem é</span>{c["nome"]}{c["especialidades"]}{c["convenios"]}</div>
        <div class="bloco"><span class="rot">Quando atende</span>
          <p style="font-size:13px;color:var(--txt2);margin:4px 0 6px">Escreva do jeito que você falaria: <span class="mono">8h-12h, 14h-18h</span>.
          Com horários aqui, o assistente marca direto na agenda. Sem horários, ele anota o pedido e a recepção confirma.</p>
          <div class="hgrade">{linhas}</div>
          <button type="button" class="btn sec peq" id="copiar-seg" style="margin-top:10px">Repetir a segunda de terça a sexta</button>
          <div class="grid2" style="margin-top:4px">
            <div><label for="f-dur">Cada consulta dura (min)</label>
              <input id="f-dur" type="number" min="5" max="480" step="5" name="prof_dur" value="{esc(ag.get("duracao") or AG.DURACAO_PADRAO)}"></div>
          </div>{c["horarios"]}</div>
        <div class="bloco"><span class="rot">Mais alguma coisa</span>{c["obs"]}</div>
      </div>
      <div class="fpe"><a class="btn sec" href="/painel/profissionais">Cancelar</a><button class="btn">{"Adicionar" if novo else "Salvar"}</button></div>
    </form>"""


def html_prof_remover(x: dict) -> str:
    return f"""<div class="folha-fundo"></div>
    <form class="folha" method="post" action="/painel/profissionais/{esc(x["id"])}/remover" role="dialog" aria-modal="true" style="height:auto">
      <div class="fcab"><h2>Remover {esc(x.get("nome", ""))}?</h2><a class="fechar" href="/painel/profissionais" aria-label="Fechar">&times;</a></div>
      <div class="fcorpo"><p style="margin-top:12px">O assistente deixa de oferecer horários com {esc(nome_conversa(x.get("nome", "")) or "este profissional")}.
        As consultas que já estão na agenda continuam lá.</p></div>
      <div class="fpe"><a class="btn sec" href="/painel/profissionais">Cancelar</a><button class="btn perigo">Remover</button></div>
    </form>"""


PROF_CSS = """<style>
.hgrade{display:flex;flex-direction:column;gap:6px}
.hdia{display:grid;grid-template-columns:118px 1fr;gap:10px;align-items:center}
.hdia input[type=text]{padding:8px 11px;font-size:14px}
.hdia input[type=text]:disabled{background:var(--papel);color:#A0AEC0}
.sw{display:flex;align-items:center;gap:8px;margin:0;font-size:14.5px;color:var(--tinta);cursor:pointer}
.sw input{appearance:none;-webkit-appearance:none;width:34px;height:20px;border-radius:20px;background:var(--linha);position:relative;cursor:pointer;flex:none;transition:background .15s}
.sw input:before{content:"";position:absolute;top:2px;left:2px;width:16px;height:16px;border-radius:50%;background:#fff;transition:transform .15s;box-shadow:0 1px 2px rgba(0,0,0,.2)}
.sw input:checked{background:var(--tinta)}
.sw input:checked:before{transform:translateX(14px)}
.regras{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}
@media(max-width:700px){.regras{grid-template-columns:1fr}}
</style>"""

PROF_JS = """<script>
(function(){var f=document.querySelector('form.folha');if(!f)return;
var linhas=[].slice.call(f.querySelectorAll('.hdia'));
function campo(l){return l.querySelector('input[type=text]')}
linhas.forEach(function(l,i){var cb=l.querySelector('input[type=checkbox]'),t=campo(l);
  function sinc(){t.disabled=!cb.checked;}
  cb.addEventListener('change',function(){if(cb.checked&&!t.value.trim()){
      for(var j=i-1;j>=0;j--){var v=campo(linhas[j]).value.trim();if(v&&!campo(linhas[j]).disabled){t.value=v;break;}}
      if(!t.value)t.value='8h-12h, 14h-18h';}
    if(!cb.checked)t.dataset.antes=t.value;sinc();if(cb.checked)t.focus();});
  t.addEventListener('input',function(){if(t.value.trim()&&!cb.checked){cb.checked=true;sinc();}});
  sinc();});
f.addEventListener('submit',function(){linhas.forEach(function(l){var t=campo(l);if(t.disabled){t.disabled=false;t.value='';}})});
var cp=document.getElementById('copiar-seg');if(cp)cp.addEventListener('click',function(){var v=campo(linhas[0]).value.trim();if(!v)return;
  for(var i=1;i<5;i++){campo(linhas[i]).value=v;linhas[i].querySelector('input[type=checkbox]').checked=true;campo(linhas[i]).disabled=false;}});
var n=f.querySelector('input[name=prof_nome]');if(n&&!n.value)n.focus();})();
</script>"""


@router.get("/painel/profissionais", response_class=HTMLResponse)
def painel_profissionais(novo: int = 0, editar: str = "", remover: str = "", msg: str = "", s: dict = Depends(exigir_login)):
    conf = obter_conf(s["clinica"]["slug"])
    profs = [x for x in conf["profissionais"] if x.get("nome")]
    por_id = {x.get("id"): x for x in profs}
    aviso = f'<div class="aviso">{esc(msg)}</div>' if msg else ""
    if profs:
        lista = ('<div class="lista">' + "".join(html_prof_card(x) for x in profs)
                 + '<a class="icard icard-novo" href="/painel/profissionais?novo=1"><i>+</i>Adicionar profissional</a></div>')
    else:
        lista = """<div class="vazia">""" + simbolo(56, False) + """
          <h2>Quem atende na clínica?</h2>
          <p>Cadastre cada profissional com os dias e horários. O assistente usa isso para oferecer só horários livres e marcar direto na agenda.</p>
          <a class="btn" href="/painel/profissionais?novo=1">Adicionar o primeiro profissional</a></div>"""
    folha = ""
    if novo:
        folha = html_prof_form({})
    elif editar and editar in por_id:
        folha = html_prof_form(por_id[editar])
    elif remover and remover in por_id:
        folha = html_prof_remover(por_id[remover])
    botao = '<a class="btn" href="/painel/profissionais?novo=1">+ Adicionar</a>' if profs else ""
    corpo = f"""{aviso}<div class="cab"><div><h1>Profissionais</h1>
      <p>O assistente só oferece ao paciente os horários livres de quem está aqui.</p></div>{botao}</div>
    {lista}{folha}"""
    return html_resp(pagina("Profissionais", corpo + PROF_JS, s, "profs", PROF_CSS))


@router.post("/painel/profissionais")
async def painel_profissionais_post(request: Request, s: dict = Depends(exigir_login)):
    slug = s["clinica"]["slug"]
    f = await request.form()
    conf = obter_conf(slug)
    x = {ch: str(f.get(f"prof_{ch}", "")).strip()[:500] for ch, _, _ in PROF_CAMPOS}
    if not x["nome"]:
        return RedirectResponse("/painel/profissionais?novo=1", status_code=303)
    pid = re.sub(r"[^a-z0-9]", "", str(f.get("prof_id", "")))[:16]
    dur = so_digitos(f.get("prof_dur", ""))
    x["agenda"] = {"dias": {str(d): str(f.get(f"prof_ag{d}", "")).strip()[:120] for d in range(7)},
                   "duracao": max(5, min(480, int(dur))) if dur else AG.DURACAO_PADRAO}
    for i, p in enumerate(conf["profissionais"]):
        if pid and p.get("id") == pid:
            conf["profissionais"][i] = {**p, **x, "id": pid}
            m = f"Alterações em {x['nome']} salvas. O assistente já usa na próxima mensagem."
            break
    else:
        x["id"] = secrets.token_hex(4)
        conf["profissionais"].append(x)
        m = f"Pronto: {x['nome']} está na lista. O assistente já usa na próxima mensagem."
    conf["atualizado_em"] = agora().isoformat(timespec="seconds")
    conf["atualizado_por"] = s["usuario"]["email"]
    salvar_conf(slug, conf)
    return RedirectResponse("/painel/profissionais?msg=" + quote(m), status_code=303)


@router.post("/painel/profissionais/{pid}/remover")
def painel_profissionais_remover(pid: str, s: dict = Depends(exigir_login)):
    slug = s["clinica"]["slug"]
    conf = obter_conf(slug)
    antes = len(conf["profissionais"])
    nome = next((p.get("nome", "") for p in conf["profissionais"] if p.get("id") == pid), "")
    conf["profissionais"] = [p for p in conf["profissionais"] if p.get("id") != pid]
    if len(conf["profissionais"]) != antes:
        conf["atualizado_em"] = agora().isoformat(timespec="seconds")
        conf["atualizado_por"] = s["usuario"]["email"]
        salvar_conf(slug, conf)
    return RedirectResponse("/painel/profissionais?msg=" + quote(f"{nome or 'O profissional'} saiu da lista."), status_code=303)


# ============================================================
# LOGO DA CLÍNICA (aparece no menu, no lugar das iniciais)
# ============================================================
@router.get("/painel/logo")
def painel_logo(s: dict = Depends(exigir_login)):
    v = R.get(K(s["clinica"]["slug"], "logo"))
    if not v:
        raise HTTPException(status_code=404)
    d = json.loads(v)
    return Response(base64.b64decode(d["b64"]), media_type=d["tipo"],
                    headers={"Cache-Control": "private, max-age=31536000", "X-Content-Type-Options": "nosniff"})


@router.post("/painel/logo")
async def painel_logo_post(logo: UploadFile = File(None), s: dict = Depends(exigir_login)):
    def volta(m):
        return RedirectResponse("/painel/configuracoes?logo=" + quote(m) + "#logo", status_code=303)
    dados = await logo.read(LOGO_MAX_BYTES + 1) if logo else b""
    if not dados:
        return volta("Escolha uma imagem.")
    if len(dados) > LOGO_MAX_BYTES:
        return volta("A imagem passou de 500 KB. Envie uma menor.")
    tipo = tipo_imagem(dados)
    if not tipo:
        return volta("Envie o logo em PNG, JPG ou WEBP.")
    c = obter_clinica(s["clinica"]["slug"])
    R.set(K(c["slug"], "logo"), json.dumps({"tipo": tipo, "b64": base64.b64encode(dados).decode()}))
    c["logo_v"] = secrets.token_hex(4)
    salvar_clinica(c)
    return volta("Logo atualizado.")


@router.post("/painel/logo/remover")
def painel_logo_remover(s: dict = Depends(exigir_login)):
    c = obter_clinica(s["clinica"]["slug"])
    R.delete(K(c["slug"], "logo"))
    c.pop("logo_v", None)
    salvar_clinica(c)
    return RedirectResponse("/painel/configuracoes?logo=" + quote("Logo removido.") + "#logo", status_code=303)


def html_card_logo(c: dict, msg: str = "") -> str:
    tem = bool(c.get("logo_v"))
    remover = ('<form method="post" action="/painel/logo/remover" class="in">'
               '<button class="btn sec peq">Tirar o logo</button></form>') if tem else ""
    return f"""<div class="card" id="logo"><h2>Logo da clínica</h2>
      {f'<div class="aviso">{esc(msg)}</div>' if msg else ''}
      <div class="linha" style="align-items:center">
        <span class="logo-prev">{html_logo_clinica(c)}</span>
        <div style="flex:1;min-width:220px"><p style="font-size:14.5px">Aparece no menu, ao lado do nome da clínica.
          {'' if tem else 'Sem logo, a gente mostra as iniciais.'}</p>
          <p style="font-size:13px;color:var(--txt2)">PNG, JPG ou WEBP, até 500 KB. Quadrado fica melhor.</p></div>
      </div>
      <div class="linha" style="margin-top:12px">
        <form method="post" action="/painel/logo" enctype="multipart/form-data" class="linha" id="form-logo">
          <label class="btn" style="margin:0;color:#fff">{'Trocar o logo' if tem else 'Enviar logo'}
            <input type="file" name="logo" accept="image/png,image/jpeg,image/webp" hidden
              onchange="if(this.files.length)this.form.submit()"></label>
          <noscript><button class="btn sec">Enviar</button></noscript>
        </form>{remover}</div></div>"""


# ============================================================
# SUA CLÍNICA (o que o assistente sabe sobre a clínica)
# ============================================================
@router.get("/painel/configuracoes", response_class=HTMLResponse)
def painel_config(salvo: str = "", logo: str = "", s: dict = Depends(exigir_login)):
    c = s["clinica"]
    conf = obter_conf(c["slug"])
    profs = [x for x in conf["profissionais"] if x.get("nome")]

    def campo(ch, rot, ex, area=False):
        if area:
            return f'<label>{rot}</label><textarea name="{ch}" placeholder="{esc(ex)}">{esc(conf.get(ch, ""))}</textarea>'
        return f'<label>{rot}</label><input type="text" name="{ch}" value="{esc(conf.get(ch, ""))}" placeholder="{esc(ex)}">'

    aviso = '<div class="aviso">Salvo. O assistente já usa as novas informações na próxima mensagem.</div>' if salvo else ""
    if profs:
        nomes = ", ".join(esc(x["nome"]) for x in profs[:4]) + (f" e mais {len(profs) - 4}" if len(profs) > 4 else "")
        com_ag = sum(1 for x in profs if AG.tem_agenda(x))
        resumo_profs = (f'<p style="font-size:14.5px"><b class="mono">{len(profs)}</b> cadastrados: {nomes}.</p>'
                        f'<p style="font-size:13px;color:var(--txt2);margin-top:2px">{com_ag} com horários na agenda.</p>')
    else:
        resumo_profs = '<p style="font-size:14.5px;color:var(--txt2)">Ninguém cadastrado ainda.</p>'
    corpo = f"""{aviso}<div class="cab"><div><h1>Sua clínica</h1>
      <p>Tudo o que estiver aqui o assistente usa para responder. O que não estiver, ele passa para a equipe.</p></div></div>
    {html_card_logo(c, logo)}
    <form method="post">
    <div class="card"><h2>A clínica</h2>
      <div class="grid2">
        <div>{campo('nome_assistente', 'Como o assistente se apresenta', 'Assistente da ' + c['nome'])}</div>
        <div>{campo('endereco', 'Endereço', 'Rua X, 123, sala 4, Porto Alegre')}</div>
        <div>{campo('funcionamento', 'Horário de funcionamento', 'Seg a sex 8h–19h; sáb 8h–12h')}</div>
        <div>{campo('pagamento', 'Formas de pagamento', 'Pix, cartão em até 6x')}</div>
      </div>
      {campo('convenios', 'Convênios aceitos pela clínica', 'Unimed Odonto, Amil Dental, OdontoPrev', True)}
    </div>
    <div class="card"><div class="linha" style="justify-content:space-between;margin-bottom:8px"><h2 style="margin:0">Profissionais</h2>
      <a class="btn sec peq" href="/painel/profissionais">{'Ver e editar' if profs else 'Adicionar'}</a></div>{resumo_profs}</div>
    <div class="card"><h2>Agenda</h2>
      <div class="grid2">
        <div>{campo('ag_antecedencia_h', 'Antecedência mínima para o assistente marcar (horas)', str(AG.ANTECEDENCIA_H_PADRAO))}</div>
        <div>{campo('ag_janela_dias', 'Marcar até quantos dias à frente', str(AG.JANELA_DIAS_PADRAO))}</div>
        <div>{campo('ag_antecedencia_mudar_h', 'Remarcar ou cancelar pelo assistente até quantas horas antes', str(AG.ANTECEDENCIA_MUDAR_H_PADRAO))}</div>
      </div>
      <p style="font-size:13px;color:var(--txt2)">Mais perto da consulta que isso, o assistente passa o pedido de remarcar ou cancelar para a equipe.</p>
      <label style="margin-top:14px;display:flex;gap:8px;align-items:flex-start;color:var(--tinta);font-size:14.5px"><input type="checkbox" name="atende_feriados" value="1"{' checked' if conf.get('atende_feriados') == '1' else ''} style="margin-top:4px">
        <span>A clínica atende em feriados nacionais (Natal, Ano-Novo, Sexta-feira Santa...)</span></label>
      <p style="font-size:13px;color:var(--txt2)">Desmarcado, o assistente não oferece horário nesses dias. Feriado da cidade ou do estado: use Bloquear horário na Agenda.</p>
      </div>
    <div class="card"><h2>Regras</h2>
      {campo('encaminhar_quando', 'Quando passar para a equipe, além do padrão', 'Pedido de orçamento de implante; paciente pede a Dra. Ana direto', True)}
      {campo('extra', 'Outras informações e regras', 'Primeira consulta de avaliação é gratuita. Não informar preços de tratamento pelo WhatsApp.', True)}
      <p style="font-size:13px;color:var(--txt2);margin-top:8px">Por padrão o assistente já passa para a equipe quando o paciente pede uma pessoa,
      em urgência ou dor forte, reclamação, remarcação ou cancelamento em cima da hora, ou pergunta que não está aqui.</p>
    </div>
    <div class="linha" style="margin-bottom:16px"><button class="btn">Salvar</button>
      <a href="/painel/experimentar" style="font-size:14px">Ver como ele responde</a></div>
    </form>
    <div class="card"><details><summary>Ver o texto que o assistente recebe</summary><pre>{esc(montar_prompt(c, conf))}</pre></details></div>
    <div class="card"><h2>Acesso</h2><p style="font-size:14px">Você entrou como <b>{esc(s['usuario']['email'])}</b>.
      <a href="/painel/senha">Trocar senha</a></p></div>"""
    return html_resp(pagina("Sua clínica", corpo, s, "config"))


@router.post("/painel/configuracoes")
async def painel_config_post(request: Request, s: dict = Depends(exigir_login)):
    slug = s["clinica"]["slug"]
    f = await request.form()
    conf = obter_conf(slug)
    for ch in ("nome_assistente", "endereco", "funcionamento", "pagamento", "convenios", "encaminhar_quando", "extra"):
        conf[ch] = str(f.get(ch, "")).strip()[:4000]
    conf["atende_feriados"] = "1" if f.get("atende_feriados") else ""
    for ch, maximo in (("ag_antecedencia_h", 240), ("ag_janela_dias", 180), ("ag_antecedencia_mudar_h", 720)):
        v = so_digitos(f.get(ch, ""))
        conf[ch] = str(min(int(v), maximo)) if v else ""
    # os profissionais têm tela própria (/painel/profissionais): aqui não mexe neles
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
        if situacao(c) != "ativa" and not c.get("desativada"):
            continue          # quem ainda nao pagou aparece so em "leads"
        st = await status_conexao(c)
        n_users = R.scard(K(c["slug"], "usuarios"))
        linhas.append(f"""<tr><td><b><a href="/admin/clinicas/{c['slug']}">{esc(c['nome'])}</a></b><br>
          <span class="mono" style="font-size:12px">{esc(c['slug'])}</span></td>
          <td>{esc(st['estado'])}</td><td>{'ligado' if c.get('bot_ativo') else 'desligado'}</td>
          <td>{n_users}</td><td>{'desativada' if c.get('desativada') else esc(rotulo_situacao(c))}</td></tr>""")
    tabela = ("<table><tr><th>Clínica</th><th>WhatsApp</th><th>Assistente</th><th>Usuários</th><th>Situação</th></tr>"
              + "".join(linhas) + "</table>") if linhas else '<div class="vazio">Nenhuma clínica assinante ainda.</div>'
    aviso = f'<div class="aviso">{esc(msg)}</div>' if msg else ""
    corpo = f"""<p style="margin-bottom:12px"><a href="/admin">Painel admin</a></p>{aviso}
    {html_leads()}
    <div class="card"><h2>Clínicas (assinantes)</h2>{tabela}</div>
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
    users = "".join(f"""<li style="margin-bottom:6px">{esc(u.get('nome') or '')} &lt;{esc(u['email'])}&gt;
        <form method="post" action="/admin/clinicas/{slug}/usuario/remover" style="display:inline">
        <input type="hidden" name="email" value="{esc(u['email'])}"><button class="btn sec peq">Remover acesso</button></form></li>"""
        for u in usuarios_da_clinica(slug))
    a = c.get("assinatura") or {}
    alerta_apagar = ""
    if a.get("preapproval_id") and situacao(c) == "ativa":
        alerta_apagar += ("<p style='font-size:13px;color:#9b1c1c'>Esta clínica tem assinatura ativa no Mercado Pago. "
                          "Apagar aqui não cancela a cobrança: cancele antes no Mercado Pago.</p>")
    if zapi_configurada(c):
        alerta_apagar += ("<p style='font-size:13px;color:#9b1c1c'>Esta clínica tem instância Z-API. Apagar aqui não "
                          "apaga a instância: cancele na Z-API se não for mais usar.</p>")
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
      {f'<form method="post" action="/admin/clinicas/{slug}/criar-zapi" style="margin-top:8px"><button class="btn sec peq">Criar e assinar a instância Z-API agora (parceiro)</button></form>' if not zapi_configurada(c) and ZAPI_PARTNER else ''}
    </div>
    {html_exp_admin(c)}
    <div class="card"><h2>Teste sem Mercado Pago</h2>
      {('<p style="font-size:14px">Pagamento simulado ativo. O painel está liberado como se a clínica tivesse pago.</p>'
        f'<form method="post" action="/admin/clinicas/{slug}/simular-pagamento/desfazer"><button class="btn sec peq">Desfazer simulação (volta para lead)</button></form>')
       if (c.get('assinatura') or {}).get('simulado') else
       ('<p style="font-size:14px">Faz o mesmo que um pagamento aprovado no Mercado Pago: libera o painel, passa a clínica para assinantes '
        'e manda o aviso "Clínica nova pagou" para vocês (e-mail e WhatsApp). Não cobra nada.</p>'
        f'<form method="post" action="/admin/clinicas/{slug}/simular-pagamento"><button class="btn sec peq">Simular pagamento aprovado</button></form>')
       if situacao(c) != 'ativa' else
       '<p style="font-size:14px;color:var(--txt2)">A clínica já está ativa.</p>'}
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
      <div style="margin-top:12px"><button class="btn sec">Salvar acesso</button></div></form>
    <form method="post" action="/admin/clinicas/{slug}/apagar" class="card"><h2>Apagar cadastro</h2>
      <p style="font-size:14px">Apaga a clínica, os acessos (e-mails de login), as conversas e os números dela. Não tem volta.
      Depois disso o e-mail fica livre para criar a conta de novo.</p>{alerta_apagar}
      <label>Para confirmar, digite o endereço da clínica: <span class="mono">{esc(slug)}</span></label>
      <input type="text" name="confirmar" autocomplete="off" required>
      <div style="margin-top:12px"><button class="btn sec">Apagar de vez</button></div></form>"""
    return html_resp(pagina(c["nome"], corpo))


def apagar_usuario(email: str, slug: str):
    email = email.strip().lower()
    encerrar_sessoes(email)
    R.delete(f"cl:usuario:{email}", f"cl:assinando:{email}")
    R.srem(K(slug, "usuarios"), email)


def apagar_clinica(slug: str):
    c = obter_clinica(slug) or {}
    for e in list(R.smembers(K(slug, "usuarios"))):
        u = obter_usuario(e)
        if not u or u.get("slug") == slug:
            apagar_usuario(e, slug)
    pid = (c.get("assinatura") or {}).get("preapproval_id")
    if pid and R.get(f"cl:preapproval:{pid}") == slug:
        R.delete(f"cl:preapproval:{pid}")
    chaves = list(R.scan_iter(match=K(slug, "*"), count=500))
    for i in range(0, len(chaves), 500):
        R.delete(*chaves[i:i + 500])
    R.delete(K(slug, "cfg"))
    R.srem("cl:index", slug)


@router.post("/admin/clinicas/{slug}/apagar")
async def admin_clinica_apagar(slug: str, request: Request, admin: str = Depends(exigir_admin)):
    c = obter_clinica(slug)
    if not c:
        raise HTTPException(status_code=404)
    f = await request.form()
    if str(f.get("confirmar", "")).strip() != slug:
        return RedirectResponse(f"/admin/clinicas/{slug}?msg=Nao+apagado:+o+endereco+digitado+nao+confere.", status_code=303)
    apagar_clinica(slug)
    print(f"admin {admin} apagou a clinica {slug}")
    return RedirectResponse("/admin/clinicas?msg=" + quote(f"Cadastro {c.get('nome', slug)} apagado."), status_code=303)


@router.post("/admin/clinicas/{slug}/simular-pagamento")
async def admin_simular_pagamento(slug: str, admin: str = Depends(exigir_admin)):
    c = obter_clinica(slug)
    if not c:
        raise HTTPException(status_code=404)
    if situacao(c) == "ativa":
        return RedirectResponse(f"/admin/clinicas/{slug}?msg=A+clinica+ja+esta+ativa.", status_code=303)
    c["assinatura"] = {**(c.get("assinatura") or {}), "simulado": True}
    c.pop("aviso_pago_enviado", None)
    salvar_clinica(c)
    await aplicar_assinatura(slug, {"id": f"simulado-{slug}", "status": "authorized",
                                    "auto_recurring": {"transaction_amount": valor_plano()}})
    print(f"admin {admin} simulou pagamento da clinica {slug}")
    return RedirectResponse(f"/admin/clinicas/{slug}?msg=" + quote("Pagamento simulado: painel liberado e aviso enviado."), status_code=303)


@router.post("/admin/clinicas/{slug}/simular-pagamento/desfazer")
async def admin_simular_desfazer(slug: str, admin: str = Depends(exigir_admin)):
    c = obter_clinica(slug)
    if not c:
        raise HTTPException(status_code=404)
    if not (c.get("assinatura") or {}).get("simulado"):
        return RedirectResponse(f"/admin/clinicas/{slug}?msg=Nao+ha+simulacao+para+desfazer.", status_code=303)
    R.delete(f"cl:preapproval:simulado-{slug}")
    c.pop("assinatura", None)
    c.pop("aviso_pago_enviado", None)
    c["situacao"] = "pendente"
    c["bot_ativo"] = False
    salvar_clinica(c)
    return RedirectResponse(f"/admin/clinicas/{slug}?msg=" + quote("Simulação desfeita: a clínica voltou para leads."), status_code=303)


@router.post("/admin/clinicas/{slug}/usuario/remover")
async def admin_usuario_remover(slug: str, request: Request, admin: str = Depends(exigir_admin)):
    f = await request.form()
    email = str(f.get("email", "")).strip().lower()
    u = obter_usuario(email)
    if not u or u.get("slug") != slug:
        return RedirectResponse(f"/admin/clinicas/{slug}?msg=Acesso+nao+encontrado.", status_code=303)
    apagar_usuario(email, slug)
    return RedirectResponse(f"/admin/clinicas/{slug}?msg=" + quote(f"Acesso {email} removido."), status_code=303)


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
ZAPI_PARTNER   = os.environ.get("ZAPI_PARTNER_TOKEN", "")          # token de parceiro (integrador) da Z-API
ZAPI_CONTA_CT  = os.environ.get("ZAPI_CLIENT_TOKEN", "")           # Client-Token da conta Z-API (vale para todas as instancias)
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
    if MP_TOKEN.startswith("TEST-"):
        falta += ('<div class="aviso erro">O token do Mercado Pago é de teste (começa com TEST-). Para assinatura o Mercado Pago '
                  'não aceita esse token: o link de pagamento abre uma página que não existe. Para testar, use o Access Token '
                  'de produção (APP_USR-) de um <b>vendedor de teste</b> e pague logado com o <b>comprador de teste</b>. '
                  'Para cobrar de verdade, use o Access Token de produção da conta de vocês.</div>')
    return f"""<form method="post" action="/admin/plano" class="card"><h2>Plano (o que a clínica paga ao criar a conta)</h2>{falta}
      <div class="grid2"><div><label>Nome que aparece no Mercado Pago</label><input type="text" name="nome" value="{esc(p.get('nome', ''))}"></div>
      <div><label>Mensalidade em R$ (tudo incluído)</label><input type="text" name="valor" value="{esc(p.get('valor', ''))}" placeholder="ex.: 497"></div></div>
      <p style="font-size:13px;color:var(--txt2);margin-top:8px">Vale para as próximas assinaturas. Quem já assinou continua no valor que assinou.
      {('Plano no Mercado Pago: <span class="mono">' + esc(p.get('mp_plan_id', '')) + '</span>') if p.get('mp_plan_id') else 'Ainda não criado no Mercado Pago.'}</p>
      <div style="margin-top:10px"><button class="btn sec">Salvar plano</button></div></form>"""


@router.post("/admin/plano")
async def admin_plano(request: Request, admin: str = Depends(exigir_admin)):
    f = await request.form()
    R.set("cl:plano", json.dumps({"nome": str(f.get("nome", "")).strip()[:120] or "a gente assume",
                                  "valor": str(f.get("valor", "")).strip().replace("R$", "").replace(",", ".").strip()}))
    plano, erro = await garantir_plano_mp()
    msg = f"Plano salvo e criado no Mercado Pago ({plano['mp_plan_id']})." if plano else f"Plano salvo, mas: {erro}"
    return RedirectResponse("/admin/clinicas?msg=" + quote(msg), status_code=303)


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
      <div style="margin-top:16px"><button class="btn" style="width:100%">Criar conta</button></div>
    </form>
    <p style="font-size:14px">Já tem conta? <a href="/entrar">Entrar</a></p>"""
    return tela_publica("Criar conta", f"{preco}Crie a conta, conheça o painel e assine quando quiser ligar o assistente.", miolo, erro)


@router.post("/criar-conta")
async def criar_conta_post(request: Request, clinica: str = Form(""), nome: str = Form(""), whatsapp: str = Form(""),
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
    tarefa = asyncio.create_task(avisar_equipe_aga(
        f"Cadastro novo (ainda não assinou): {clinica}\nContato: {nome.strip()} / {email} / {fmt_tel(whatsapp)}\n"
        f"{BASE_URL}/admin/clinicas/{slug}", "Cadastro novo na plataforma"))
    _tarefas.add(tarefa)
    tarefa.add_done_callback(_tarefas.discard)
    resp = RedirectResponse("/painel?msg=Conta+criada.+Bem-vindo!", status_code=303)
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
    if a.get("preapproval_id"):
        R.set(f"cl:preapproval:{a['preapproval_id']}", slug)
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
        criada = await criar_instancia_zapi(slug) if not zapi_configurada(c) else None
        if criada is True:
            await avisar_equipe_aga(
                f"Clínica nova pagou: {c['nome']}\nContato: {donos} / {fmt_tel(c.get('recepcao_tel', ''))}\n"
                f"O número dela na Z-API já foi criado e assinado sozinho. Ela conecta lendo o QR Code no painel.",
                "Clínica nova pagou")
        else:
            motivo = f"\nA criação automática falhou: {criada}" if isinstance(criada, str) else ""
            await avisar_equipe_aga(
                f"Clínica nova pagou: {c['nome']}\nContato: {donos} / {fmt_tel(c.get('recepcao_tel', ''))}\n"
                f"Criar a instância Z-API dela e preencher em {BASE_URL}/admin/clinicas/{slug} (prazo prometido: 24h).{motivo}",
                "Clínica nova pagou: criar o WhatsApp dela")
    elif antes == "ativa" and c["situacao"] == "bloqueada":
        extra = (f"\nSe ela não voltar, cancele a instância Z-API dela ({c.get('zapi_instance')}) no painel da Z-API, "
                 "para não seguir pagando por ela.") if c.get("zapi_instance") else ""
        await avisar_equipe_aga(f"A assinatura da {c['nome']} ficou {status_mp} no Mercado Pago. O assistente dela foi desligado.{extra}",
                                "Assinatura parada")
    return c


async def criar_instancia_zapi(slug: str) -> bool | str | None:
    """Cria e assina a instancia Z-API da clinica pela API de parceiro (integrador).
    Devolve True se deu certo, None se nao ha token de parceiro, ou o texto do erro."""
    if not ZAPI_PARTNER:
        return None
    c = obter_clinica(slug)
    hd = {"Authorization": f"Bearer {ZAPI_PARTNER}", "Content-Type": "application/json"}
    corpo = {"name": f"aga-{slug}"[:60], "sessionName": c["nome"][:60], "receivedCallbackUrl": url_webhook(c),
             "isDevice": False, "businessDevice": True}
    try:
        async with httpx.AsyncClient(timeout=30) as http:
            r1 = await http.post("https://api.z-api.io/instances/integrator/on-demand", headers=hd, json=corpo)
            d = r1.json() if r1.status_code < 300 else {}
            if not d.get("id") or not d.get("token"):
                return f"criar instância: {r1.status_code} {r1.text[:200]}"
            c = obter_clinica(slug)
            c.update({"zapi_instance": d["id"], "zapi_token": d["token"], "zapi_client_token": ZAPI_CONTA_CT,
                      "zapi_criada_em": agora().isoformat(timespec="seconds"), "zapi_assinada": False})
            salvar_clinica(c)
            r2 = await http.post(f"https://api.z-api.io/instances/{d['id']}/token/{d['token']}/integrator/on-demand/subscription",
                                 headers=hd, json={})
            if r2.status_code >= 300:
                return f"instância {d['id']} criada, mas a assinatura falhou ({r2.status_code} {r2.text[:200]}). Sem assinar, ela some em 2 dias."
            c = obter_clinica(slug)
            c["zapi_assinada"] = True
            salvar_clinica(c)
        if ZAPI_CONTA_CT:
            try:
                await configurar_webhook_zapi(obter_clinica(slug))   # liga "notificar enviadas por mim"
            except Exception as e:
                print(f"ZAPI webhook {slug}: {e}")
        print(f"ZAPI instancia criada e assinada para {slug}: {d['id']}")
        return True
    except Exception as e:
        return f"{type(e).__name__}: {e}"


async def reverificar_assinatura(c: dict) -> dict:
    a = c.get("assinatura") or {}
    pid = a.get("preapproval_id")
    if not pid or not MP_TOKEN or a.get("simulado"):
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
        pid = None
        if "preapproval" in tipo or "subscription" in tipo:
            pid = id_mp
            if "authorized_payment" in tipo:
                code, d = await mp("GET", f"/authorized_payments/{id_mp}")
                pid = d.get("preapproval_id") if code == 200 else None
        elif "payment" in tipo:
            code, d = await mp("GET", f"/v1/payments/{id_mp}")
            if code != 200:
                return False
            pid = (d.get("metadata") or {}).get("preapproval_id") or d.get("preapproval_id")
            ref = str(d.get("external_reference", ""))
            if not pid:
                return ref.startswith("clinica:")
        if not pid:
            return False
        code, d = await mp("GET", f"/preapproval/{pid}")
        if code != 200:
            return False
        slug = slug_da_assinatura(d)
        if not slug:
            return False
        await aplicar_assinatura(slug, d)
        return True
    except Exception as e:
        print(f"MP webhook clinica erro: {e}")
        return False


def slug_da_assinatura(d: dict) -> str | None:
    """Descobre de qual clinica e uma assinatura do MP: pela referencia ou pelo vinculo salvo na volta do pagamento."""
    ref = str(d.get("external_reference") or "")
    if ref.startswith("clinica:"):
        return ref.split(":", 1)[1]
    return R.get(f"cl:preapproval:{d.get('id')}")


async def garantir_plano_mp() -> tuple[dict | None, str]:
    """Cria (ou reaproveita) o plano de assinatura no MP para o valor atual. Devolve (plano, erro)."""
    p = obter_plano()
    v = valor_plano()
    if not v:
        return None, "Defina o valor da mensalidade."
    if not MP_TOKEN:
        return None, "Falta o token do Mercado Pago."
    if p.get("mp_plan_id") and p.get("mp_valor") == v and p.get("mp_init_point"):
        return p, ""
    corpo = {"reason": p.get("nome") or "a gente assume", "back_url": f"{BASE_URL}/assinar/retorno",
             "auto_recurring": {"frequency": 1, "frequency_type": "months", "transaction_amount": v, "currency_id": "BRL"}}
    code, d = await mp("POST", "/preapproval_plan", corpo)
    if code >= 300 or not d.get("id"):
        print(f"MP criar plano: {code} {str(d)[:400]}")
        return None, f"Mercado Pago recusou criar o plano ({code}: {d.get('message', '')})"
    p.update({"mp_plan_id": d["id"], "mp_valor": v, "mp_init_point": d.get("init_point")
              or f"https://www.mercadopago.com.br/subscriptions/checkout?preapproval_plan_id={d['id']}"})
    R.set("cl:plano", json.dumps(p))
    print(f"MP plano criado {d['id']} init_point={p['mp_init_point']}")
    return p, ""


def html_leads() -> str:
    linhas = []
    for c in sorted(listar_clinicas(), key=lambda x: x.get("criado_em", ""), reverse=True):
        if situacao(c) == "ativa" or c.get("desativada"):
            continue
        us = usuarios_da_clinica(c["slug"])
        contato = ", ".join(f"{esc(u.get('nome') or '')} &lt;{esc(u['email'])}&gt;" for u in us)
        tel = so_digitos(c.get("recepcao_tel", ""))
        wa = f'<a href="https://wa.me/{tel_envio(tel)}" target="_blank" rel="noopener">{fmt_tel(tel)}</a>' if tel else ""
        linhas.append(f"""<tr><td><a href="/admin/clinicas/{c['slug']}">{esc(c['nome'])}</a></td><td>{contato}<br>{wa}</td>
          <td>{esc(rotulo_situacao(c))}</td><td>{html_exp_resumo(c)}</td><td>{ha_quanto(c.get('criado_em', ''))}</td></tr>""")
    if not linhas:
        return ""
    return ('<div class="card"><h2>Cadastros sem assinatura (leads)</h2><table><tr><th>Clínica</th><th>Contato</th>'
            '<th>Situação</th><th>Experimentou</th><th>Cadastro</th></tr>' + "".join(linhas) + "</table></div>")


def html_faixa_assinatura(c: dict, convite_experimentar: bool = True) -> str:
    sit = situacao(c)
    if sit == "ativa":
        return ""
    v = valor_plano()
    preco = f" ({fmt_reais(v)} por mês, tudo incluído)" if v else ""
    if sit == "bloqueada":
        return faixa("assinatura", 'O pagamento da assinatura parou e o assistente está desligado. '
                     '<a href="/assinar">Assinar de novo</a>', erro=True)
    convite = (' ou <a href="/painel/experimentar">veja antes como ele responde</a>' if convite_experimentar else "")
    return faixa("assinatura", f'Falta assinar para a gente ligar o WhatsApp da clínica{preco}. '
                 f'<a href="/assinar">Assinar agora</a>{convite}')


@router.get("/assinar", response_class=HTMLResponse)
async def assinar(request: Request, erro: str = "", s: dict = Depends(exigir_login)):
    c = await reverificar_assinatura(s["clinica"])
    if situacao(c) == "ativa":
        return RedirectResponse("/painel", status_code=303)
    v = valor_plano()
    if not v or not MP_TOKEN:
        miolo = '<div class="card"><p>O pagamento ainda não está disponível. A gente avisa você pelo WhatsApp.</p></div>'
    else:
        miolo = f"""<div class="card">
          <p class="mono" style="font-size:28px;font-weight:600">{fmt_reais(v)}<span style="font-size:14px;color:var(--txt2)"> por mês</span></p>
          <p style="color:var(--txt2);font-size:14px;margin:6px 0 14px">Tudo incluído: o assistente no WhatsApp da clínica, o painel e o número.
          Cobrança mensal pelo Mercado Pago. Você cancela quando quiser.</p>
          <form method="post" action="/assinar"><button class="btn" style="width:100%">Pagar com Mercado Pago</button></form></div>"""
    sub = ("O pagamento da sua assinatura parou. Para voltar a usar, assine de novo." if situacao(c) == "bloqueada"
           else f"Assine para ligar o assistente da {esc(c['nome'])}.")
    miolo += '<p style="font-size:14px"><a href="/painel">Voltar ao painel</a></p>'
    return tela_publica("Assinatura", sub, miolo, erro)


@router.post("/assinar")
async def assinar_post(s: dict = Depends(exigir_login)):
    c = s["clinica"]
    plano, erro = await garantir_plano_mp()
    if not plano:
        print(f"MP assinar {c['slug']}: {erro}")
        return RedirectResponse("/assinar?erro=Nao+deu+para+abrir+o+pagamento.+Tente+de+novo+em+instantes.", status_code=303)
    c = obter_clinica(c["slug"])
    c["assinatura"] = {**(c.get("assinatura") or {}), "iniciada_em": agora().isoformat(timespec="seconds"),
                       "valor": plano["mp_valor"], "plano_id": plano["mp_plan_id"]}
    salvar_clinica(c)
    R.set(f"cl:assinando:{s['usuario']['email']}", c["slug"], ex=86400)
    sep = "&" if "?" in plano["mp_init_point"] else "?"
    return RedirectResponse(f"{plano['mp_init_point']}{sep}external_reference={quote('clinica:' + c['slug'])}", status_code=303)


@router.get("/assinar/retorno")
async def assinar_retorno(request: Request, preapproval_id: str = ""):
    s = sessao_atual(request)
    if not s:
        return RedirectResponse("/entrar", status_code=303)
    c = s["clinica"]
    if preapproval_id and MP_TOKEN:
        code, d = await mp("GET", f"/preapproval/{preapproval_id}")
        if code == 200:
            slug = slug_da_assinatura(d)
            plano = obter_plano()
            if not slug and d.get("preapproval_plan_id") and d.get("preapproval_plan_id") == plano.get("mp_plan_id"):
                slug = c["slug"]      # sem referencia: vincula a quem esta logado e acabou de pagar
            if slug == c["slug"]:
                R.set(f"cl:preapproval:{preapproval_id}", slug)
                c = await aplicar_assinatura(slug, d) or c
            else:
                print(f"MP retorno: preapproval {preapproval_id} de outra clinica ({slug}), sessao {c['slug']}")
        else:
            print(f"MP retorno {preapproval_id}: {code} {str(d)[:300]}")
    if situacao(c) == "ativa":
        return RedirectResponse("/painel?msg=Pagamento+confirmado.+Bem-vindo!", status_code=303)
    return RedirectResponse("/painel?msg=O+pagamento+ainda+nao+foi+confirmado.+Se+voce+acabou+de+pagar,+atualize+em+1+minuto.",
                            status_code=303)


# ---------- conectar o WhatsApp (QR Code da Z-API) ----------
@router.get("/painel/whatsapp", response_class=HTMLResponse)
async def painel_whatsapp(s: dict = Depends(exigir_login)):
    c = s["clinica"]
    if situacao(c) != "ativa":
        return RedirectResponse("/assinar", status_code=303)
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


@router.post("/admin/clinicas/{slug}/criar-zapi")
async def admin_criar_zapi(slug: str, admin: str = Depends(exigir_admin)):
    c = obter_clinica(slug)
    if not c:
        raise HTTPException(status_code=404)
    if zapi_configurada(c):
        return RedirectResponse(f"/admin/clinicas/{slug}?msg=Essa+clinica+ja+tem+Z-API.", status_code=303)
    res = await criar_instancia_zapi(slug)
    msg = "Instancia criada e assinada." if res is True else f"Nao deu: {res or 'falta ZAPI_PARTNER_TOKEN'}"
    return RedirectResponse(f"/admin/clinicas/{slug}?msg=" + quote(msg), status_code=303)


# ============================================================
# EXPERIMENTAR O ASSISTENTE (antes de pagar ou de ligar o WhatsApp)
# ============================================================
# A clinica conversa com o assistente fazendo o papel de paciente, com as configuracoes dela.
# Nada sai pelo WhatsApp. Limite de mensagens por dia (padrao 60; o admin pode mudar por clinica).
# Chaves: cl:{slug}:exp:hist (lista), cl:{slug}:exp:info (hash: total, primeira_em, ultima_em,
#         pedido_cota_em), cl:{slug}:exp:uso:{AAAA-MM-DD} (contador do dia)
EXP_LIMITE_PADRAO = int(os.environ.get("CLINICA_EXPERIMENTAR_LIMITE", "60"))
EXP_MAX_CHARS = 1000
EXP_TEL = "experimentar"   # "telefone" do paciente de mentira nas reservas de simulacao


def exp_limite(c: dict) -> int:
    try:
        return int(c.get("exp_limite") or EXP_LIMITE_PADRAO)
    except (TypeError, ValueError):
        return EXP_LIMITE_PADRAO


def exp_uso_hoje(slug: str) -> int:
    return int(R.get(K(slug, "exp", "uso", agora().strftime("%Y-%m-%d"))) or 0)


def exp_info(slug: str) -> dict:
    return R.hgetall(K(slug, "exp", "info"))


def exp_hist(slug: str) -> list:
    return [json.loads(x) for x in R.lrange(K(slug, "exp", "hist"), -HIST_LIMITE, -1)]


def exp_registrar(slug: str, papel: str, texto: str):
    hk = K(slug, "exp", "hist")
    R.rpush(hk, json.dumps({"papel": papel, "texto": texto, "em": agora().isoformat(timespec="seconds")}, ensure_ascii=False))
    R.ltrim(hk, -HIST_LIMITE, -1)


def exp_mensagens_para_ia(slug: str) -> list:
    msgs = []
    for m in exp_hist(slug)[-HIST_PROMPT:]:
        if m["papel"] not in ("paciente", "assistente"):
            continue
        role = "user" if m["papel"] == "paciente" else "assistant"
        if msgs and msgs[-1]["role"] == role:
            msgs[-1]["content"] += "\n" + m["texto"]
        else:
            msgs.append({"role": role, "content": m["texto"]})
    while msgs and msgs[0]["role"] != "user":
        msgs.pop(0)
    return msgs


async def exp_responder(c: dict):
    slug = c["slug"]
    msgs = exp_mensagens_para_ia(slug)
    if not msgs or msgs[-1]["role"] != "user":
        return
    dt = agora()
    conf = obter_conf(slug)
    system = montar_prompt(c, conf) + contexto_tempo(conf)
    ctx = {"slug": slug, "conf": conf, "tel": EXP_TEL, "sim": True, "reservas": []}
    bruto = await conversar_ia(system, msgs, ctx, K(slug, "tokens_exp", dt.strftime("%Y-%m")))
    texto, motivo, pedido = interpretar_resposta(bruto)
    if texto:
        exp_registrar(slug, "assistente", texto)
    for m in ctx.get("mudancas", []):
        exp_registrar(slug, "nota", (
            f"Consulta de simulação {m['acao']}: era {m['antes']}"
            + (f"; agora {AG.quando_txt(m['item'])} com {m['item']['prof_nome']}" if m["acao"] == "remarcada" else "")
            + ". Com o assistente ligado, isso mudaria a agenda de verdade, a recepção receberia o aviso no WhatsApp "
            "e entraria na contagem do painel."))
    for it in ctx["reservas"]:
        exp_registrar(slug, "nota", (
            f"Reserva de simulação na agenda: {it.get('paciente') or 'paciente'}, {AG.quando_txt(it)} com {it['prof_nome']}. "
            "Com o assistente ligado, ela entraria na agenda de verdade e a recepção receberia o aviso no WhatsApp. "
            "Veja em Agenda (marque \"mostrar simulação\")."))
    if pedido:
        exp_registrar(slug, "nota", (
            f"Aqui a recepção receberia no WhatsApp um pedido de agendamento: {pedido['nome'] or 'paciente'}, "
            f"com {pedido['profissional'] or '-'}, {pedido['quando'] or '-'}, {pedido['convenio'] or '-'}. "
            "Para o assistente marcar direto na agenda, preencha os horários de cada um em Profissionais."))
    if motivo:
        exp_registrar(slug, "nota", (
            f"Aqui o assistente pausaria e a recepção receberia um aviso para continuar a conversa. Motivo: {motivo}."))


def exp_sugestoes(conf: dict) -> list:
    profs = [x for x in conf.get("profissionais", []) if x.get("nome")]
    sug = ["Oi, queria marcar uma consulta"]
    if profs:
        sug.append(f"Tem horário com {nome_conversa(profs[0]['nome'])} essa semana?")
    conv1 = next((x.strip() for x in (conf.get("convenios") or "").split(",") if x.strip()), "")
    sug.append(f"Vocês atendem {conv1}?" if conv1 else "Vocês atendem convênio?")
    sug += ["Quanto custa uma limpeza?", "Estou com muita dor de dente"]
    return sug


def html_exp_msgs(slug: str, sugestoes: list | None = None) -> str:
    itens = []
    for m in exp_hist(slug):
        hora = m.get("em", "")[11:16]
        if m["papel"] == "nota":
            itens.append(f'<div class="exp-nota">{esc(m["texto"])}</div>')
        else:
            itens.append(f'<div class="msg {m["papel"]}">{esc(m["texto"])}<small>{hora}</small></div>')
    if not itens:
        chips = "".join(f'<button type="button" class="chip">{esc(t)}</button>' for t in (sugestoes or [])[:4])
        return (f'<div class="exp-vazio">{simbolo(48, False)}<b>Você é o paciente.</b>'
                f'<span>Escreva como um paciente escreveria. O assistente responde com o que sabe da sua clínica.</span>'
                f'<div class="chips">{chips}</div></div>')
    return "".join(itens)


EXP_CSS = """<style>
.exp-grid{display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:28px;align-items:start}
.fone{background:#fff;border:1px solid var(--linha);border-radius:22px;overflow:hidden;display:flex;flex-direction:column;height:calc(100vh - 80px);min-height:520px;max-height:860px;box-shadow:var(--sombra)}
.fone-cab{display:flex;align-items:center;gap:12px;padding:12px 14px 12px 16px;background:var(--tinta);color:#fff}
.fone-cab .av{width:40px;height:40px;background:rgba(255,255,255,.08)}
.fone-cab .nm{flex:1;min-width:0}
.fone-cab .nm b{display:block;font-size:15.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.fone-cab .nm small{display:block;font-size:12.5px;color:#AEB9CC}
.fone-cab .uso{font-family:'JetBrains Mono',monospace;font-size:11.5px;color:#AEB9CC;white-space:nowrap}
.fone-cab .mais>summary{color:#C9D2E0}
.fone-cab .mais>summary:hover,.fone-cab .mais[open]>summary{background:rgba(255,255,255,.1);color:#fff}
.fone-cab .mais>div{color:var(--tinta)}
.fone .faixas{padding:10px 12px 0;background:var(--papel)}
.fone .faixa{margin-bottom:8px;font-size:13px;padding:7px 8px 7px 12px}
.fone-msgs{flex:1;overflow-y:auto;background:var(--papel);padding:16px 14px}
.exp-nota{align-self:center;max-width:88%;font-size:12.5px;color:var(--txt2);background:#fff;border:1px dashed var(--lilas);border-radius:12px;padding:7px 12px;text-align:center}
.exp-vazio{margin:auto;max-width:380px;display:flex;flex-direction:column;align-items:center;text-align:center;gap:8px;padding:30px 6px;color:var(--txt2);font-size:14px}
.exp-vazio b{font-family:'Bricolage Grotesque',sans-serif;font-size:20px;color:var(--tinta);margin-top:6px}
.chips{display:flex;flex-wrap:wrap;gap:8px;justify-content:center;margin-top:8px}
@media(min-width:1100px){.exp-vazio .chips{display:none}}
.chip{font:inherit;font-size:13.5px;padding:7px 12px;border-radius:999px;border:1px solid var(--linha);background:#fff;color:var(--tinta);cursor:pointer;text-align:left}
.chip:hover{border-color:var(--tinta)}
.fone-pe{border-top:1px solid var(--linha2);background:#fff;padding:10px 12px}
.exp-form{display:flex;gap:8px;align-items:flex-end}
.exp-form textarea{min-height:44px;height:44px;max-height:140px;flex:1;border-radius:22px;padding:11px 16px;resize:none;line-height:1.4}
.enviar{width:44px;height:44px;border-radius:50%;flex:none;border:0;background:var(--tinta);color:#fff;display:flex;align-items:center;justify-content:center;cursor:pointer}
.enviar:hover{background:var(--tinta2)}.enviar:disabled{opacity:.5}
.enviar svg{width:20px;height:20px}
.exp-fim{font-size:14px}.exp-fim b{display:block}
.exp-lado h1{font-size:30px;font-weight:800}
.exp-lado p{color:var(--txt2);font-size:14.5px;margin-top:8px}
.exp-lado .rot{display:block;margin:24px 0 10px}
.exp-lado .chips{justify-content:flex-start}
.exp-lado .ref{display:flex;flex-direction:column;gap:8px}
.exp-lado .ref a{display:flex;justify-content:space-between;gap:10px;background:#fff;border:1px solid var(--linha);border-radius:12px;padding:11px 14px;text-decoration:none;font-size:14.5px}
.exp-lado .ref a:hover{border-color:var(--tinta)}
.exp-lado .ref a span{color:var(--txt2);font-size:13px}
@media(max-width:1099px){.exp-grid{grid-template-columns:1fr}.exp-lado{display:none}}
@media(max-width:899px){
  body.cheia .topo{display:none}
  body.cheia main{padding:0}
  body.cheia main>.aviso{margin:0;border-radius:0}
  .fone{border:0;border-radius:0;box-shadow:none;height:calc(100dvh - var(--baixo) - env(safe-area-inset-bottom));min-height:0;max-height:none}
  .fone-cab{padding:10px 8px 10px 12px;padding-top:calc(10px + env(safe-area-inset-top))}
}
</style>"""

EXP_JS = """<script>
(function(){var b=document.getElementById('exp-box');if(b)b.scrollTop=b.scrollHeight;
var f=document.getElementById('exp-form');
document.querySelectorAll('.chip').forEach(function(c){c.addEventListener('click',function(){
  if(!f)return;var t=f.querySelector('textarea');t.value=c.textContent;f.requestSubmit();});});
if(!f)return;var t=f.querySelector('textarea');
function ajusta(){t.style.height='44px';t.style.height=Math.min(t.scrollHeight+2,140)+'px';}
t.addEventListener('input',ajusta);
if(window.matchMedia('(min-width:900px)').matches)t.focus();
t.addEventListener('keydown',function(e){if(e.key==='Enter'&&!e.shiftKey&&window.matchMedia('(min-width:900px)').matches){e.preventDefault();if(t.value.trim())f.requestSubmit();}});
f.addEventListener('submit',function(){var bt=f.querySelector('button');bt.disabled=true;
var d=document.createElement('div');d.className='msg paciente';d.textContent=t.value;var c=b.querySelector('.chat');
var v=c.querySelector('.exp-vazio');if(v)v.remove();c.appendChild(d);
var e=document.createElement('div');e.className='exp-nota';e.textContent='digitando...';c.appendChild(e);
b.scrollTop=b.scrollHeight;});})();
</script>"""

_SETA = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" '
         'stroke-linejoin="round" aria-hidden="true"><path d="M5 12h14M13 6l6 6-6 6"/></svg>')


@router.get("/painel/experimentar", response_class=HTMLResponse)
def painel_experimentar(msg: str = "", s: dict = Depends(exigir_login)):
    c = obter_clinica(s["clinica"]["slug"])
    slug = c["slug"]
    usado, limite = exp_uso_hoje(slug), exp_limite(c)
    info = exp_info(slug)
    conf = obter_conf(slug)
    profs = [x for x in conf["profissionais"] if x.get("nome")]
    sug = exp_sugestoes(conf)
    aviso = f'<div class="aviso">{esc(msg)}</div>' if msg else ""
    faixas = ""
    if not (conf.get("funcionamento") or profs):
        faixas += faixa("exp-sabe-pouco", 'Ele ainda sabe pouco da sua clínica. '
                        '<a href="/painel/configuracoes">Contar sobre a clínica</a>', erro=True)
    faixas += html_faixa_assinatura(c, False)
    if usado >= limite:
        if info.get("pedido_cota_em"):
            rodape = ('<div class="exp-fim"><b>Você usou as mensagens de hoje.</b>'
                      '<span style="color:var(--txt2)">Seu pedido de mais mensagens foi enviado. A gente libera em breve. '
                      'Amanhã o contador também volta a zero.</span></div>')
        else:
            rodape = (f'<div class="exp-fim linha"><div style="flex:1;min-width:200px"><b>Você usou as {limite} mensagens de hoje.</b>'
                      '<span style="color:var(--txt2)">Amanhã o contador volta a zero.</span></div>'
                      '<form method="post" action="/painel/experimentar/mais"><button class="btn lil peq">Pedir mais mensagens</button></form></div>')
    else:
        rodape = (f'<form method="post" action="/painel/experimentar" class="exp-form" id="exp-form">'
                  f'<textarea name="texto" maxlength="{EXP_MAX_CHARS}" placeholder="Escreva como paciente..." rows="1" required '
                  f'aria-label="Mensagem do paciente"></textarea>'
                  f'<button class="enviar" aria-label="Enviar">{_SETA}</button></form>')
    nome_ass = conf.get("nome_assistente") or f"Assistente da {c['nome']}"
    menu = (f'<details class="mais"><summary aria-label="Mais opções">{icone("mais")}</summary><div>'
            f'<form method="post" action="/painel/experimentar/limpar"><button>Começar outra conversa</button></form>'
            f'<a href="/painel/configuracoes">Mudar o que ele sabe</a><a href="/painel/profissionais">Profissionais e horários</a></div></details>')
    chips = "".join(f'<button type="button" class="chip">{esc(t)}</button>' for t in sug)
    com_ag = sum(1 for x in profs if AG.tem_agenda(x))
    corpo = f"""{aviso}<div class="exp-grid">
    <section class="fone">
      <div class="fone-cab"><span class="av">{simbolo(26)}</span>
        <div class="nm"><b>{esc(nome_ass)}</b><small>Simulação · nada sai pelo WhatsApp</small></div>
        <span class="uso" title="Mensagens usadas hoje">{min(usado, limite)}/{limite}</span>{menu}</div>
      {f'<div class="faixas">{faixas}</div>' if faixas else ''}
      <div class="fone-msgs" id="exp-box"><div class="chat">{html_exp_msgs(slug, sug)}</div></div>
      <div class="fone-pe">{rodape}</div>
    </section>
    <aside class="exp-lado">
      <span class="rot">Simular</span>
      <h1>Seja o paciente</h1>
      <p>Converse com o assistente como se fosse um paciente. Ele responde com o que está em Sua clínica e Profissionais:
      mudou lá, ele já responde diferente aqui. Nenhum paciente vê esta conversa.</p>
      <span class="rot">Comece por aqui</span><div class="chips">{chips}</div>
      <span class="rot">Ele responde com</span>
      <div class="ref"><a href="/painel/configuracoes">Sua clínica <span>endereço, convênios, regras</span></a>
        <a href="/painel/profissionais">Profissionais <span class="mono">{len(profs)} · {com_ag} com horários</span></a></div>
    </aside></div>"""
    return html_resp(pagina("Simular", corpo + EXP_JS, s, "experimentar", EXP_CSS, classe="cheia"))


@router.post("/painel/experimentar")
async def painel_experimentar_post(texto: str = Form(""), s: dict = Depends(exigir_login)):
    c = obter_clinica(s["clinica"]["slug"])
    slug = c["slug"]
    texto = texto.strip()[:EXP_MAX_CHARS]
    if not texto:
        return RedirectResponse("/painel/experimentar", status_code=303)
    async with _lock(f"exp:{slug}"):
        if exp_uso_hoje(slug) >= exp_limite(c):
            return RedirectResponse("/painel/experimentar", status_code=303)
        dk = K(slug, "exp", "uso", agora().strftime("%Y-%m-%d"))
        R.incr(dk)
        R.expire(dk, 3 * 86400)
        ik = K(slug, "exp", "info")
        agora_iso = agora().isoformat(timespec="seconds")
        R.hincrby(ik, "total", 1)
        R.hsetnx(ik, "primeira_em", agora_iso)
        R.hset(ik, "ultima_em", agora_iso)
        exp_registrar(slug, "paciente", texto)
        try:
            await exp_responder(c)
        except Exception as e:
            print(f"EXPERIMENTAR {slug} erro: {e}")
            return RedirectResponse("/painel/experimentar?msg=" + quote("O assistente não conseguiu responder agora. Tente de novo."),
                                    status_code=303)
    return RedirectResponse("/painel/experimentar", status_code=303)


@router.post("/painel/experimentar/limpar")
def painel_experimentar_limpar(s: dict = Depends(exigir_login)):
    R.delete(K(s["clinica"]["slug"], "exp", "hist"))
    return RedirectResponse("/painel/experimentar", status_code=303)


@router.post("/painel/experimentar/mais")
async def painel_experimentar_mais(s: dict = Depends(exigir_login)):
    c = obter_clinica(s["clinica"]["slug"])
    slug = c["slug"]
    ik = K(slug, "exp", "info")
    if not R.hget(ik, "pedido_cota_em"):
        R.hset(ik, "pedido_cota_em", agora().isoformat(timespec="seconds"))
        donos = ", ".join(f"{u.get('nome') or ''} <{u['email']}>" for u in usuarios_da_clinica(slug))
        await avisar_equipe_aga(
            f"{c['nome']} usou as {exp_limite(c)} mensagens de hoje experimentando o assistente e pediu mais.\n"
            f"Situação: {rotulo_situacao(c)}\nContato: {donos} / {fmt_tel(c.get('recepcao_tel', ''))}\n"
            f"Total de mensagens até agora: {R.hget(ik, 'total') or 0}\n"
            f"Liberar mais e ver a conversa: {BASE_URL}/admin/clinicas/{slug}",
            "Lead engajado: pediu mais mensagens")
    return RedirectResponse("/painel/experimentar", status_code=303)


def html_exp_resumo(c: dict) -> str:
    """Resumo curto para a lista de leads do admin."""
    info = exp_info(c["slug"])
    if not info.get("total"):
        return '<span style="color:var(--txt2)">não experimentou</span>'
    txt = f"{info['total']} msgs, hoje {exp_uso_hoje(c['slug'])}/{exp_limite(c)}"
    if info.get("pedido_cota_em"):
        txt += ' <span class="tag ag">pediu mais</span>'
    return txt


def html_exp_admin(c: dict) -> str:
    slug = c["slug"]
    info = exp_info(slug)
    pedido = (f'<p style="font-size:14px"><span class="tag ag">Pediu mais mensagens</span> {ha_quanto(info["pedido_cota_em"])}</p>'
              if info.get("pedido_cota_em") else "")
    if info.get("total"):
        resumo = (f'<p style="font-size:14px">Mensagens enviadas: <b>{info["total"]}</b> no total, '
                  f'<b>{exp_uso_hoje(slug)}</b> hoje. Primeira {ha_quanto(info.get("primeira_em", ""))}, '
                  f'última {ha_quanto(info.get("ultima_em", ""))}.</p>')
    else:
        resumo = '<p style="font-size:14px;color:var(--txt2)">Ainda não experimentou o assistente.</p>'
    conversa = "".join(
        f'<div class="msg {m["papel"] if m["papel"] != "nota" else "paciente"}" '
        f'style="{"font-size:12px;color:var(--txt2)" if m["papel"] == "nota" else ""}">{esc(m["texto"])}</div>'
        for m in exp_hist(slug)[-40:])
    ver = (f'<details style="margin-top:8px"><summary>Ver a conversa atual</summary>'
           f'<div class="chat" style="margin-top:10px">{conversa}</div></details>') if conversa else ""
    return f"""<div class="card"><h2>Experimentar o assistente</h2>{pedido}{resumo}
      <form method="post" action="/admin/clinicas/{slug}/experimentar" style="display:flex;gap:8px;align-items:flex-end;flex-wrap:wrap">
        <div><label>Mensagens por dia</label><input type="text" name="limite" value="{exp_limite(c)}" style="width:120px"></div>
        <label style="margin-bottom:10px"><input type="checkbox" name="zerar_hoje" value="1"> zerar o contador de hoje</label>
        <button class="btn sec peq" style="margin-bottom:6px">Salvar e liberar</button></form>
      <p style="font-size:13px;color:var(--txt2);margin-top:6px">Salvar também tira o aviso de "pediu mais". Padrão: {EXP_LIMITE_PADRAO} por dia.</p>
      {ver}</div>"""


@router.post("/admin/clinicas/{slug}/experimentar")
async def admin_experimentar(slug: str, request: Request, admin: str = Depends(exigir_admin)):
    c = obter_clinica(slug)
    if not c:
        raise HTTPException(status_code=404)
    f = await request.form()
    try:
        c["exp_limite"] = max(0, min(5000, int(str(f.get("limite", "")).strip())))
    except ValueError:
        return RedirectResponse(f"/admin/clinicas/{slug}?msg=Limite+invalido.", status_code=303)
    salvar_clinica(c)
    if f.get("zerar_hoje"):
        R.delete(K(slug, "exp", "uso", agora().strftime("%Y-%m-%d")))
    R.hdel(K(slug, "exp", "info"), "pedido_cota_em")
    return RedirectResponse(f"/admin/clinicas/{slug}?msg=" + quote(f"Liberado: {c['exp_limite']} mensagens por dia."), status_code=303)
