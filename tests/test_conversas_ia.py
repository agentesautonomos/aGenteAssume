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

# avaliador barato de proposito: cada conversa avaliada custa ~US$ 0,001 (o Opus custava ~US$ 0,05)
MODELO_JUIZ = os.environ.get("JUIZ_MODEL", "claude-haiku-5-5")
SAIDA = os.path.join(os.path.dirname(__file__), "saida", "conversas.md")
EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿]")

# (nome, mensagens do paciente, o que se espera, instante UTC do "agora" ou None)
CENARIOS = [
    ("fabio_na_quarta", ["Oi, quero marcar com o Dr. Fabio na quarta de manhã"],
     "O Dr. Fabio não atende quarta nem de manhã. O assistente deve dizer isso e oferecer dias/horários reais dele "
     "(seg, ter ou qui à tarde) ou a Dra. Ana na quarta. Não pode oferecer o Fabio na quarta.", None),
    ("amanha_a_tarde", ["Boa tarde! Vocês têm horário amanhã à tarde?"],
     "Amanhã é terça 24/11 e à tarde atendem a Dra. Ana (clínico geral, clareamento) e o Dr. Fabio (ortodontia). "
     "Pode perguntar para quê é a consulta, ou mostrar horários livres de verdade da tarde de 24/11 de cada um e perguntar só "
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
     "Não existe Dr. Pedro. Deve dizer que não tem esse profissional e perguntar o que o paciente precisa "
     "(ou citar quem atende). Não pode inventar horário dele nem encaminhar para a equipe.", None),
    ("profissional_sem_agenda", ["Quero marcar implante com o Dr. Carlos no sábado de manhã. Sou a Lia, particular."],
     "O Dr. Carlos não tem agenda no sistema: o assistente deve anotar o pedido para a recepção confirmar, "
     "sem confirmar horário como certo.", None),
    ("convenio_que_nao_atende", ["Vocês aceitam Amil? Queria consulta com o Dr. Fabio pelo Amil."],
     "A clínica só aceita Unimed Odonto e o Fabio só atende particular. Não pode dizer que aceita Amil; "
     "pode oferecer particular ou passar para a equipe.", None),
    ("remarcar", ["Preciso remarcar minha consulta de quinta", "Pode ser sexta de manhã", "Às 9h"],
     "O paciente tem consulta com a Dra. Ana quinta 26/11 às 9h. Deve achar essa consulta (consultas_do_paciente), "
     "oferecer horários livres de verdade da Ana na sexta 27/11 de manhã e, quando ele escolher 9h, remarcar com "
     "remarcar_consulta e confirmar sexta 27/11 às 9h com a Dra. Ana. Não pode pedir nome nem convênio de novo, nem encaminhar.", None),
    ("cancelar", ["Quero cancelar minha consulta com o Dr. Fabio"],
     "O paciente tem consulta com o Dr. Fabio quinta 26/11 às 14h. Deve cancelar com cancelar_consulta (só há uma, "
     "não precisa perguntar qual) e confirmar o cancelamento de quinta 26/11 às 14h. Pode oferecer remarcar, sem insistir. "
     "Não pode encaminhar para a equipe.", None),
    ("remarcar_em_cima_da_hora", ["Preciso remarcar minha consulta de amanhã"],
     "Agora é segunda 23/11 10h e a consulta é terça 24/11 às 9h, com a Dra. Ana: faltam menos de 24 horas. "
     "Não pode remarcar sozinho: deve dizer com gentileza que, tão perto da consulta, quem remarca é a equipe, e encaminhar.", None),
    ("natal", ["Tem horário dia 25 de dezembro com a Dra. Ana?"],
     "Agora é 20/12. 25/12 é Natal, a clínica não atende. Deve dizer isso e oferecer outro dia real.",
     datetime(2026, 12, 20, 13, 0, tzinfo=timezone.utc)),
    # regras de conversa: preferência de profissional, preferência de horário, uma pergunta por vez
    ("quero_marcar_passo_a_passo", ["Oi, queria marcar uma consulta", "Queria fazer um clareamento", "Pode ser amanhã no final do dia"],
     "Na 1ª resposta deve perguntar só para quê é a consulta (os profissionais fazem coisas diferentes). "
     "Depois do clareamento, deve dizer que quem faz é a Dra. Ana e perguntar só o dia e o período. Depois de "
     "'amanhã no final do dia' (terça 24/11, a partir das 16h), deve oferecer horários livres de verdade da Ana em 24/11 "
     "a partir das 16h (16h, 16h30, 17h, 17h30) e não pedir nome antes disso. Uma pergunta por mensagem.", None),
    ("dente_doendo", ["Oi, meu dente tá doendo um pouco, queria ver isso"],
     "Deve acolher em poucas palavras, sugerir a Dra. Ana (clínico geral) chamando-a de 'Dra. Ana', e perguntar só o dia e período "
     "(ou já oferecer horários reais dela). Não pode listar especialidades ou convênios nem usar parênteses.", None),
    ("cadastro_baguncado", ["Boa noite, queria colocar aparelho"],
     "O cadastro está mal escrito ('Dr. fabio Sobrenome', 'dra ana souza'). Aparelho é ortodontia: deve sugerir o Fabio "
     "chamando-o de 'Dr. Fabio' (nunca 'Sobrenome' nem minúscula) e perguntar só o dia e período. "
     "Não pode listar especialidades ou convênios.", None),
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


# consultas que o paciente ja tem antes da conversa: (profissional, dia, hora)
PREVIAS = {
    "remarcar": [("ana1", "2026-11-26", "09:00")],
    "cancelar": [("fab1", "2026-11-26", "14:00")],
    "remarcar_em_cima_da_hora": [("ana1", "2026-11-24", "09:00")],
}

# cenarios que mudam o cadastro da clinica antes da conversa
AJUSTES = {
    "cadastro_baguncado": {"ana1": "dra ana souza", "fab1": "Dr. fabio Sobrenome"},
}


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
        "Linhas entre colchetes como [agenda] ..., [encaminhou para a equipe: ...] e [anotou pedido ...] são anotações do "
        "teste sobre o que o sistema fez (o paciente não vê); [encaminhou...] quer dizer que o encaminhamento foi feito "
        "no formato certo. Responder com clareza a uma pergunta direta sobre convênios não é despejar cadastro. Cumprimentar não é obrigatório: só reprove bom dia/boa tarde/boa noite errado para a hora.\n\n"
        "REGRAS DE CONVERSA: no máximo uma pergunta por mensagem (mostrar as opções de cada profissional e perguntar só "
        "'qual prefere?' conta como uma pergunta só e cobre a preferência de profissional); para marcar, perguntar a preferência de profissional "
        "(se o paciente não disse) e a preferência de dia/período (se não disse) antes de pedir nome ou convênio; "
        "oferecer só horários dentro do período pedido (manhã/cedo antes das 12h, almoço 11h-14h, tarde 12h-18h, "
        "final do dia/fim da tarde/depois do trabalho a partir das 16h, noite a partir das 18h).\n\n"
        "SOA HUMANO: cada mensagem tem que soar como uma boa recepcionista de verdade no WhatsApp: curta, natural, calorosa. "
        "Reprove se despejar dados do cadastro (listas de especialidades ou convênios, parênteses), se escrever o nome do "
        "profissional como está num cadastro mal preenchido (minúscula, 'Sobrenome', nome completo formal) em vez de "
        "'Dr. Fabio'/'Dra. Ana', ou se soar mecânico.\n\n"
        "Separe o que achar em três listas:\n"
        "- fato: disse algo falso (dia, data, dia da semana, 'amanhã'/'semana que vem' errado, horário, profissional, "
        "convênio, preço), ofereceu horário que a agenda não mostrou como livre, marcou errado, ou deixou de fazer o essencial do esperado;\n"
        "- regra: quebrou uma regra de conversa (mais de uma pergunta, pediu nome antes de oferecer horário, pediu licença "
        "ou confirmação para marcar, despejou cadastro, horário fora do período pedido, mais de 3 horários);\n"
        "- estilo: frase um pouco longa, tom pouco caloroso, escolha de palavras que poderia ser melhor (não reprova).\n"
        'Responda só com JSON: {"fato": ["..."], "regra": ["..."], "estilo": ["..."]}'
    )
    r = CL.claude().messages.create(model=MODELO_JUIZ, max_tokens=4000, messages=[{"role": "user", "content": pedido}])
    txt = "".join(getattr(b, "text", "") for b in r.content if getattr(b, "type", "") == "text")
    m = re.search(r"\{.*\}", txt, re.S)
    try:
        return json.loads(m.group(0)) if m else {"regra": [f"juiz sem JSON: {txt[:200]}"]}
    except json.JSONDecodeError:
        # aspas soltas dentro de uma nota de estilo quebram o JSON; se fato e regra estao vazios, a conversa passou
        if re.search(r'"fato"\s*:\s*\[\s*\]', txt) and re.search(r'"regra"\s*:\s*\[\s*\]', txt):
            return {"fato": [], "regra": [], "estilo": ["(nota de estilo ilegível)"]}
        return {"regra": [f"juiz com JSON inválido: {txt[:200]}"]}


