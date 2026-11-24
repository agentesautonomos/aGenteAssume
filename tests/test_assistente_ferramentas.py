"""O que o assistente recebe das ferramentas da agenda e do texto de contexto.

Esses testes simulam os pedidos tipicos de paciente no nivel das ferramentas
(o que o assistente pergunta para a agenda e o que a agenda responde a ele).
Se a resposta da ferramenta estiver errada ou ambigua, o assistente erra com o paciente.
"""
from datetime import date, datetime, timezone

import agenda as AG
import clinicas as CL
from conftest import SLUG, CLINICA


# ---------- "quarta de manha, quem tem?" (bug do Dr. Fabio, PR 8) ----------
def test_sem_profissional_numa_quarta_so_aparece_quem_atende(ferramenta):
    r = ferramenta("ver_horarios_livres", data_inicial="2026-11-25", dias=1)
    assert "Ana" in r and "Fabio" not in r


def test_fabio_na_quarta_diz_que_ele_nao_atende_quarta(ferramenta):
    r = ferramenta("ver_horarios_livres", profissional="Dr. Fabio Lima", data_inicial="2026-11-25", dias=1)
    assert "Sem horário" in r and "seg, ter e qui" in r


# ---------- "quinta as 17h tem?" ----------
def test_dia_cheio_mostra_tambem_os_horarios_do_fim_da_tarde(ferramenta):
    r = ferramenta("ver_horarios_livres", profissional="Dra. Ana Souza", data_inicial="2026-11-26", dias=1)
    assert "17:00" in r and "17:30" in r


def test_consulta_curta_mostra_o_dia_todo(ferramenta, conf):
    conf["profissionais"][0]["agenda"]["duracao"] = 20      # 24 horarios por dia
    r = ferramenta("ver_horarios_livres", profissional="Dra. Ana Souza", data_inicial="2026-11-26", dias=1)
    assert "17:20" in r


def test_semana_inteira_nao_esconde_horarios_sem_avisar(ferramenta, conf):
    conf["profissionais"][0]["agenda"]["duracao"] = 20
    r = ferramenta("ver_horarios_livres", profissional="Dra. Ana Souza", data_inicial="2026-11-24", dias=7)
    for linha in [l for l in r.splitlines() if l.startswith(("ter", "qua", "qui", "sex"))]:
        assert "17:20" in linha or "mais" in linha, linha


def test_periodo_tarde(ferramenta):
    r = ferramenta("ver_horarios_livres", profissional="Dra. Ana Souza", data_inicial="2026-11-24", dias=1, periodo="tarde")
    assert "14:00" in r and "09:00" not in r


def test_periodo_fim_do_dia(ferramenta):
    r = ferramenta("ver_horarios_livres", profissional="Dra. Ana Souza", data_inicial="2026-11-24", dias=1, periodo="fim_do_dia")
    assert "16:00" in r and "17:30" in r
    assert "15:30" not in r and "09:00" not in r


def test_periodo_almoco(ferramenta):
    r = ferramenta("ver_horarios_livres", profissional="Dra. Ana Souza", data_inicial="2026-11-24", dias=1, periodo="almoco")
    assert "11:00" in r and "11:30" in r and "14:00" not in r and "10:30" not in r


def test_prompt_pede_uma_pergunta_por_vez_e_preferencias(conf):
    from conftest import CLINICA
    txt = CL.montar_prompt(CLINICA, conf)
    assert "uma pergunta por vez" in txt
    assert "pergunte se tem preferência por algum" in txt
    assert "final do dia" in txt


def test_periodo_errado_nao_esconde_horario_do_dia(ferramenta):
    # a IA pediu "noite" para 17h: a ferramenta mostra o dia todo em vez de dizer que nao tem
    r = ferramenta("ver_horarios_livres", profissional="Dra. Ana Souza", data_inicial="2026-11-26", dias=1, periodo="noite")
    assert "Sem horário" not in r and "17:00" in r


def test_sem_horario_ja_traz_proximas_opcoes(ferramenta, relogio):
    relogio.ajustar(datetime(2026, 12, 20, 13, 0, tzinfo=timezone.utc))
    r = ferramenta("ver_horarios_livres", profissional="Dra. Ana Souza", data_inicial="2026-12-25", dias=1)
    assert "feriado" in r.lower() and "Próximos horários livres" in r


# ---------- motivo certo quando nao ha horario ----------
def test_alem_da_janela_nao_diz_que_o_profissional_nao_atende(ferramenta):
    r = ferramenta("ver_horarios_livres", profissional="Dra. Ana Souza", data_inicial="2027-01-20", dias=3)
    assert "Não ofereça" not in r
    assert "30 dias" in r


