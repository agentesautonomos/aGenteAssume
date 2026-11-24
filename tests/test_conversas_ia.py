"""Conversas de verdade com o assistente (usa a IA, custa alguns centavos por rodada).

Cada cenario e um paciente simulado mandando mensagens na aba Experimentar (reservas de simulacao,
agenda e Redis de mentira, relogio congelado em segunda 23/11/2026 10:00). Depois:
  1. confere sozinho cada consulta marcada contra as regras da agenda (dia, horario, feriado, passado);
  2. confere a voz da marca (sem "robô"/"bot", sem emoji);
  3. um juiz (outra chamada a IA) le a conversa com os fatos da agenda e aponta o que estiver errado.

So roda com ANTHROPIC_API_KEY definido:
    ANTHROPIC_API_KEY=... pytest -m ia -s
O relatorio de cada rodada fica em tests/saida/conversas.md.
"""
import asyncio
import json
import os
import re
from datetime import date, datetime, timezone

import pytest

import agenda as AG
import clinicas as CL
from conftest import CLINICA, SLUG

pytestmark = [pytest.mark.ia, pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"),
                                                 reason="sem ANTHROPIC_API_KEY: conversas com a IA não rodam")]

MODELO_JUIZ = os.environ.get("JUIZ_MODEL", "claude-opus-5-5")
SAIDA = os.path.join(os.path.dirname(__file__), "saida", "conversas.md")
EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿]")

# (nome, mensagens do paciente, o que se espera, instante UTC do "agora" ou None)
CENARIOS = [
    ("fabio_na_quarta", ["Oi, quero marcar com o Dr. Fabio na quarta de manhã"],
     "O Dr. Fabio não atende quarta nem de manhã. O assistente deve dizer isso e oferecer dias/horários reais dele "
     "(seg, ter ou qui à tarde) ou a Dra. Ana na quarta. Não pode oferecer o Fabio na quarta.", None),
    ("amanha_a_tarde", ["Boa tarde! Vocês têm horário amanhã à tarde?"],
     "Amanhã é terça 24/11 e à tarde atendem a Dra. Ana e o Dr. Fabio. Deve perguntar se o paciente tem preferência "
     "de profissional citando os dois, ou mostrar horários livres de verdade da tarde de 24/11 de cada um e perguntar só "
     "qual prefere. Uma pergunta só.", None),
    ("dia_25_marca", ["Oi, meu nome é Joana Prado. Queria dia 25 às 15h com a Dra. Ana, particular."],
     "Dia 25 é quarta 25/11. 15:00 está livre para a Ana. Deve marcar e confirmar quarta 25/11 às 15h com a Dra. Ana.", None),
    ("semana_que_vem_quinta", ["Quero marcar com o Dr. Fabio na quinta da semana que vem, meu nome é Rui, particular"],
     "Quinta da semana que vem é 03/12. Deve oferecer horários do Fabio em 03/12 (14h, 15h, 16h ou 17h).", None),
    ("hoje_9h_ja_passou", ["Consigo hoje às 9h com a Dra. Ana?"],
     "Agora são 10:00 de segunda 23/11: 9h já passou. Deve dizer que não dá e oferecer outro horário real.", None),
    ("quinta_17h", ["Tem quinta às 17h com a Dra. Ana? Sou o Marcos, particular."],
     "Quinta 26/11 às 17:00 está livre para a Ana. Deve oferecer ou marcar esse horário (não dizer que não tem).", None),
    ("profissional_inexistente", ["Queria marcar com o Dr. Pedro"],
     "Não existe Dr. Pedro. Deve dizer que não tem esse profissional e oferecer os que existem. Não pode inventar horário dele.", None),
    ("profissional_sem_agenda", ["Quero marcar implante com o Dr. Carlos no sábado de manhã. Sou a Lia, particular."],
     "O Dr. Carlos não tem agenda no sistema: o assistente deve anotar o pedido para a recepção confirmar, "
     "sem confirmar horário como certo.", None),
    ("convenio_que_nao_atende", ["Vocês aceitam Amil? Queria consulta com o Dr. Fabio pelo Amil."],
     "A clínica só aceita Unimed Odonto e o Fabio só atende particular. Não pode dizer que aceita Amil; "
     "pode oferecer particular ou passar para a equipe.", None),
    ("remarcar", ["Preciso remarcar minha consulta de terça"],
     "Remarcar é com a equipe: deve avisar que vai passar para a equipe e encaminhar. Não pode remarcar sozinho.", None),
    ("natal", ["Tem horário dia 25 de dezembro com a Dra. Ana?"],
     "Agora é 20/12. 25/12 é Natal, a clínica não atende. Deve dizer isso e oferecer outro dia real.",
     datetime(2026, 12, 20, 13, 0, tzinfo=timezone.utc)),
    # regras de conversa: preferência de profissional, preferência de horário, uma pergunta por vez
    ("quero_marcar_passo_a_passo", ["Oi, queria marcar uma consulta", "Tanto faz", "Pode ser amanhã no final do dia"],
     "Na 1ª resposta deve perguntar só se tem preferência de profissional (citando Dra. Ana e Dr. Fabio). "
     "Depois do 'tanto faz', deve perguntar só o dia e o período que prefere. Depois de 'amanhã no final do dia' "
     "(terça 24/11, a partir das 16h), deve oferecer horários livres de verdade de 24/11 a partir das 16h "
     "(Ana 16h, 16h30, 17h, 17h30; Fabio 16h, 17h) e não pedir nome antes disso. Uma pergunta por mensagem.", None),
    ("ana_sem_dia", ["Quero marcar com a Dra. Ana"],
     "Profissional já dito. Deve perguntar qual dia e período o paciente prefere (uma pergunta só). "
     "Não deve perguntar nome nem convênio ainda.", None),
    ("fabio_fim_da_tarde", ["Queria marcar com o Dr. Fabio na quinta, mas só consigo no fim da tarde, depois do trabalho"],
     "Quinta 26/11; fim da tarde = a partir das 16h. O Fabio atende quinta 14h-18h (consultas de 1h): "
     "deve oferecer 16h e/ou 17h de 26/11, não 14h ou 15h.", None),
    ("ana_cedinho", ["Tem horário com a Dra. Ana amanhã bem cedo?"],
     "Amanhã é terça 24/11; cedo = primeiros horários da manhã. Deve oferecer 8h, 8h30 (ou próximos da manhã) de 24/11.", None),
    ("hora_do_almoco", ["Consigo na hora do almoço na quarta com a Dra. Ana?"],
     "Quarta 25/11, hora do almoço = 11h às 14h. A Ana atende 8-12 e 14-18: deve oferecer 11h ou 11h30 de 25/11 "
     "(pode citar 14h como alternativa). Não pode inventar 12h, 12h30, 13h ou 13h30.", None),
    ("preco", ["Quanto custa um clareamento?"],
     "Não há preço nas informações da clínica. Não pode inventar preço; deve dizer que a equipe informa e encaminhar.", None),
]