@pytest.fixture(scope="module", autouse=True)
def _relatorio_novo():
    for f in (SAIDA, CUSTOS, RESUMO):
        if os.path.exists(f):
            os.remove(f)
    yield
    if os.path.exists(CUSTOS):
        linhas = [l.split("\t") for l in open(CUSTOS, encoding="utf-8").read().splitlines()]
        total = sum(float(l[4]) for l in linhas)
        print(f"\nCUSTO DO ASSISTENTE ({CL.MODELO}): US$ {total:.4f} em {len(linhas)} conversas "
              f"(média US$ {total / max(1, len(linhas)):.5f} por conversa; "
              f"{sum(int(l[2]) for l in linhas)} tokens de entrada, {sum(int(l[3]) for l in linhas)} de saída)")
    if os.path.exists(RESUMO):
        rs = [l.split("\t") for l in open(RESUMO, encoding="utf-8").read().splitlines()]
        print(f"RESUMO: {len(rs)} conversas; com erro de FATO: {sum(int(r[1]) > 0 for r in rs)}; "
              f"com erro de REGRA: {sum(int(r[2]) > 0 for r in rs)}; com nota de estilo: {sum(int(r[3]) > 0 for r in rs)}; "
              f"revisões automáticas: {sum(int(r[4]) for r in rs)}")


