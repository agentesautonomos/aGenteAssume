"""
Agenda propria da clinica (minima): horarios por profissional, horarios livres, reservas e bloqueios.

Tudo fica no Redis da propria clinica (prefixo cl:{slug}:), separado da agenda antiga do main.py
(que e da prospeccao e continua igual).

A agenda e montada a partir das Configuracoes de cada profissional:
    prof["agenda"] = {"dias": {"0": "08:00-12:00, 14:00-18:00", ..., "6": ""}, "duracao": 30}
(0 = segunda ... 6 = domingo; o texto aceita "8h-12h", "08:00-12:00", "8h as 12h").

Itens da agenda (consultas e bloqueios) ficam em:
    cl:{slug}:agenda      hash  id -> json        (agenda real)
    cl:{slug}:agenda_z    zset  id -> inicio (epoch)
    cl:{slug}:agenda_sim / agenda_sim_z           (reservas feitas na aba Experimentar)

As reservas de simulacao nunca ocupam a agenda real. Na simulacao, os horarios livres
respeitam a agenda real E as reservas de simulacao.

Preparado para integrar com agenda externa depois: quem usa este modulo so chama
grade / livres / reservar / mover / cancelar / bloquear / desbloquear / listar.
Para ligar uma agenda de fora (Google Agenda, sistema da clinica), basta trocar o
"backend" dessas funcoes (ex.: ler ocupados de la em `ocupados` e gravar em `reservar`),
sem mexer no painel nem no assistente.
"""
import os
import re
import json
import time
import secrets
from contextlib import contextmanager
from datetime import datetime, date, timedelta, timezone

import redis

R = redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379"), decode_responses=True)
BR = timezone(timedelta(hours=-3))

DIAS_CURTOS = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]
DIAS_LONGOS = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"]
DURACAO_PADRAO = 30
ANTECEDENCIA_H_PADRAO = 2     # nao oferece horario que comeca daqui a menos de 2 horas
JANELA_DIAS_PADRAO = 30       # nao marca alem de 30 dias


def K(slug: str, *partes) -> str:
    return "cl:" + slug + (":" + ":".join(str(p) for p in partes) if partes else "")


def agora_local() -> datetime:
    return datetime.now(BR).replace(tzinfo=None)


# ============================================================
# HORARIOS DE CADA PROFISSIONAL
# ============================================================
_RE_INT = re.compile(
    r"(\d{1,2})\s*(?:[:h]\s*(\d{2})?)?\s*(?:-|–|—|às|as|a|até|ate)\s*(\d{1,2})\s*(?:[:h]\s*(\d{2})?)?", re.I)


def ler_intervalos(txt: str) -> list:
    """'8h-12h, 14:00-18:30' -> [(480, 720), (840, 1110)] em minutos do dia."""
    out = []
    for m in _RE_INT.finditer(txt or ""):
        h1, m1, h2, m2 = int(m.group(1)), int(m.group(2) or 0), int(m.group(3)), int(m.group(4) or 0)
        a, b = h1 * 60 + m1, h2 * 60 + m2
        if 0 <= a < b <= 24 * 60 and m1 < 60 and m2 < 60:
            out.append((a, b))
    return sorted(out)


def hm(minutos: int) -> str:
    return f"{minutos // 60:02d}:{minutos % 60:02d}"