def test_dia_bloqueado_nao_diz_que_o_profissional_nao_atende(ferramenta, conf):
    AG.bloquear(SLUG, None, date(2026, 11, 24), "", "", "Dedetização")
    r = ferramenta("ver_horarios_livres", profissional="Dra. Ana Souza", data_inicial="2026-11-24", dias=1)
    assert "Sem horário" in r
    assert "bloque" in r.lower() or "ocupad" in r.lower()


def test_agenda_cheia_no_dia(ferramenta, conf):
    for h in AG.livres_no_dia(SLUG, conf, conf["profissionais"][1], date(2026, 11, 24)):
        AG.reservar(SLUG, conf, conf["profissionais"][1], date(2026, 11, 24), h, {"paciente": "x"})
    r = ferramenta("ver_horarios_livres", profissional="Dr. Fabio Lima", data_inicial="2026-11-24", dias=1)
    assert "Sem horário" in r and ("ocupad" in r.lower() or "chei" in r.lower())


def test_data_passada_vira_hoje(ferramenta):
    r = ferramenta("ver_horarios_livres", profissional="Dra. Ana Souza", data_inicial="2026-11-01", dias=1)
    assert "23/11" in r and "14:00" in r


# ---------- marcar ----------
def test_marcar_e_listar_do_paciente(ferramenta):
    r = ferramenta("marcar_consulta", profissional="Dra. Ana Souza", data="2026-11-24", hora="09:00",
                   nome_paciente="Joana", convenio="particular")
    assert r.startswith("Consulta marcada") and "terça-feira, 24/11 às 09:00" in r
    assert "24/11" in ferramenta("consultas_do_paciente")
    assert "Nenhuma" in ferramenta("consultas_do_paciente", tel="5511888880000")


def test_marcar_horario_ocupado_sugere_outros(ferramenta):
    ferramenta("marcar_consulta", profissional="Dra. Ana Souza", data="2026-11-24", hora="09:00", nome_paciente="A")
    r = ferramenta("marcar_consulta", profissional="Dra. Ana Souza", data="2026-11-24", hora="09:00", nome_paciente="B")
    assert r.startswith("Não marcado") and "09:30" in r


def test_marcar_fabio_na_quarta_nao_marca(ferramenta):
    r = ferramenta("marcar_consulta", profissional="Dr. Fabio Lima", data="2026-11-25", hora="14:00", nome_paciente="A")
    assert r.startswith("Não marcado")


def test_marcar_sem_nome_pede_o_nome(ferramenta):
    r = ferramenta("marcar_consulta", profissional="Dra. Ana Souza", data="2026-11-24", hora="09:00")
    assert "nome" in r.lower() and not ferramenta.reservas


def test_hora_com_um_digito(ferramenta):
    r = ferramenta("marcar_consulta", profissional="Dra. Ana Souza", data="2026-11-24", hora="9:00", nome_paciente="A")
    assert r.startswith("Consulta marcada")


def test_hora_escrita_9h(ferramenta):
    r = ferramenta("marcar_consulta", profissional="Dra. Ana Souza", data="2026-11-24", hora="9h", nome_paciente="A")
    assert r.startswith("Consulta marcada")


def test_hora_escrita_14h30(ferramenta):
    r = ferramenta("marcar_consulta", profissional="Dra. Ana Souza", data="2026-11-24", hora="14h30", nome_paciente="A")
    assert r.startswith("Consulta marcada") and "14:30" in r


def test_data_no_formato_brasileiro(ferramenta):
    r = ferramenta("marcar_consulta", profissional="Dra. Ana Souza", data="24/11/2026", hora="09:00", nome_paciente="A")
    assert r.startswith("Consulta marcada")


def test_profissional_sem_agenda(ferramenta):
    r = ferramenta("marcar_consulta", profissional="Dr. Carlos Reis", data="2026-11-28", hora="09:00", nome_paciente="A")
    assert "não encontrado" in r and not ferramenta.reservas


def test_profissional_inexistente(ferramenta):
    r = ferramenta("ver_horarios_livres", profissional="Dr. Pedro")
    assert "não encontrado" in r and "Dra. Ana Souza" in r


def test_simulacao_marca_so_na_simulacao(ferramenta, conf):
    r = ferramenta("marcar_consulta", sim=True, tel="experimentar", profissional="Dra. Ana Souza",
                   data="2026-11-24", hora="09:00", nome_paciente="A")
    assert r.startswith("Consulta marcada")
    assert AG.listar(SLUG, date(2026, 11, 24), date(2026, 11, 24)) == []


def test_feriado_aparece_como_feriado(ferramenta, relogio):
    relogio.ajustar(datetime(2026, 12, 20, 13, 0, tzinfo=timezone.utc))
    r = ferramenta("ver_horarios_livres", profissional="Dra. Ana Souza", data_inicial="2026-12-25", dias=1)
    assert "Sem horário" in r and "feriado" in r.lower()