# US$ por milhao de tokens (entrada, saida) do modelo do assistente
PRECOS = {"claude-haiku-4-5-20251001": (1.0, 5.0), "claude-haiku-4-5": (1.0, 5.0), "claude-haiku-5-5": (0.10, 0.50),
          "claude-sonnet-5-5": (2.0, 10.0)}
CUSTOS = os.path.join(os.path.dirname(__file__), "saida", "custos.tsv")
RESUMO = os.path.join(os.path.dirname(__file__), "saida", "resumo.tsv")


def _registrar_custo(nome, ent, sai, custo):
    os.makedirs(os.path.dirname(CUSTOS), exist_ok=True)
    with open(CUSTOS, "a", encoding="utf-8") as f:
        f.write(f"{CL.MODELO}\t{nome}\t{ent}\t{sai}\t{custo:.5f}\n")


def _registrar(nome: str, texto: str):
    os.makedirs(os.path.dirname(SAIDA), exist_ok=True)
    with open(SAIDA, "a", encoding="utf-8") as f:
        f.write(f"\n## {nome}\n\n{texto}\n")


REPETIR = max(1, int(os.environ.get("REPETIR") or 1))
_RODADAS = [(f"{c[0]}#{i + 1}" if REPETIR > 1 else c[0], *c[1:]) for i in range(REPETIR) for c in CENARIOS]


