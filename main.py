import os
import requests
import json
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ============ CONFIGURACION ============
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
FOOTBALL_API_KEY = os.environ.get("FOOTBALL_API_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
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
        return "Gemini no pudo generar respuesta."
    except Exception as e:
        return f"Error: {str(e)}"

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
    data = "PARTIDO: " + home + " vs " + away + "
"
    data += "LIGA: " + league + "

"
    data += "FORMA " + home + ":
"
    for i, m in enumerate(home_form[:5], 1):
        h = m["teams"]["home"]["name"]
        a = m["teams"]["away"]["name"]
        hg = m["goals"]["home"] if m["goals"]["home"] is not None else "-"
        ag = m["goals"]["away"] if m["goals"]["away"] is not None else "-"
        data += str(i) + ". " + h + " " + str(hg) + "-" + str(ag) + " " + a + "
"
    data += "
FORMA " + away + ":
"
    for i, m in enumerate(away_form[:5], 1):
        h = m["teams"]["home"]["name"]
        a = m["teams"]["away"]["name"]
        hg = m["goals"]["home"] if m["goals"]["home"] is not None else "-"
        ag = m["goals"]["away"] if m["goals"]["away"] is not None else "-"
        data += str(i) + ". " + h + " " + str(hg) + "-" + str(ag) + " " + a + "
"
    data += "
H2H:
"
    for i, m in enumerate(h2h[:5], 1):
        h = m["teams"]["home"]["name"]
        a = m["teams"]["away"]["name"]
        hg = m["goals"]["home"] if m["goals"]["home"] is not None else "-"
        ag = m["goals"]["away"] if m["goals"]["away"] is not None else "-"
        data += str(i) + ". " + h + " " + str(hg) + "-" + str(ag) + " " + a + "
"
    return data

# ============ COMANDOS ============

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "Bot Underground Activado!

"
        "Ligas menores | Corners | Over/Under Goles
"
        "Gemini IA + API-Football

"
        "Comandos:
"
        "/underground - Partidos hoy
"
        "/corners - Analisis corners
"
        "/goles - Analisis goles
"
        "/predecir [A] vs [B] - Analisis completo
"
        "/vivo - En vivo ahora"
    )
    await update.message.reply_text(msg)

