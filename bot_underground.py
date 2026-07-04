import requests
import json
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ============ CONFIGURACIÓN ============
TELEGRAM_TOKEN = "8881964514:AAFWfgyt3BvVfMVIof-0VFWbFT6LvJOgzOc"
FOOTBALL_API_KEY = "30f51e06906260859fb22fdbab0311c4"
GEMINI_API_KEY = "AQ.Ab8RN6JpbCX2Vu9WkkLUEdxe3LoChQZxylBvoYuqPUby8JnT9A"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
FOOTBALL_URL = "https://v3.football.api-sports.io"

# Ligas TOP que EXCLUIMOS (underground only)
LIGAS_TOP_IDS = {
    39, 140, 135, 78, 61, 2, 3, 848, 94, 88, 144, 179, 292, 169, 119, 113, 203, 207, 271, 307
}

# ============ FUNCIONES ============

def get_gemini_response(prompt: str) -> str:
    headers = {"Content-Type": "application/json"}
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    url = f"{GEMINI_URL}?key={GEMINI_API_KEY}"
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=30)
        data = response.json()
        if "candidates" in data and len(data["candidates"]) > 0:
            return data["candidates"][0]["content"]["parts"][0]["text"]
        return "❌ Gemini no pudo generar respuesta."
    except Exception as e:
        return f"❌ Error: {str(e)}"

def api_request(endpoint: str, params: dict = None):
    headers = {"x-apisports-key": FOOTBALL_API_KEY}
    try:
        r = requests.get(f"{FOOTBALL_URL}/{endpoint}", headers=headers, params=params, timeout=15)
        return r.json().get("response", [])
    except:
        return []

def get_underground(date_str: str = None):
    if not date_str:
        date_str = datetime.now().strftime("%Y-%m-%d")
    matches = api_request("fixtures", {"date": date_str, "timezone": "America/Caracas"})
    return [m for m in matches if m.get("league", {}).get("id", 0) not in LIGAS_TOP_IDS]

def get_team_form(team_id: int):
    return api_request("fixtures", {"team": team_id, "last": 5})

def get_h2h(t1: int, t2: int):
    return api_request("fixtures/headtohead", {"h2h": f"{t1}-{t2}", "last": 5})

def format_data(match, home_form, away_form, h2h):
    home = match["teams"]["home"]["name"]
    away = match["teams"]["away"]["name"]
    league = match["league"]["name"]
    data = f"📅 {home} vs {away}\n🏆 {league}\n\n📊 FORMA {home}:\n"
    for i, m in enumerate(home_form[:5], 1):
        h, a = m["teams"]["home"]["name"], m["teams"]["away"]["name"]
        hg = m["goals"]["home"] if m["goals"]["home"] is not None else "-"
        ag = m["goals"]["away"] if m["goals"]["away"] is not None else "-"
        data += f"  {i}. {h} {hg}-{ag} {a}\n"
    data += f"\n📊 FORMA {away}:\n"
    for i, m in enumerate(away_form[:5], 1):
        h, a = m["teams"]["home"]["name"], m["teams"]["away"]["name"]
        hg = m["goals"]["home"] if m["goals"]["home"] is not None else "-"
        ag = m["goals"]["away"] if m["goals"]["away"] is not None else "-"
        data += f"  {i}. {h} {hg}-{ag} {a}\n"
    data += "\n🔄 H2H:\n"
    for i, m in enumerate(h2h[:5], 1):
        h, a = m["teams"]["home"]["name"], m["teams"]["away"]["name"]
        hg = m["goals"]["home"] if m["goals"]["home"] is not None else "-"
        ag = m["goals"]["away"] if m["goals"]["away"] is not None else "-"
        data += f"  {i}. {h} {hg}-{ag} {a}\n"
    return data

