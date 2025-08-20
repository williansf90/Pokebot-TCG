import os
import logging
import re
import time
import unicodedata
import asyncio
import random
from collections import deque
from typing import Deque, Dict, Optional, Tuple

import httpx
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

# =========================
# Logging
# =========================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger("pokebot")

# =========================
# Constantes
# =========================
PIKACHU_GIF_URL = "https://media.giphy.com/media/DRfu7BT8ZK1uo/giphy.gif"
POKEMON_API_URL = "https://api.pokemontcg.io/v2/cards"
TELEGRAM_BOT_TOKEN = "7572354652:AAEg_EgwLhq55dHNBmiuH4G-iCIxpjb346A"

# =========================
# Rate Limit por usuário
# =========================
MAX_CHAMADAS = 10          # por usuário
INTERVALO_SEGUNDOS = 60
historico_por_usuario: Dict[int, Deque[float]] = {}

def pode_fazer_requisicao(user_id: int) -> bool:
    agora = time.time()
    dq = historico_por_usuario.get(user_id)
    if dq is None:
        dq = deque()
        historico_por_usuario[user_id] = dq
    while dq and (agora - dq[0] > INTERVALO_SEGUNDOS):
        dq.popleft()
    if len(dq) < MAX_CHAMADAS:
        dq.append(agora)
        return True
    return False

# =========================
# Cache (simples)
# =========================
cache_cartas: Dict[Tuple[str, str, int], Tuple[dict, float]] = {}

def cache_get(chave):
    """Retorna a carta do cache, ou None se não existir."""
    item = cache_cartas.get(chave)
    if not item:
        return None
    carta, ts = item
    return carta

def cache_set(chave, carta):
    """Armazena a carta no cache."""
    cache_cartas[chave] = (carta, time.time())

# =========================
# Deduplicação + limite de concorrência
# =========================
sem = asyncio.Semaphore(5)  # até 5 chamadas externas simultâneas
chamadas_em_andamento: Dict[Tuple[str, str, int], asyncio.Future] = {}

async def http_get_dedup(chave: Tuple[str, str, int], params: dict, max_retries: int = 3) -> dict:
    # Se já existe uma chamada igual em andamento, aguarda o mesmo resultado/erro
    fut = chamadas_em_andamento.get(chave)
    if fut is not None:
        logger.info(f"🔄 Esperando requisição em andamento para {chave}")
        return await fut  # pode levantar exceção se o criador set_exception

    loop = asyncio.get_event_loop()
    fut = loop.create_future()
    chamadas_em_andamento[chave] = fut

    try:
        async with sem:
            async with httpx.AsyncClient(timeout=12, headers={"Accept": "application/json"}) as client:
                last_exc = None
                for attempt in range(max_retries):
                    try:
                        resp = await client.get(POKEMON_API_URL, params=params)
                        if resp.status_code == 429:
                            espera = (2 ** attempt) + random.uniform(0, 1)
                            logger.warning(f"429 recebido. Backoff {espera:.2f}s")
                            await asyncio.sleep(espera)
                            continue
                        resp.raise_for_status()
                        data = resp.json()
                        if not fut.done():
                            fut.set_result(data)
                        return data
                    except (httpx.TimeoutException, httpx.HTTPStatusError, httpx.RequestError) as e:
                        last_exc = e
                        espera = (2 ** attempt) + random.uniform(0, 1)
                        logger.warning(f"Erro {e}. Retry em {espera:.2f}s")
                        await asyncio.sleep(espera)

                # Esgotou as tentativas → propaga erro para todo mundo que aguarda
                if not fut.done():
                    fut.set_exception(last_exc or RuntimeError("Falha desconhecida"))
                raise last_exc or RuntimeError("Falha desconhecida")
    finally:
        # Remove do mapa de deduplicação (o future já foi completado com sucesso/erro)
        chamadas_em_andamento.pop(chave, None)

# =========================
# Funções Auxiliares de texto 
# =========================
def normalizar_texto(txt: str) -> str:
    return unicodedata.normalize('NFKD', txt).encode('ASCII', 'ignore').decode('utf-8')

def escape_markdown(text: str) -> str:
    return text.replace('*', '\\*').replace('_', '\\_').replace('[', '\\[').replace(']', '\\]')