# ---------- texto de contexto (data de hoje, datas relativas) ----------
def ctx(conf):
    return CL.contexto_tempo(conf)


def test_contexto_tem_hoje_em_sp(conf, relogio):
    relogio.ajustar(datetime(2026, 11, 24, 1, 30, tzinfo=timezone.utc))   # 22:30 de segunda em SP
    assert "AGORA: segunda-feira, 23/11/2026 22:30" in ctx(conf)


def test_contexto_explica_amanha_e_semana_que_vem(conf):
    t = ctx(conf)
    assert "amanhã" in t and "24/11" in t
    assert "semana que vem" in t and "30/11" in t and "06/12" in t


def test_contexto_cobre_dia_do_mes_dentro_da_janela(conf):
    # "dia 10" = 10/12, que ainda esta dentro dos 30 dias de agenda
    assert "10/12" in ctx(conf)


def test_contexto_avisa_feriado_na_janela(conf, relogio):
    relogio.ajustar(datetime(2026, 12, 20, 13, 0, tzinfo=timezone.utc))
    assert "Natal" in ctx(conf)


def test_quem_atende_quarta_nao_tem_fabio(conf):
    linha = [p for p in ctx(conf).split(";") if "quarta-feira:" in p][0]
    assert "Fabio" not in linha and "Ana" in linha


# ---------- regras no texto do assistente ----------
def test_prompt_manda_conferir_convenio_do_profissional(conf):
    p = CL.montar_prompt(CLINICA, conf)
    assert "convênio" in p.lower() and "profissional atende esse convênio" in p


def test_prompt_profissional_sem_agenda_vira_pedido(conf):
    p = CL.montar_prompt(CLINICA, conf)
    assert "Para Dr. Carlos Reis ainda não há agenda" in p


def test_ferramenta_so_lista_quem_tem_agenda(conf):
    nomes = CL.ferramentas_agenda(conf)[1]["input_schema"]["properties"]["profissional"]["enum"]
    assert nomes == ["Dra. Ana Souza", "Dr. Fabio Lima"]


# ---------- a resposta tem que soar como recepcionista, nao como ficha de cadastro ----------
def test_nome_de_conversa_limpa_o_cadastro():
    assert CL.nome_conversa("Dr. Marco tonding") == "Dr. Marco"
    assert CL.nome_conversa("Dr. Fabio Sobrenome") == "Dr. Fabio"
    assert CL.nome_conversa("dra ana souza") == "Dra. Ana"
    assert CL.nome_conversa("Carla") == "Carla"


def test_prompt_traz_nome_de_conversa_e_pede_para_nao_despejar_cadastro(conf):
    txt = CL.montar_prompt(CLINICA, conf)
    assert "Como chamar na conversa: Dra. Ana" in txt
    assert "Nunca copie dados do cadastro" in txt
    assert "o que ele precisa" in txt


# ---------- datas escritas pelo sistema e revisao automatica da resposta ----------
def test_horarios_vem_com_o_dia_ja_escrito_e_sugestao_de_3(ferramenta):
    r = ferramenta("ver_horarios_livres", profissional="Dra. Ana Souza", data_inicial="2026-11-24", dias=1, periodo="tarde")
    assert "amanhã, terça 24/11 [2026-11-24]" in r
    sug = [l for l in r.split("\n") if l.startswith("SUGESTÃO PARA OFERECER")][0]
    assert "amanhã, terça 24/11 às 14h" in sug and "com Dra. Ana" in sug
    assert len(CL._RE_HORA.findall(sug)) == 3


def test_sugestao_sem_profissional_tem_3_no_total(ferramenta):
    r = ferramenta("ver_horarios_livres", data_inicial="2026-11-24", dias=1, periodo="tarde")
    sug = [l for l in r.split("\n") if l.startswith("SUGESTÃO PARA OFERECER")]
    assert len(sug) == 1 and len(CL._RE_HORA.findall(sug[0])) == 3
    assert "Dra. Ana" in sug[0] and "Dr. Fabio" in sug[0]


def test_natal_sugere_proximos_dias_com_data_certa(ferramenta, relogio):
    relogio.ajustar(datetime(2026, 12, 20, 13, 0, tzinfo=timezone.utc))
    r = ferramenta("ver_horarios_livres", profissional="Dra. Ana Souza", data_inicial="2026-12-25", dias=1)
    assert "sexta 25/12: feriado" in r
    assert "SUGESTÃO PARA OFERECER" in r and "segunda 28/12 às 8h" in r


def test_revisao_corrige_dia_da_semana_errado():
    hoje = date(2026, 11, 23)
    txt, probs = CL.revisar_resposta("Tenho quarta 26/11 às 9h com a Dra. Ana. Qual prefere?", hoje)
    assert "quinta 26/11" in txt and probs == []