async def underground(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Buscando partidos underground...")
    matches = get_underground()
    if not matches:
        await update.message.reply_text("No hay partidos underground hoy.")
        return
    msg = "Underground - " + datetime.now().strftime("%d/%m") + "

"
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
        msg += league + ":
"
        for g in games[:2]:
            home = g["teams"]["home"]["name"]
            away = g["teams"]["away"]["name"]
            time = g["fixture"]["date"][11:16]
            msg += "  " + time + " " + home + " vs " + away + "
"
            count += 1
        msg += "
"
    await update.message.reply_text(msg)

async def vivo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Buscando en vivo...")
    matches = api_request("fixtures", {"live": "all", "timezone": "America/Caracas"})
    live = [m for m in matches if m.get("league", {}).get("id", 0) not in LIGAS_TOP_IDS]
    if not live:
        await update.message.reply_text("No hay underground en vivo.")
        return
    msg = "Underground EN VIVO:

"
    for m in live[:8]:
        h = m["teams"]["home"]["name"]
        a = m["teams"]["away"]["name"]
        hg = m["goals"]["home"] if m["goals"]["home"] is not None else 0
        ag = m["goals"]["away"] if m["goals"]["away"] is not None else 0
        mn = m["fixture"]["status"]["elapsed"]
        lg = m["league"]["name"]
        msg += h + " " + str(hg) + "-" + str(ag) + " " + a + " (" + str(mn) + "')
"
        msg += lg + "

"
    await update.message.reply_text(msg)

async def corners(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Analizando corners...")
    matches = get_underground()
    if not matches:
        await update.message.reply_text("No hay partidos hoy.")
        return
    selected = matches[:2]
    msg = "ANALISIS CORNERS

"
    for m in selected:
        h = m["teams"]["home"]["name"]
        a = m["teams"]["away"]["name"]
        hid = m["teams"]["home"]["id"]
        aid = m["teams"]["away"]["id"]
        lg = m["league"]["name"]
        hf = get_team_form(hid)
        af = get_team_form(aid)
        hh = get_h2h(hid, aid)
        data = format_data(m, hf, af, hh)
        prompt = "Eres experto en CORNERS en ligas MENORES. Analiza:

" + data + "

Responde en español con:
1. Prediccion corners total (Over/Under linea)
2. Apuesta con MAS VALOR
3. Stake (1-10)
4. Por que es underground (errores bookmaker)"
        pred = get_gemini_response(prompt)
        msg += h + " vs " + a + "
" + lg + "

" + pred + "

---

"
    await update.message.reply_text(msg)

async def goles(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Analizando goles...")
    matches = get_underground()
    if not matches:
        await update.message.reply_text("No hay partidos hoy.")
        return
    selected = matches[:2]
    msg = "ANALISIS OVER/UNDER GOLES

"
    for m in selected:
        h = m["teams"]["home"]["name"]
        a = m["teams"]["away"]["name"]
        hid = m["teams"]["home"]["id"]
        aid = m["teams"]["away"]["id"]
        lg = m["league"]["name"]
        hf = get_team_form(hid)
        af = get_team_form(aid)
        hh = get_h2h(hid, aid)
        data = format_data(m, hf, af, hh)
        prompt = "Eres experto en OVER/UNDER GOLES en ligas MENORES. Analiza:

" + data + "

Responde en español con:
1. Prediccion total goles (Over/Under linea)
2. BTTS (Si/No)
3. Apuesta con MAS VALOR
4. Stake (1-10)
5. Errores del bookmaker en esta liga"
        pred = get_gemini_response(prompt)
        msg += h + " vs " + a + "
" + lg + "

" + pred + "

---

"
    await update.message.reply_text(msg)

async def predecir(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 3:
        await update.message.reply_text("Uso: /predecir EquipoA vs EquipoB")
        return
    texto = " ".join(context.args)
    if " vs " not in texto:
        await update.message.reply_text("Usa vs. Ej: /predecir Aalesund vs Start")
        return
    e1, e2 = texto.split(" vs ", 1)
    e1 = e1.strip()
    e2 = e2.strip()
    await update.message.reply_text("Analizando " + e1 + " vs " + e2 + "...")

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
        h = match["teams"]["home"]["name"]
        a = match["teams"]["away"]["name"]
        hid = match["teams"]["home"]["id"]
        aid = match["teams"]["away"]["id"]
        lg = match["league"]["name"]
        is_ug = match["league"]["id"] not in LIGAS_TOP_IDS
        hf = get_team_form(hid)
        af = get_team_form(aid)
        hh = get_h2h(hid, aid)
        data = format_data(match, hf, af, hh)
        if is_ug:
            lt = "UNDERGROUND (errores en cuotas)"
        else:
            lt = "LIGA TOP (mercado eficiente)"
        prompt = "Eres experto en VALOR en apuestas. Analiza:

" + data + "

LIGA: " + lt + "

Responde en español con:
1. 1X2
2. Corners Over/Under
3. Goles Over/Under + BTTS
4. Apuesta con MAS VALOR
5. Riesgos
6. Stake (1-10)
7. Cuota justa estimada"
    else:
        prompt = "Analiza " + e1 + " vs " + e2 + ". Si no tienes datos, di: No tengo datos. Usa /underground para ver disponibles."

    pred = get_gemini_response(prompt)
    await update.message.reply_text(e1 + " vs " + e2 + "
Gemini IA

" + pred)

# ============ MAIN ============
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("underground", underground))
    app.add_handler(CommandHandler("corners", corners))
    app.add_handler(CommandHandler("goles", goles))
    app.add_handler(CommandHandler("predecir", predecir))
    app.add_handler(CommandHandler("vivo", vivo))
    print("Bot Underground iniciado!")
    app.run_polling()

if __name__ == "__main__":
    main()