# =========================
# Seleção da carta (extraída)
# =========================
def selecionar_carta(cartas_encontradas: list[dict], total_usuario: int) -> Optional[dict]:
    def sid(c):
        s = c.get('set') or {}
        return s.get('id')

    def pt(c):
        s = c.get('set') or {}
        return s.get('printedTotal')

    # 1) Se todos os resultados têm o MESMO set.id, é decisivo
    set_ids = {x for x in (sid(c) for c in cartas_encontradas) if x}
    if len(set_ids) == 1:
        logger.info("🎯 Seleção por set.id único")
        return cartas_encontradas[0]

    # 2) Tente correspondência EXATA de printedTotal
    exatos = [c for c in cartas_encontradas if pt(c) == total_usuario]
    if len(exatos) == 1:
        logger.info("🎯 Seleção por printedTotal EXATO")
        return exatos[0]
    if len(exatos) > 1:
        ex_ids = {x for x in (sid(c) for c in exatos) if x}
        if len(ex_ids) == 1:
            logger.info("🎯 EXATO + set.id único")
            return exatos[0]

    # 3) Tolerância ±1 no printedTotal
    tol = [c for c in cartas_encontradas if isinstance(pt(c), int) and abs(pt(c) - total_usuario) <= 1]
    if len(tol) == 1:
        logger.info("🎯 Tolerância (±1)")
        return tol[0]
    if len(tol) > 1:
        tol_ids = {x for x in (sid(c) for c in tol) if x}
        if len(tol_ids) == 1:
            logger.info("🎯 Tolerância (±1) + set.id único")
            return tol[0]

    return None

# =========================
# Comandos do Bot
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Olá! Eu sou um bot de busca de cartas de Pokémon TCG.\n\n"
        "Use /carta <Nome> (<Nº>/<Total>) para pesquisar.\n"
        "Ex.: /carta Omanyte (60/75)"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "*Comandos Disponíveis:*\n\n"
        "*/start* - Mensagem de boas-vindas\n"
        "*/help* - Lista de comandos\n"
        "*/carta <Nome> (<Nº>/<Total>)* - Busca carta específica\n"
        "_Exemplo: /carta Pikachu (58/102)_",
        parse_mode='Markdown'
    )

def extrair_nome_e_numeracao(args: list[str]) -> Tuple[Optional[str], Optional[str], Optional[int]]:
    if not args:
        return None, None, None
    joined = " ".join(args)
    m = re.search(r"(\(?\s*(\d+)\s*/\s*(\d+)\s*\)?)", joined)
    if not m:
        return None, None, None
    numero = str(int(m.group(2)))
    total = int(m.group(3))
    nome = joined[:m.start()].strip()
    nome = re.sub(r"\s+", " ", nome)
    return nome, numero, total