def test_revisao_pega_amanha_e_semana_que_vem_errados():
    hoje = date(2026, 12, 20)   # domingo
    _, probs = CL.revisar_resposta("A Dra. Ana tem na semana que vem, segunda 28/12 às 9h. Qual prefere?", hoje)
    assert any("semana que vem" in p for p in probs)
    _, probs = CL.revisar_resposta("Tenho amanhã, quarta 23/12, às 9h. Qual prefere?", hoje)
    assert any("amanhã" in p for p in probs)
    _, probs = CL.revisar_resposta("Tenho amanhã, segunda 21/12, às 9h. Qual prefere?", hoje)
    assert probs == []


def test_revisao_pega_horarios_demais_perguntas_e_licenca():
    hoje = date(2026, 11, 23)
    _, probs = CL.revisar_resposta("Tenho 14h, 15h, 16h ou 17h. Qual prefere?", hoje)
    assert any("4 horários" in p for p in probs)
    _, probs = CL.revisar_resposta("Oi, tudo bem? Tem preferência por alguém?", hoje)
    assert any("mais de uma pergunta" in p for p in probs)
    _, probs = CL.revisar_resposta("Está livre. Posso marcar para você?", hoje)
    assert any("licença" in p for p in probs)
    _, probs = CL.revisar_resposta("Pronto, marquei quinta 26/11 às 17h com a Dra. Ana.\n[AGENDAMENTO: a | b | c (x) | d | e]", hoje)
    assert probs == []


def test_cedo_sugere_os_primeiros_e_fim_do_dia_os_ultimos(ferramenta):
    r = ferramenta("ver_horarios_livres", profissional="Dra. Ana Souza", data_inicial="2026-11-24", dias=1, periodo="cedo")
    sug = [l for l in r.split("\n") if l.startswith("SUGESTÃO")][0]
    assert "8h, 8h30 ou 9h" in sug
    r = ferramenta("ver_horarios_livres", profissional="Dra. Ana Souza", data_inicial="2026-11-24", dias=1, periodo="fim_do_dia")
    sug = [l for l in r.split("\n") if l.startswith("SUGESTÃO")][0]
    assert "16h30, 17h ou 17h30" in sug


def test_revisao_tira_dia_repetido():
    txt, _ = CL.revisar_resposta("Consigo sim! Na quarta, quarta 25/11, a Dra. Ana tem 11h ou 11h30. Qual prefere?", date(2026, 11, 23))
    assert "Na quarta 25/11" in txt


def test_texto_interno_da_revisao_e_reconhecido():
    assert CL._RE_META.search("Pela revisão, a mensagem anterior ofereceu seis horários, e o limite é três no total.")
    assert not CL._RE_META.search("Tenho quinta 26/11 às 9h para uma consulta de revisão com a Dra. Ana. Qual prefere?")


def test_hoje_explica_que_horario_anterior_ja_passou(ferramenta):
    r = ferramenta("ver_horarios_livres", profissional="Dra. Ana Souza", dias=1)
    assert "Agora são 10:00" in r and "já passaram" in r


def test_revisao_cumprimenta_pelo_relogio():
    txt, _ = CL.revisar_resposta("Boa noite! Para aparelho quem atende é o Dr. Fabio.", date(2026, 11, 23), hora=10)
    assert txt.startswith("Bom dia!")


def test_revisao_pega_dias_de_atendimento_errados(conf):
    hoje = date(2026, 11, 23)
    _, probs = CL.revisar_resposta("Poxa, o Dr. Fabio não atende às quartas; ele atende de segunda a quinta, à tarde. "
                                   "Qual prefere?", hoje, conf=conf, hora=10)
    assert any("quarta" in p and "Dr. Fabio" in p for p in probs)
    _, probs = CL.revisar_resposta("O Dr. Fabio não atende às quartas; ele atende segunda, terça e quinta à tarde. "
                                   "Qual prefere?", hoje, conf=conf, hora=10)
    assert probs == []
    _, probs = CL.revisar_resposta("A Dra. Ana atende de segunda a sexta. Qual dia fica melhor?", hoje, conf=conf, hora=10)
    assert probs == []


def test_reescrita_que_pede_desculpa_pela_confusao_e_descartada():
    assert CL._RE_META.search("Desculpe a confusão. O Dr. Fabio não atende de manhã.")


def test_revisao_pega_nesta_semana_sem_data():
    _, probs = CL.revisar_resposta("De manhã ele não tem horário nesta semana. Qual prefere?", date(2026, 11, 23), hora=10)
    assert any("nesta semana" in p for p in probs)


def test_revisao_troca_nome_do_cadastro_pelo_de_conversa(conf):
    t, _ = CL.revisar_resposta("Amanhã tenho com a Dra. Ana Souza às 14h.", conf=conf)
    assert "Dra. Ana às 14h" in t and "Souza" not in t