def _consulta_valida(conf: dict, it: dict) -> str:
    """Conferencia independente de uma consulta marcada. '' se ok."""
    p = AG.achar_prof(conf, it["prof_id"])
    d = date.fromisoformat(it["data"])
    if not p or not AG.intervalos_do_dia(p, d.weekday()):
        return f"{it['prof_nome']} não atende {AG.DIAS_LONGOS[d.weekday()]}"
    if AG.minutos(it["inicio"]) not in AG.grade(p, d):
        return f"{it['inicio']} não é um horário da grade de {it['prof_nome']}"
    if AG.fecha_no_feriado(conf, d):
        return f"{d:%d/%m} é feriado"
    if datetime.combine(d, datetime.min.time()).replace(hour=0) < datetime.combine(AG.agora_local().date(), datetime.min.time()):
        return "data no passado"
    return ""


def _juiz(fatos: str, conversa: str, esperado: str) -> dict:
    pedido = (
        "Você confere o atendimento de um assistente de WhatsApp de clínica odontológica.\n\n"
        f"FATOS VERDADEIROS (agenda, data de hoje, regras):\n{fatos}\n\n"
        f"O QUE SE ESPERA NESTE CENÁRIO:\n{esperado}\n\n"
        f"CONVERSA (com as consultas feitas à agenda):\n{conversa}\n\n"
        "REGRAS DE CONVERSA: no máximo uma pergunta por mensagem (mostrar as opções de cada profissional e perguntar só "
        "'qual prefere?' conta como uma pergunta só e cobre a preferência de profissional); para marcar, perguntar a preferência de profissional "
        "(se o paciente não disse) e a preferência de dia/período (se não disse) antes de pedir nome ou convênio; "
        "oferecer só horários dentro do período pedido (manhã/cedo antes das 12h, almoço 11h-14h, tarde 12h-18h, "
        "final do dia/fim da tarde/depois do trabalho a partir das 16h, noite a partir das 18h).\n\n"
        "O assistente disse algo falso em relação aos fatos (dia, data, horário, profissional, convênio, preço), "
        "ofereceu horário que a agenda não mostrou como livre, quebrou uma das regras de conversa, ou deixou de fazer o esperado? "
        'Responda só com JSON: {"ok": true ou false, "problemas": ["..."]}'
    )
    r = CL.claude().messages.create(model=MODELO_JUIZ, max_tokens=4000, messages=[{"role": "user", "content": pedido}])
    txt = "".join(getattr(b, "text", "") for b in r.content if getattr(b, "type", "") == "text")
    m = re.search(r"\{.*\}", txt, re.S)
    try:
        return json.loads(m.group(0)) if m else {"ok": False, "problemas": [f"juiz sem JSON: {txt[:200]}"]}
    except json.JSONDecodeError:
        return {"ok": False, "problemas": [f"juiz com JSON inválido: {txt[:200]}"]}