def minutos(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def duracao(prof: dict) -> int:
    try:
        d = int((prof.get("agenda") or {}).get("duracao") or DURACAO_PADRAO)
    except (TypeError, ValueError):
        d = DURACAO_PADRAO
    return max(5, min(480, d))


def intervalos_do_dia(prof: dict, dia_semana: int) -> list:
    return ler_intervalos(((prof.get("agenda") or {}).get("dias") or {}).get(str(dia_semana), ""))


def tem_agenda(prof: dict) -> bool:
    return bool(prof.get("nome")) and any(intervalos_do_dia(prof, d) for d in range(7))


def profs_com_agenda(conf: dict) -> list:
    return [p for p in conf.get("profissionais", []) if tem_agenda(p)]


def resumo_semana(prof: dict) -> str:
    """'seg e qua 08:00–12:00; sex 14:00–18:00 (consultas de 30 min)'"""
    grupos = []   # [(texto_intervalos, [dias])]
    for d in range(7):
        ints = intervalos_do_dia(prof, d)
        if not ints:
            continue
        t = ", ".join(f"{hm(a)}–{hm(b)}" for a, b in ints)
        for g in grupos:
            if g[0] == t:
                g[1].append(d)
                break
        else:
            grupos.append((t, [d]))
    if not grupos:
        return ""
    partes = []
    for t, ds in grupos:
        if len(ds) > 2 and ds[-1] - ds[0] == len(ds) - 1:
            nomes = f"{DIAS_CURTOS[ds[0]]} a {DIAS_CURTOS[ds[-1]]}"
        elif len(ds) > 1:
            nomes = ", ".join(DIAS_CURTOS[x] for x in ds[:-1]) + f" e {DIAS_CURTOS[ds[-1]]}"
        else:
            nomes = DIAS_CURTOS[ds[0]]
        partes.append(f"{nomes} {t}")
    return "; ".join(partes) + f" (consultas de {duracao(prof)} min)"


def grade(prof: dict, dia: date) -> list:
    """Horarios de inicio possiveis (em minutos) no dia, pela agenda do profissional."""
    dur = duracao(prof)
    out = []
    for a, b in intervalos_do_dia(prof, dia.weekday()):
        t = a
        while t + dur <= b:
            out.append(t)
            t += dur
    return out


def achar_prof(conf: dict, ref: str) -> dict | None:
    """Acha o profissional pelo id ou pelo nome (sem diferenciar maiusculas)."""
    ref = (ref or "").strip()
    profs = [p for p in conf.get("profissionais", []) if p.get("nome")]
    for p in profs:
        if p.get("id") == ref:
            return p
    low = ref.lower()
    for p in profs:
        if p["nome"].strip().lower() == low:
            return p
    parecidos = [p for p in profs if low and (low in p["nome"].lower() or p["nome"].lower() in low)]
    return parecidos[0] if len(parecidos) == 1 else None


# ============================================================
# ITENS (consultas e bloqueios)
# ============================================================
def _chaves(slug: str, sim: bool) -> tuple[str, str]:
    return (K(slug, "agenda_sim"), K(slug, "agenda_sim_z")) if sim else (K(slug, "agenda"), K(slug, "agenda_z"))


def _epoch(dia: str, hhmm: str) -> float:
    return datetime.strptime(f"{dia} {hhmm}", "%Y-%m-%d %H:%M").replace(tzinfo=BR).timestamp()


def _gravar(slug: str, it: dict):
    hk, zk = _chaves(slug, it.get("sim", False))
    R.hset(hk, it["id"], json.dumps(it, ensure_ascii=False))
    R.zadd(zk, {it["id"]: _epoch(it["data"], it["inicio"])})


def obter(slug: str, iid: str) -> dict | None:
    for sim in (False, True):
        v = R.hget(_chaves(slug, sim)[0], iid)
        if v:
            return json.loads(v)
    return None


def listar(slug: str, ini: date, fim: date, sim: bool = False, incluir_canceladas: bool = False) -> list:
    """Itens entre ini e fim (inclusive). Com sim=True, junta a agenda real e a de simulacao."""
    a = datetime.combine(ini, datetime.min.time()).replace(tzinfo=BR).timestamp()
    b = datetime.combine(fim + timedelta(days=1), datetime.min.time()).replace(tzinfo=BR).timestamp() - 1
    out = []
    for s in ((False, True) if sim else (False,)):
        hk, zk = _chaves(slug, s)
        ids = R.zrangebyscore(zk, a, b)
        if ids:
            out += [json.loads(v) for v in R.hmget(hk, ids) if v]
    if not incluir_canceladas:
        out = [x for x in out if x.get("status") != "cancelada"]
    out.sort(key=lambda x: (x["data"], x["inicio"], x.get("prof_nome", "")))
    return out


def ocupados(slug: str, dia: date, prof_id: str, sim: bool = False, ignorar: str = "") -> list:
    """[(ini, fim)] em minutos que ja estao tomados para o profissional nesse dia."""
    out = []
    for x in listar(slug, dia, dia, sim):
        if x["id"] == ignorar:
            continue
        if x.get("tipo") == "bloqueio" and x.get("prof_id") not in ("", prof_id):
            continue
        if x.get("tipo") != "bloqueio" and x.get("prof_id") != prof_id:
            continue
        out.append((minutos(x["inicio"]), minutos(x["fim"])))
    return out


def _conflita(a: int, b: int, tomados: list) -> bool:
    return any(a < y and x < b for x, y in tomados)


def regras(conf: dict) -> tuple[int, int]:
    try:
        ant = int(conf.get("ag_antecedencia_h") if conf.get("ag_antecedencia_h") not in (None, "") else ANTECEDENCIA_H_PADRAO)
    except (TypeError, ValueError):
        ant = ANTECEDENCIA_H_PADRAO
    try:
        jan = int(conf.get("ag_janela_dias") or JANELA_DIAS_PADRAO)
    except (TypeError, ValueError):
        jan = JANELA_DIAS_PADRAO
    return max(0, min(240, ant)), max(1, min(180, jan))


def livres_no_dia(slug: str, conf: dict, prof: dict, dia: date, sim: bool = False, ignorar: str = "",
                  respeitar_regras: bool = True) -> list:
    """Horarios livres (HH:MM) do profissional no dia."""
    agora = agora_local()
    ant, jan = regras(conf)
    if respeitar_regras and (dia > agora.date() + timedelta(days=jan)):
        return []
    limite = agora + timedelta(hours=ant if respeitar_regras else 0)
    dur = duracao(prof)
    tomados = ocupados(slug, dia, prof.get("id", ""), sim, ignorar)
    out = []
    for t in grade(prof, dia):
        if datetime.combine(dia, datetime.min.time()) + timedelta(minutes=t) < limite:
            continue
        if not _conflita(t, t + dur, tomados):
            out.append(hm(t))
    return out


def livres(slug: str, conf: dict, prof: dict, ini: date, dias: int, sim: bool = False) -> dict:
    """{date: [HH:MM, ...]} so com os dias que tem horario livre."""
    out = {}
    for i in range(max(1, min(31, dias))):
        d = ini + timedelta(days=i)
        hs = livres_no_dia(slug, conf, prof, d, sim)
        if hs:
            out[d] = hs
    return out


@contextmanager
def trava(slug: str):
    """Evita duas reservas no mesmo horario ao mesmo tempo (paciente e recepcao, ou dois pacientes)."""
    chave = K(slug, "agenda_trava")
    tok = secrets.token_hex(8)
    for _ in range(60):
        if R.set(chave, tok, nx=True, px=5000):
            break
        time.sleep(0.05)
    try:
        yield
    finally:
        if R.get(chave) == tok:
            R.delete(chave)


def _novo_id(sim: bool) -> str:
    return ("s" if sim else "a") + agora_local().strftime("%y%m%d") + secrets.token_hex(4)


def _validar(slug, conf, prof, dia: date, hora: str, sim: bool, encaixe: bool, ignorar: str = "") -> str:
    """'' se pode marcar; senao o motivo."""
    try:
        t = minutos(hora)
    except Exception:
        return "Horário inválido."
    if encaixe:
        if datetime.combine(dia, datetime.min.time()) + timedelta(minutes=t) < agora_local() - timedelta(hours=12):
            return "Esse horário já passou."
        if _conflita(t, t + duracao(prof), ocupados(slug, dia, prof.get("id", ""), sim, ignorar)):
            return "Já tem consulta ou bloqueio nesse horário."
        return ""
    if hora not in livres_no_dia(slug, conf, prof, dia, sim, ignorar):
        return "Esse horário não está livre."
    return ""


def reservar(slug: str, conf: dict, prof: dict, dia: date, hora: str, dados: dict,
             sim: bool = False, encaixe: bool = False, por: str = "", origem: str = "assistente") -> tuple[bool, dict | str]:
    """Marca a consulta se o horario ainda estiver livre. Devolve (True, item) ou (False, motivo)."""
    with trava(slug):
        erro = _validar(slug, conf, prof, dia, hora, sim, encaixe)
        if erro:
            return False, erro
        t = minutos(hora)
        it = {
            "id": _novo_id(sim), "tipo": "consulta", "status": "marcada", "sim": sim,
            "prof_id": prof.get("id", ""), "prof_nome": prof.get("nome", ""),
            "data": dia.isoformat(), "inicio": hm(t), "fim": hm(t + duracao(prof)),
            "paciente": (dados.get("paciente") or "").strip()[:120],
            "tel": re.sub(r"\D", "", dados.get("tel") or ""),
            "convenio": (dados.get("convenio") or "").strip()[:120],
            "obs": (dados.get("obs") or "").strip()[:500],
            "origem": origem, "encaixe": bool(encaixe),
            "criado_em": agora_local().isoformat(timespec="seconds"), "criado_por": por,
        }
        _gravar(slug, it)
        return True, it


def mover(slug: str, conf: dict, iid: str, prof: dict, dia: date, hora: str,
          encaixe: bool = False, por: str = "") -> tuple[bool, dict | str]:
    with trava(slug):
        it = obter(slug, iid)
        if not it or it.get("tipo") != "consulta" or it.get("status") == "cancelada":
            return False, "Consulta não encontrada."
        erro = _validar(slug, conf, prof, dia, hora, it.get("sim", False), encaixe, ignorar=iid)
        if erro:
            return False, erro
        antes = f"{quando_txt(it)} com {it.get('prof_nome', '')}"
        t = minutos(hora)
        it.update({"prof_id": prof.get("id", ""), "prof_nome": prof.get("nome", ""), "data": dia.isoformat(),
                   "inicio": hm(t), "fim": hm(t + duracao(prof)), "encaixe": bool(encaixe)})
        it.setdefault("mudancas", []).append(
            {"em": agora_local().isoformat(timespec="seconds"), "por": por, "acao": "movida", "antes": antes})
        _gravar(slug, it)
        return True, it


def cancelar(slug: str, iid: str, por: str = "") -> dict | None:
    with trava(slug):
        it = obter(slug, iid)
        if not it or it.get("tipo") != "consulta":
            return None
        it["status"] = "cancelada"
        it.setdefault("mudancas", []).append({"em": agora_local().isoformat(timespec="seconds"), "por": por, "acao": "cancelada"})
        _gravar(slug, it)
        return it


def bloquear(slug: str, prof: dict | None, dia: date, ini: str, fim: str, motivo: str, por: str = "") -> tuple[bool, dict | str]:
    """Bloqueia um periodo (ou o dia todo, ini='' fim=''). prof=None bloqueia para todos os profissionais."""
    try:
        a = minutos(ini) if ini else 0
        b = minutos(fim) if fim else 24 * 60
    except Exception:
        return False, "Horário inválido."
    if b <= a:
        return False, "O fim precisa ser depois do início."
    it = {
        "id": _novo_id(False), "tipo": "bloqueio", "status": "ativo", "sim": False,
        "prof_id": (prof or {}).get("id", ""), "prof_nome": (prof or {}).get("nome", "") or "Todos",
        "data": dia.isoformat(), "inicio": hm(a), "fim": hm(b) if b < 24 * 60 else "23:59",
        "motivo": (motivo or "").strip()[:200], "dia_todo": not ini and not fim,
        "criado_em": agora_local().isoformat(timespec="seconds"), "criado_por": por,
    }
    with trava(slug):
        _gravar(slug, it)
    return True, it


def desbloquear(slug: str, iid: str) -> bool:
    it = obter(slug, iid)
    if not it or it.get("tipo") != "bloqueio":
        return False
    hk, zk = _chaves(slug, False)
    R.hdel(hk, iid)
    R.zrem(zk, iid)
    return True


def consultas_do_tel(slug: str, tel: str, sim: bool = False) -> list:
    hoje = agora_local().date()
    tel = re.sub(r"\D", "", tel or "")
    itens = listar(slug, hoje, hoje + timedelta(days=365), sim)
    return [x for x in itens if x.get("tipo") == "consulta" and x.get("tel") == tel and x.get("sim", False) == sim]


def apagar_simulacao(slug: str):
    hk, zk = _chaves(slug, True)
    R.delete(hk, zk)


def quando_txt(it_ou_dia, hora: str = "") -> str:
    """'quarta-feira, 26/11 às 09:00'"""
    if isinstance(it_ou_dia, dict):
        d, hora = date.fromisoformat(it_ou_dia["data"]), it_ou_dia["inicio"]
    else:
        d = it_ou_dia
    return f"{DIAS_LONGOS[d.weekday()]}, {d.strftime('%d/%m')}" + (f" às {hora}" if hora else "")
