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