async def procurar_carta_especifica(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id if update.effective_user else update.effective_chat.id
    chat_id = update.effective_chat.id
    if not pode_fazer_requisicao(user_id):
        await update.message.reply_text("🚫 Muitas consultas seguidas! Aguarde alguns segundos e tente novamente.")
        return

    loading_msg = await context.bot.send_animation(chat_id=chat_id, animation=PIKACHU_GIF_URL)
    try:
        nome_carta, numero_carta, total_colecao_usuario = extrair_nome_e_numeracao(context.args)
        if not (nome_carta and numero_carta and total_colecao_usuario is not None):
            await update.message.reply_text("Formato inválido. Use: /carta <Nome> (<Nº>/<Total>)")
            return

        nome_normalizado = normalizar_texto(nome_carta)
        chave_cache = (nome_normalizado.lower(), numero_carta, total_colecao_usuario)

        # 1) Tenta pegar do cache
        carta = cache_get(chave_cache)
        if carta:
            logger.info(f"✅ Cache hit para {chave_cache}")
            await enviar_carta(update, context, carta)
            return

        # 2) Busca na API
        try:
            dados = await http_get_dedup(chave_cache, {"q": f'name:"{nome_normalizado}" number:"{numero_carta}"'})
        except Exception:
            await update.message.reply_text("Erro de conexão com a API Pokémon TCG.")
            return

        cartas_encontradas = dados.get('data', []) or []
        if not cartas_encontradas:
            await update.message.reply_text(f"Não encontrei '{nome_carta}' nº {numero_carta}.")
            return

        # 3) Seleciona a carta correta (função extraída)
        carta_correta = selecionar_carta(cartas_encontradas, total_colecao_usuario)
        if not carta_correta:
            await update.message.reply_text(
                f"Encontrei '{nome_carta}' nº {numero_carta}, mas não na coleção com {total_colecao_usuario} cartas."
            )
            return

        # 4) Salva no cache e envia
        cache_set(chave_cache, carta_correta)
        await enviar_carta(update, context, carta_correta)

    finally:
        # garante que o GIF sai em qualquer caminho
        await apagar_loading(context, chat_id, loading_msg.message_id)

CAPTION_LIMIT = 1024

def _truncate(text: str, limit: int = CAPTION_LIMIT - 50) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."

async def enviar_carta(update: Update, context: ContextTypes.DEFAULT_TYPE, carta: dict) -> None:
    printed_total = (carta.get('set') or {}).get('printedTotal', '?')
    nome_formatado = f"{carta.get('name','')} ({carta.get('number','')}/{printed_total})"
    colecao = (carta.get('set') or {}).get('name', 'Desconhecida')
    raridade = carta.get('rarity', 'Não informada')
    tipo = ", ".join(carta.get('types', ['N/A']))
    preco = "$2.73 (via TCGplayer)"  # TODO: puxar preço real da API se quiser
    url_imagem = ((carta.get('images') or {}).get('large')) or ((carta.get('images') or {}).get('small'))

    # -------------------
    # Balão 1: informações gerais
    # -------------------
    caption = (
        f"🃏 {nome_formatado}\n"
        f"📦 Coleção: {colecao}\n"
        f"⭐️ Raridade: {raridade}\n"
        f"🔥 Tipo: {tipo}\n"
        f"💰 Preço Médio: {preco}\n"
    )
    caption = _truncate(caption, CAPTION_LIMIT - 20)

    try:
        if url_imagem:
            await context.bot.send_photo(
                chat_id=update.effective_chat.id,
                photo=url_imagem,
                caption=caption  # sem parse_mode para não interferir na diagramação
            )
        else:
            await update.message.reply_text(caption)
    except Exception as e:
        logger.warning(f"Falha em send_photo, fallback: {e}")
        await update.message.reply_text(caption)

    # -------------------
    # Balão 2: habilidades (se tiver) + ataques
    # -------------------
    partes = []

    # Habilidades
    habilidades = carta.get("abilities", [])
    if habilidades:
        partes.append("🧠 Habilidades:")
        for hab in habilidades:
            nome_hab = hab.get("name", "???")
            texto_hab = hab.get("text", "")
            partes.append(f"- {nome_hab}")
            if texto_hab:
                partes.append(texto_hab)

    # Ataques
    ataques = carta.get("attacks", [])
    if ataques:
        if habilidades:
            partes.append("")  # linha em branco entre habilidades e ataques
        partes.append("🗡️ Ataques:")
        for atk in ataques:
            nome_atk = atk.get("name", "???")
            custo = ", ".join(atk.get("cost", [])) or "N/A"
            dano = atk.get("damage", "")
            texto = atk.get("text", "")

            linha_topo = f"- {nome_atk} | Custo: {custo}"
            if dano:
                linha_topo += f" | Dano: {dano}"
            partes.append(linha_topo)
            if texto:
                partes.append(texto)

    if partes:
        # envia o 2º balão apenas se houver habilidades/ataques
        msg = "\n".join(partes)
        await context.bot.send_message(chat_id=update.effective_chat.id, text=msg)



async def apagar_loading(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int) -> None:
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass

async def texto_desconhecido(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Desculpe, não entendi. Digite /help para ver os comandos.")

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Erro não tratado", exc_info=context.error)

def listar_cache() -> list[dict]:
    """
    Retorna todas as cartas atualmente no cache (apenas os dados da carta).
    """
    return [carta for (carta, ts) in cache_cartas.values()]

# Comando para inspecionar cache (apenas para debug)
async def comando_cache(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cartas = listar_cache()
    if not cartas:
        await update.message.reply_text("🗑️ Cache vazio.")
        return

    resposta = "\n".join(
        f"- {c.get('name','?')} ({c.get('number','?')}/{(c.get('set') or {}).get('printedTotal','?')})"
        for c in cartas[:20]  # mostra no máximo 20 pra não lotar o chat
    )
    await update.message.reply_text(f"Cartas no cache:\n{resposta}")

# =========================
# Inicialização do Bot
# =========================
def main():
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("cache", comando_cache))
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("carta", procurar_carta_especifica))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, texto_desconhecido))
    application.add_error_handler(on_error)
    logger.info("🤖 Bot Pokémon TCG iniciado...")
    application.run_polling()

if __name__ == '__main__':
    main()