# ============ COMANDOS ============

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "🎯 *Bot Underground Activado!*\n\n"
        "🔍 Ligas menores | 🚩 Corners | ⚽ Over/Under Goles\n"
        "🤖 Gemini IA + API-Football\n\n"
        "📋 *Comandos:*\n"
        "• /underground - Partidos hoy\n"
        "• /corners - Análisis corners\n"
        "• /goles - Análisis goles\n"
        "• /predecir [A] vs [B] - Análisis completo\n"
        "• /vivo - En vivo ahora"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def underground(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Buscando partidos underground...", parse_mode="Markdown")
    matches = get_underground()
    if not matches:
        await update.message.reply_text("📭 No hay partidos underground hoy.")
        return
    msg = f"🎯 *Underground - {datetime.now().strftime('%d/%m')}*\n\n"
    by_league = {}
    for m in matches:
        ln = m["league"]["name"]
        if ln not in by_league:
            by_league[ln] = []
        by_league[ln].append(m)
    count = 0
    for league, games in by_league.items():
        if count >= 12:
            break
        msg += f"🏆 *{league}*\n"
        for g in games[:2]:
            home, away = g["teams"]["home"]["name"], g["teams"]["away"]["name"]
            time = g["fixture"]["date"][11:16]
            msg += f"  ⏰ {time} {home} vs {away}\n"
            count += 1
        msg += "\n"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def vivo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔴 Buscando en vivo...", parse_mode="Markdown")
    matches = api_request("fixtures", {"live": "all", "timezone": "America/Caracas"})
    live = [m for m in matches if m.get("league", {}).get("id", 0) not in LIGAS_TOP_IDS]
    if not live:
        await update.message.reply_text("📭 No hay underground en vivo.")
        return
    msg = "🔴 *Underground EN VIVO:*\n\n"
    for m in live[:8]:
        h, a = m["teams"]["home"]["name"], m["teams"]["away"]["name"]
        hg = m["goals"]["home"] if m["goals"]["home"] is not None else 0
        ag = m["goals"]["away"] if m["goals"]["away"] is not None else 0
        mn = m["fixture"]["status"]["elapsed"]
        lg = m["league"]["name"]
        msg += f"⚡ *{h}* {hg}-{ag} *{a}* ({mn}')\n🏆 {lg}\n\n"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def corners(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚩 Analizando corners...", parse_mode="Markdown")
    matches = get_underground()
    if not matches:
        await update.message.reply_text("📭 No hay partidos hoy.")
        return
    selected = matches[:2]
    msg = "🚩 *ANÁLISIS CORNERS*\n\n"
    for m in selected:
        h, a = m["teams"]["home"]["name"], m["teams"]["away"]["name"]
        hid, aid = m["teams"]["home"]["id"], m["teams"]["away"]["id"]
        lg = m["league"]["name"]
        hf, af, hh = get_team_form(hid), get_team_form(aid), get_h2h(hid, aid)
        data = format_data(m, hf, af, hh)
        prompt = f"""Eres experto en CORNERS en ligas MENORES. Analiza:\n\n{data}\n\nResponde en español con:\n1. 🚩 Predicción corners total (Over/Under línea)\n2. 🎯 Apuesta con MÁS VALOR\n3. 💰 Stake (1-10)\n4. ⚠️ Por qué es underground (errores bookmaker)"
        pred = get_gemini_response(prompt)
        msg += f"⚽ *{h}* vs *{a}*\n🏆 {lg}\n\n{pred}\n\n{'━'*15}\n\n"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def goles(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⚽ Analizando goles...", parse_mode="Markdown")
    matches = get_underground()
    if not matches:
        await update.message.reply_text("📭 No hay partidos hoy.")
        return
    selected = matches[:2]
    msg = "⚽ *ANÁLISIS OVER/UNDER GOLES*\n\n"
    for m in selected:
        h, a = m["teams"]["home"]["name"], m["teams"]["away"]["name"]
        hid, aid = m["teams"]["home"]["id"], m["teams"]["away"]["id"]
        lg = m["league"]["name"]
        hf, af, hh = get_team_form(hid), get_team_form(aid), get_h2h(hid, aid)
        data = format_data(m, hf, af, hh)
        prompt = f"""Eres experto en OVER/UNDER GOLES en ligas MENORES. Analiza:\n\n{data}\n\nResponde en español con:\n1. ⚽ Predicción total goles (Over/Under línea)\n2. 🎯 BTTS (Sí/No)\n3. 💎 Apuesta con MÁS VALOR\n4. 💰 Stake (1-10)\n5. ⚠️ Errores del bookmaker en esta liga"
        pred = get_gemini_response(prompt)
        msg += f"⚽ *{h}* vs *{a}*\n🏆 {lg}\n\n{pred}\n\n{'━'*15}\n\n"
    await update.message.reply_text(msg, parse_mode="Markdown")

async def predecir(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 3:
        await update.message.reply_text("❌ Uso: `/predecir EquipoA vs EquipoB`", parse_mode="Markdown")
        return
    texto = " ".join(context.args)
    if " vs " not in texto:
        await update.message.reply_text("❌ Usa 'vs'. Ej: `/predecir Aalesund vs Start`", parse_mode="Markdown")
        return
    e1, e2 = texto.split(" vs ", 1)
    e1, e2 = e1.strip(), e2.strip()
    await update.message.reply_text(f"🔍 Analizando {e1} vs {e2}...", parse_mode="Markdown")

    today = datetime.now().strftime("%Y-%m-%d")
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    all_matches = get_underground(today) + get_underground(tomorrow)

    match = None
    for m in all_matches:
        hn = m["teams"]["home"]["name"].lower()
        an = m["teams"]["away"]["name"].lower()
        if (e1.lower() in hn or e1.lower() in an) and (e2.lower() in hn or e2.lower() in an):
            match = m
            break

    if not match:
        for d in [today, tomorrow]:
            mm = api_request("fixtures", {"date": d, "timezone": "America/Caracas"})
            for m in mm:
                hn = m["teams"]["home"]["name"].lower()
                an = m["teams"]["away"]["name"].lower()
                if (e1.lower() in hn or e1.lower() in an) and (e2.lower() in hn or e2.lower() in an):
                    match = m
                    break
            if match:
                break

    if match:
        h, a = match["teams"]["home"]["name"], match["teams"]["away"]["name"]
        hid, aid = match["teams"]["home"]["id"], match["teams"]["away"]["id"]
        lg = match["league"]["name"]
        is_ug = match["league"]["id"] not in LIGAS_TOP_IDS
        hf, af, hh = get_team_form(hid), get_team_form(aid), get_h2h(hid, aid)
        data = format_data(match, hf, af, hh)
        lt = "UNDERGROUND (errores en cuotas)" if is_ug else "LIGA TOP (mercado eficiente)"
        prompt = f"""Eres experto en VALOR en apuestas. Analiza:\n\n{data}\n\nLIGA: {lt}\n\nResponde en español con:\n1. 🏆 1X2\n2. 🚩 Corners Over/Under\n3. ⚽ Goles Over/Under + BTTS\n4. 💎 Apuesta con MÁS VALOR\n5. ⚠️ Riesgos\n6. 💰 Stake (1-10)\n7. 📈 Cuota justa estimada"
    else:
        prompt = f"""Analiza {e1} vs {e2}. Si no tienes datos, di: "No tengo datos. Usa /underground para ver disponibles.""""

    pred = get_gemini_response(prompt)
    await update.message.reply_text(f"⚽ *{e1}* vs *{e2}*\n🤖 *Gemini IA*\n\n{pred}", parse_mode="Markdown")

# ============ MAIN ============
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("underground", underground))
    app.add_handler(CommandHandler("corners", corners))
    app.add_handler(CommandHandler("goles", goles))
    app.add_handler(CommandHandler("predecir", predecir))
    app.add_handler(CommandHandler("vivo", vivo))
    print("🤖 Bot Underground iniciado!")
    app.run_polling()

if __name__ == "__main__":
    main()