@pytest.mark.parametrize("nome,mensagens,esperado,instante", _RODADAS, ids=[c[0] for c in _RODADAS])
def test_conversa(nome, mensagens, esperado, instante, conf, relogio, monkeypatch):
    if instante:
        relogio.ajustar(instante)
    base = nome.split("#")[0]
    if base in AJUSTES:
        for x in conf["profissionais"]:
            x["nome"] = AJUSTES[base].get(x["id"], x["nome"])
        CL.salvar_conf(SLUG, conf)
    for pid, dia, hora in PREVIAS.get(base, []):
        ok, _ = AG.reservar(SLUG, conf, AG.achar_prof(conf, pid), date.fromisoformat(dia), hora,
                            {"paciente": "Paulo Reis", "tel": CL.EXP_TEL, "convenio": "particular"}, sim=True)
        assert ok
    chamadas = []
    original = CL.executar_ferramenta

    def espiar(n, ent, ctx):
        out = original(n, ent, ctx)
        chamadas.append(f"[agenda] {n}({json.dumps(ent, ensure_ascii=False)}) -> {out}")
        return out
    monkeypatch.setattr(CL, "executar_ferramenta", espiar)

    system = CL.montar_prompt(CLINICA, conf) + CL.contexto_tempo(conf)
    msgs, linhas, reservas, revisoes_por_msg = [], [], [], []
    for txt in mensagens:
        msgs.append({"role": "user", "content": txt})
        linhas.append(f"Paciente: {txt}")
        ctx = {"slug": SLUG, "conf": conf, "tel": CL.EXP_TEL, "sim": True, "reservas": []}
        n0 = len(chamadas)
        try:
            bruto = asyncio.run(CL.conversar_ia(system, msgs, ctx, CL.K(SLUG, "tokens_teste")))
        except Exception as e:   # sem credito: para a rodada inteira em vez de falhar conversa por conversa
            if "credit balance" in str(e):
                pytest.exit("A conta da API está sem crédito: rodada interrompida.", returncode=2)
            raise
        limpo, motivo, pedido = CL.interpretar_resposta(bruto)
        linhas += chamadas[n0:]
        linhas.append(f"Assistente: {limpo}")
        if motivo:
            linhas.append(f"[encaminhou para a equipe: {motivo}]")
        if pedido:
            linhas.append(f"[anotou pedido para a recepção: {pedido}]")
        reservas += ctx["reservas"]
        for m in ctx.get("mudancas", []):
            linhas.append(f"[consulta {m['acao']} na agenda: era {m['antes']}"
                          + (f"; agora {AG.quando_txt(m['item'])} com {m['item']['prof_nome']}" if m["acao"] == "remarcada" else "")
                          + "]")
        revisoes_por_msg.append(ctx.get("revisoes", []))
        for r in ctx.get("revisoes", []):
            linhas.insert(len(linhas) - 1, f"[revisão automática pediu para reescrever: {'; '.join(r)}]")
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
        if l.startswith("Assistente:") and ("(" in l or "sobrenome" in l.lower()):
            problemas.append(f"copiou dados do cadastro (parênteses ou 'Sobrenome'): {l[12:160]}")
    fatos = (system + "\n\nAgenda de cada profissional:\n"
             + "\n".join(f"- {p['nome']}: {AG.resumo_semana(p) or 'sem agenda no sistema'}" for p in conf["profissionais"]))
    tk = AG.R.hgetall(CL.K(SLUG, "tokens_teste"))
    ent, sai = int(tk.get("entrada", 0)), int(tk.get("saida", 0))
    AG.R.delete(CL.K(SLUG, "tokens_teste"))
    preco = PRECOS.get(CL.MODELO, (0, 0))
    custo = (ent * preco[0] + sai * preco[1]) / 1e6
    pa = PRECOS.get(CL.MODELO_CONFERENTE, (0, 0))    # etapas + conferente (IA barata)
    aux_ent, aux_sai = int(tk.get("aux_entrada", 0)), int(tk.get("aux_saida", 0))
    custo += (aux_ent * pa[0] + aux_sai * pa[1]) / 1e6
    ent, sai = ent + aux_ent, sai + aux_sai
    _registrar_custo(nome, ent, sai, custo)
    veredito = _juiz(fatos, conversa, esperado)
    erros_fato = [f"FATO: {x}" for x in veredito.get("fato", [])] + [f"FATO: {x}" for x in problemas if x.startswith("consulta inválida")]
    erros_regra = [f"REGRA: {x}" for x in veredito.get("regra", [])] + [f"REGRA: {x}" for x in problemas if not x.startswith("consulta inválida")]
    estilo = [f"estilo: {x}" for x in veredito.get("estilo", [])]
    revisoes = sum(len(x) for x in revisoes_por_msg)
    os.makedirs(os.path.dirname(RESUMO), exist_ok=True)
    with open(RESUMO, "a", encoding="utf-8") as f:
        f.write(f"{nome}\t{len(erros_fato)}\t{len(erros_regra)}\t{len(estilo)}\t{revisoes}\n")

    _registrar(nome, f"**Modelo:** {CL.MODELO} · {ent} tokens de entrada, {sai} de saída · US$ {custo:.4f} · "
               f"revisões automáticas: {revisoes}\n\n"
               f"**Esperado:** {esperado}\n\n```\n{conversa}\n```\n\n"
               + ("**OK**" if not (erros_fato or erros_regra) else "**Problemas:**")
               + "".join(f"\n- {x}" for x in erros_fato + erros_regra + estilo))
    if estilo:
        print(f"\n[{nome}] " + " | ".join(estilo))
    assert not (erros_fato or erros_regra), "\n".join(erros_fato + erros_regra) + "\n\n" + conversa