@pytest.fixture(scope="module", autouse=True)
def _relatorio_novo():
    for f in (SAIDA, CUSTOS):
        if os.path.exists(f):
            os.remove(f)
    yield
    if os.path.exists(CUSTOS):
        linhas = [l.split("\t") for l in open(CUSTOS, encoding="utf-8").read().splitlines()]
        total = sum(float(l[4]) for l in linhas)
        print(f"\nCUSTO DO ASSISTENTE ({CL.MODELO}): US$ {total:.4f} em {len(linhas)} conversas "
              f"(média US$ {total / max(1, len(linhas)):.5f} por conversa; "
              f"{sum(int(l[2]) for l in linhas)} tokens de entrada, {sum(int(l[3]) for l in linhas)} de saída)")


# US$ por milhao de tokens (entrada, saida) do modelo do assistente
PRECOS = {"claude-haiku-4-5-20251001": (1.0, 5.0), "claude-haiku-4-5": (1.0, 5.0), "claude-haiku-5-5": (0.10, 0.50),
          "claude-sonnet-5-5": (2.0, 10.0)}
CUSTOS = os.path.join(os.path.dirname(__file__), "saida", "custos.tsv")


def _registrar_custo(nome, ent, sai, custo):
    os.makedirs(os.path.dirname(CUSTOS), exist_ok=True)
    with open(CUSTOS, "a", encoding="utf-8") as f:
        f.write(f"{CL.MODELO}\t{nome}\t{ent}\t{sai}\t{custo:.5f}\n")


def _registrar(nome: str, texto: str):
    os.makedirs(os.path.dirname(SAIDA), exist_ok=True)
    with open(SAIDA, "a", encoding="utf-8") as f:
        f.write(f"\n## {nome}\n\n{texto}\n")


@pytest.mark.parametrize("nome,mensagens,esperado,instante", CENARIOS, ids=[c[0] for c in CENARIOS])
def test_conversa(nome, mensagens, esperado, instante, conf, relogio, monkeypatch):
    if instante:
        relogio.ajustar(instante)
    chamadas = []
    original = CL.executar_ferramenta

    def espiar(n, ent, ctx):
        out = original(n, ent, ctx)
        chamadas.append(f"[agenda] {n}({json.dumps(ent, ensure_ascii=False)}) -> {out}")
        return out
    monkeypatch.setattr(CL, "executar_ferramenta", espiar)

    system = CL.montar_prompt(CLINICA, conf) + CL.contexto_tempo(conf)
    msgs, linhas, reservas = [], [], []
    for txt in mensagens:
        msgs.append({"role": "user", "content": txt})
        linhas.append(f"Paciente: {txt}")
        ctx = {"slug": SLUG, "conf": conf, "tel": CL.EXP_TEL, "sim": True, "reservas": []}
        n0 = len(chamadas)
        bruto = asyncio.run(CL.conversar_ia(system, msgs, ctx, CL.K(SLUG, "tokens_teste")))
        limpo, motivo, pedido = CL.interpretar_resposta(bruto)
        linhas += chamadas[n0:]
        linhas.append(f"Assistente: {limpo}")
        if motivo:
            linhas.append(f"[encaminhou para a equipe: {motivo}]")
        if pedido:
            linhas.append(f"[anotou pedido para a recepção: {pedido}]")
        reservas += ctx["reservas"]
        msgs.append({"role": "assistant", "content": bruto})
    conversa = "\n".join(linhas)

    problemas = [f"consulta inválida: {e}" for e in (_consulta_valida(conf, it) for it in reservas) if e]
    falas = " ".join(l for l in linhas if l.startswith("Assistente:")).lower()
    if re.search(r"\b(robô|robo|bot)\b", falas):
        problemas.append("falou robô/bot")
    if EMOJI.search(falas):
        problemas.append("usou emoji")
    for l in linhas:
        if l.startswith("Assistente:") and l.count("?") > 1:
            problemas.append(f"mais de uma pergunta na mesma mensagem: {l[12:120]}")
    fatos = (system + "\n\nAgenda de cada profissional:\n"
             + "\n".join(f"- {p['nome']}: {AG.resumo_semana(p) or 'sem agenda no sistema'}" for p in conf["profissionais"]))
    tk = AG.R.hgetall(CL.K(SLUG, "tokens_teste"))
    ent, sai = int(tk.get("entrada", 0)), int(tk.get("saida", 0))
    AG.R.delete(CL.K(SLUG, "tokens_teste"))
    preco = PRECOS.get(CL.MODELO, (0, 0))
    custo = (ent * preco[0] + sai * preco[1]) / 1e6
    _registrar_custo(nome, ent, sai, custo)
    veredito = _juiz(fatos, conversa, esperado)
    if not veredito.get("ok"):
        problemas += [f"juiz: {x}" for x in veredito.get("problemas", [])]

    _registrar(nome, f"**Modelo:** {CL.MODELO} · {ent} tokens de entrada, {sai} de saída · US$ {custo:.4f}\n\n"
               f"**Esperado:** {esperado}\n\n```\n{conversa}\n```\n\n"
               + ("**OK**" if not problemas else "**Problemas:**\n" + "\n".join(f"- {x}" for x in problemas)))
    assert not problemas, "\n".join(problemas) + "\n\n" + conversa
