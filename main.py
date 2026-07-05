import os
import asyncio
import threading
import requests
import json
import sqlite3
import re
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ============ PUERTO FALSO PARA RENDER ============
class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK - Bot Underground running")
    def log_message(self, format, *args):
        pass

def start_dummy_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), DummyHandler)
    print(f"[Render] Puerto falso activo en {port}")
    server.serve_forever()

threading.Thread(target=start_dummy_server, daemon=True).start()
# ==================================================

# ============ CONFIGURACION ============
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
FOOTBALL_API_KEY = os.environ.get("FOOTBALL_API_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
FOOTBALL_URL = "https://v3.football.api-sports.io"

LIGAS_TOP_IDS = {
    39, 140, 135, 78, 61, 2, 3, 848, 94, 88, 144, 179, 292, 169, 119, 113, 203, 207, 271, 307
}

# ============ BASE DE DATOS ============
DB_PATH = "/tmp/picks.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS picks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fecha TEXT,
            comando TEXT,
            partido TEXT,
            liga TEXT,
            mercado TEXT,
            tipo TEXT,
            linea REAL,
            prediccion TEXT,
            valor TEXT,
            stake INTEGER,
            resultado TEXT DEFAULT 'pendiente',
            fecha_resultado TEXT,
            goles_home INTEGER,
            goles_away INTEGER,
            cuota_estimada REAL
        )
    """)
    conn.commit()
    conn.close()

init_db()

def save_pick(fecha, comando, partido, liga, mercado, tipo, linea, prediccion, valor, stake, cuota_estimada=None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO picks (fecha, comando, partido, liga, mercado, tipo, linea, prediccion, valor, stake, cuota_estimada)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (fecha, comando, partido, liga, mercado, tipo, linea, prediccion, valor, stake, cuota_estimada))
    conn.commit()
    conn.close()

def get_picks_pendientes():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, partido, mercado, tipo, linea FROM picks WHERE resultado = 'pendiente'")
    picks = c.fetchall()
    conn.close()
    return picks

def update_resultado(pick_id, resultado, goles_home, goles_away):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        UPDATE picks SET resultado = ?, fecha_resultado = ?, goles_home = ?, goles_away = ?
        WHERE id = ?
    """, (resultado, datetime.now().strftime("%Y-%m-%d"), goles_home, goles_away, pick_id))
    conn.commit()
    conn.close()

def get_stats(min_stake=0):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        SELECT 
            COUNT(*) as total,
            SUM(CASE WHEN resultado = 'ganado' THEN 1 ELSE 0 END) as ganados,
            SUM(CASE WHEN resultado = 'perdido' THEN 1 ELSE 0 END) as perdidos,
            SUM(CASE WHEN resultado = 'pendiente' THEN 1 ELSE 0 END) as pendientes,
            ROUND(AVG(CASE WHEN resultado != 'pendiente' THEN stake END), 1) as avg_stake,
            ROUND(
                100.0 * SUM(CASE WHEN resultado = 'ganado' THEN 1 ELSE 0 END) / 
                NULLIF(SUM(CASE WHEN resultado != 'pendiente' THEN 1 ELSE 0 END), 0),
                1
            ) as winrate
        FROM picks WHERE stake >= ?
    """, (min_stake,))
    stats = c.fetchone()

    c.execute("""
        SELECT 
            CASE 
                WHEN stake >= 8 THEN '8-10'
                WHEN stake >= 6 THEN '6-7'
                ELSE '1-5'
            END as rango,
            COUNT(*) as total,
            SUM(CASE WHEN resultado = 'ganado' THEN 1 ELSE 0 END) as ganados,
            SUM(CASE WHEN resultado = 'perdido' THEN 1 ELSE 0 END) as perdidos,
            ROUND(
                100.0 * SUM(CASE WHEN resultado = 'ganado' THEN 1 ELSE 0 END) / 
                NULLIF(SUM(CASE WHEN resultado != 'pendiente' THEN 1 ELSE 0 END), 0),
                1
            ) as winrate
        FROM picks WHERE resultado != 'pendiente' AND stake >= ?
        GROUP BY rango
        ORDER BY rango DESC
    """, (min_stake,))
    por_stake = c.fetchall()
    conn.close()
    return stats, por_stake

# ============ CONFIGURACION USUARIO ============
USER_CONFIG = {"min_stake": 0}

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

def get_matches(date_str: str = None, underground_only: bool = False):
    """Obtiene partidos. underground_only=True filtra ligas menores."""
    if not date_str:
        date_str = datetime.now().strftime("%Y-%m-%d")
    matches = api_request("fixtures", {"date": date_str, "timezone": "America/Caracas"})
    if underground_only:
        return [m for m in matches if m.get("league", {}).get("id", 0) not in LIGAS_TOP_IDS]
    return matches

def get_team_form(team_id: int):
    return api_request("fixtures", {"team": team_id, "last": 5})

def get_h2h(t1: int, t2: int):
    return api_request("fixtures/headtohead", {"h2h": f"{t1}-{t2}", "last": 5})

def format_data(match, home_form, away_form, h2h):
    home = match["teams"]["home"]["name"]
    away = match["teams"]["away"]["name"]
    league = match["league"]["name"]

    lines = []
    lines.append("PARTIDO: " + home + " vs " + away)
    lines.append("LIGA: " + league)
    lines.append("")
    lines.append("FORMA " + home + ":")

    for i, m in enumerate(home_form[:5], 1):
        h = m["teams"]["home"]["name"]
        a = m["teams"]["away"]["name"]
        hg = m["goals"]["home"] if m["goals"]["home"] is not None else "-"
        ag = m["goals"]["away"] if m["goals"]["away"] is not None else "-"
        lines.append(str(i) + ". " + h + " " + str(hg) + "-" + str(ag) + " " + a)

    lines.append("")
    lines.append("FORMA " + away + ":")

    for i, m in enumerate(away_form[:5], 1):
        h = m["teams"]["home"]["name"]
        a = m["teams"]["away"]["name"]
        hg = m["goals"]["home"] if m["goals"]["home"] is not None else "-"
        ag = m["goals"]["away"] if m["goals"]["away"] is not None else "-"
        lines.append(str(i) + ". " + h + " " + str(hg) + "-" + str(ag) + " " + a)

    lines.append("")
    lines.append("H2H:")

    for i, m in enumerate(h2h[:5], 1):
        h = m["teams"]["home"]["name"]
        a = m["teams"]["away"]["name"]
        hg = m["goals"]["home"] if m["goals"]["home"] is not None else "-"
        ag = m["goals"]["away"] if m["goals"]["away"] is not None else "-"
        lines.append(str(i) + ". " + h + " " + str(hg) + "-" + str(ag) + " " + a)

    return "\n".join(lines)

def parse_prediccion(texto: str, mercado_default: str):
    """Extrae datos estructurados de la respuesta de Gemini"""
    resultado = {
        "prediccion": "",
        "mercado": mercado_default,
        "tipo": "",
        "linea": 0.0,
        "valor": "",
        "stake": 5,
        "cuota": None
    }

    lines = texto.split("\n")
    for line in lines:
        line = line.strip()
        if line.upper().startswith("PREDICCION:"):
            resultado["prediccion"] = line.split(":", 1)[1].strip()
            if "over" in line.lower():
                resultado["tipo"] = "over"
            elif "under" in line.lower():
                resultado["tipo"] = "under"
            elif "si" in line.lower() or "yes" in line.lower():
                resultado["tipo"] = "si"
            elif "no" in line.lower():
                resultado["tipo"] = "no"

            nums = re.findall(r'\d+\.\d+|\d+', line)
            if nums:
                try:
                    resultado["linea"] = float(nums[0])
                except:
                    pass

        elif line.upper().startswith("VALOR:"):
            resultado["valor"] = line.split(":", 1)[1].strip()
            cuota_match = re.search(r'@\s*([\d.]+)', line)
            if cuota_match:
                try:
                    resultado["cuota"] = float(cuota_match.group(1))
                except:
                    pass

        elif line.upper().startswith("STAKE:"):
            stake_text = line.split(":", 1)[1].strip()
            nums = re.findall(r'\d+', stake_text)
            if nums:
                try:
                    resultado["stake"] = int(nums[0])
                except:
                    pass

    return resultado

def evaluar_resultado(mercado: str, tipo: str, linea: float, goles_home: int, goles_away: int):
    """Determina si un pick ganó o perdió"""
    if mercado == "goles":
        total = goles_home + goles_away
        if tipo == "over":
            return "ganado" if total > linea else "perdido"
        elif tipo == "under":
            return "ganado" if total < linea else "perdido"
    elif mercado == "corners":
        return "pendiente"  # API gratuita no da corners
    return "pendiente"

# ============ COMANDOS ============

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "🤖 Bot Underground Activado!\n\n"
        "📊 COMANDOS:\n"
        "\n🌍 BÚSQUEDA:\n"
        "/partidos - Todos los partidos del día\n"
        "/underground - Solo ligas menores\n"
        "/vivo - Partidos en vivo ahora\n"
        "/live - Picks en vivo (live betting)\n"
        "\n📈 ANÁLISIS:\n"
        "/corners - Análisis corners\n"
        "/goles - Análisis Over/Under goles\n"
        "/predecir [A] vs [B] - Análisis completo\n"
        "\n⚙️ GESTIÓN:\n"
        "/actualizar - Actualizar resultados automático\n"
        "/stats - Ver estadísticas\n"
        "/config - Ver configuración"
    )
    await update.message.reply_text(msg)

async def config(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) >= 2:
        key = context.args[0].lower()
        if key == "min_stake":
            try:
                valor = int(context.args[1])
                USER_CONFIG["min_stake"] = valor
                await update.message.reply_text(f"✅ min_stake configurado: {valor}")
            except:
                await update.message.reply_text("❌ Uso: /config min_stake 7")
        else:
            await update.message.reply_text(f"❌ Config desconocida: {key}")
    else:
        config_text = "⚙️ CONFIGURACIÓN ACTUAL:\n\n"
        for k, v in USER_CONFIG.items():
            config_text += f"{k}: {v}\n"
        config_text += "\nUso: /config min_stake 7"
        await update.message.reply_text(config_text)

async def partidos(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Todos los partidos del día (todas las ligas)"""
    await update.message.reply_text("🔍 Buscando TODOS los partidos del día...")
    matches = get_matches(underground_only=False)
    if not matches:
        await update.message.reply_text("No hay partidos hoy.")
        return

    msg_lines = ["📅 TODOS LOS PARTIDOS - " + datetime.now().strftime("%d/%m") + "\n"]
    by_league = {}
    for m in matches:
        ln = m["league"]["name"]
        if ln not in by_league:
            by_league[ln] = []
        by_league[ln].append(m)

    count = 0
    for league, games in sorted(by_league.items()):
        if count >= 15:
            break
        msg_lines.append(f"🏆 {league}:")
        for g in games[:2]:
            home = g["teams"]["home"]["name"]
            away = g["teams"]["away"]["name"]
            time = g["fixture"]["date"][11:16]
            is_ug = g["league"]["id"] not in LIGAS_TOP_IDS
            tag = " 🟢" if is_ug else ""
            msg_lines.append(f"  {time} {home} vs {away}{tag}")
            count += 1
        msg_lines.append("")

    msg_lines.append("🟢 = Liga menor (underground)")
    await update.message.reply_text("\n".join(msg_lines))

async def underground(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Solo ligas menores"""
    await update.message.reply_text("🔍 Buscando partidos UNDERGROUND...")
    matches = get_matches(underground_only=True)
    if not matches:
        await update.message.reply_text("No hay partidos underground hoy.")
        return

    msg_lines = ["📅 UNDERGROUND - " + datetime.now().strftime("%d/%m") + "\n"]
    by_league = {}
    for m in matches:
        ln = m["league"]["name"]
        if ln not in by_league:
            by_league[ln] = []
        by_league[ln].append(m)

    count = 0
    for league, games in sorted(by_league.items()):
        if count >= 12:
            break
        msg_lines.append(f"🏆 {league}:")
        for g in games[:2]:
            home = g["teams"]["home"]["name"]
            away = g["teams"]["away"]["name"]
            time = g["fixture"]["date"][11:16]
            msg_lines.append(f"  {time} {home} vs {away}")
            count += 1
        msg_lines.append("")

    await update.message.reply_text("\n".join(msg_lines))

async def vivo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Partidos en vivo ahora"""
    await update.message.reply_text("🔴 Buscando partidos EN VIVO...")
    matches = api_request("fixtures", {"live": "all", "timezone": "America/Caracas"})

    # Todos los partidos en vivo
    all_live = matches
    ug_live = [m for m in matches if m.get("league", {}).get("id", 0) not in LIGAS_TOP_IDS]

    if not all_live:
        await update.message.reply_text("No hay partidos en vivo ahora.")
        return

    msg_lines = ["🔴 EN VIVO AHORA:\n"]
    for m in all_live[:10]:
        h = m["teams"]["home"]["name"]
        a = m["teams"]["away"]["name"]
        hg = m["goals"]["home"] if m["goals"]["home"] is not None else 0
        ag = m["goals"]["away"] if m["goals"]["away"] is not None else 0
        mn = m["fixture"]["status"]["elapsed"]
        lg = m["league"]["name"]
        is_ug = m["league"]["id"] not in LIGAS_TOP_IDS
        tag = " 🟢UG" if is_ug else ""
        msg_lines.append(f"{h} {hg}-{ag} {a} ({mn}\'){tag}")
        msg_lines.append(f"  {lg}")
        msg_lines.append("")

    msg_lines.append(f"\n📊 Total: {len(all_live)} partidos | 🟢 Underground: {len(ug_live)}")
    await update.message.reply_text("\n".join(msg_lines))

async def live(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Análisis de partidos en vivo para live betting"""
    await update.message.reply_text("⚡ Analizando partidos en vivo para LIVE BETTING...")
    matches = api_request("fixtures", {"live": "all", "timezone": "America/Caracas"})

    if not matches:
        await update.message.reply_text("No hay partidos en vivo ahora.")
        return

    # Filtrar partidos en minuto 30-75 (mejor ventana para live)
    live_matches = []
    for m in matches:
        mn = m["fixture"]["status"]["elapsed"]
        if mn and 30 <= mn <= 75:
            live_matches.append(m)

    if not live_matches:
        await update.message.reply_text("No hay partidos en ventana óptima (30-75 min). Intenta más tarde.")
        return

    # Tomar hasta 2 partidos
    selected = live_matches[:2]
    fecha_hoy = datetime.now().strftime("%Y-%m-%d")
    min_stake = USER_CONFIG.get("min_stake", 0)
    mostrados = 0

    for m in selected:
        h = m["teams"]["home"]["name"]
        a = m["teams"]["away"]["name"]
        hid = m["teams"]["home"]["id"]
        aid = m["teams"]["away"]["id"]
        lg = m["league"]["name"]
        mn = m["fixture"]["status"]["elapsed"]
        hg = m["goals"]["home"] if m["goals"]["home"] is not None else 0
        ag = m["goals"]["away"] if m["goals"]["away"] is not None else 0

        hf = get_team_form(hid)
        af = get_team_form(aid)
        hh = get_h2h(hid, aid)
        data = format_data(m, hf, af, hh)

        prompt = (
            f"Eres experto en LIVE BETTING. El partido va {hg}-{ag} al minuto {mn}. "
            f"Analiza estos datos y da picks para el RESTO del partido. "
            f"Responde EXACTAMENTE en este formato (max 500 chars):\n\n"
            + data
            + f"\n\nMARCADOR ACTUAL: {h} {hg}-{ag} {a} ({mn}\')\n"
            + "\nFORMATO OBLIGATORIO:\n"
            "PREDICCION: [ej: Over 1.5 goles 2do tiempo, o Next Goal local]\n"
            "VALOR: [apuesta con mas valor y cuota estimada con @]\n"
            "STAKE: [1-10]\n"
            "POR QUE: [1 linea maximo]"
        )

        pred_text = get_gemini_response(prompt)
        parsed = parse_prediccion(pred_text, "live")

        save_pick(
            fecha=fecha_hoy,
            comando="live",
            partido=f"{h} vs {a}",
            liga=lg,
            mercado="live",
            tipo=parsed["tipo"],
            linea=parsed["linea"],
            prediccion=parsed["prediccion"],
            valor=parsed["valor"],
            stake=parsed["stake"],
            cuota_estimada=parsed["cuota"]
        )

        if parsed["stake"] >= min_stake:
            msg = f"⚡ LIVE - {h} {hg}-{ag} {a} ({mn}\')\n🏆 {lg}\n\n{pred_text}"
            await update.message.reply_text(msg)
            mostrados += 1

    if mostrados == 0 and min_stake > 0:
        await update.message.reply_text(f"📊 Ningún live pick cumple con stake ≥ {min_stake}.")

async def corners(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Análisis de corners - busca en TODAS las ligas, no solo underground"""
    await update.message.reply_text("🚩 Analizando corners (TODAS las ligas)...")
    matches = get_matches(underground_only=False)
    if not matches:
        await update.message.reply_text("No hay partidos hoy.")
        return

    selected = matches[:2]
    fecha_hoy = datetime.now().strftime("%Y-%m-%d")
    min_stake = USER_CONFIG.get("min_stake", 0)
    mostrados = 0

    for m in selected:
        h = m["teams"]["home"]["name"]
        a = m["teams"]["away"]["name"]
        hid = m["teams"]["home"]["id"]
        aid = m["teams"]["away"]["id"]
        lg = m["league"]["name"]
        is_ug = m["league"]["id"] not in LIGAS_TOP_IDS
        hf = get_team_form(hid)
        af = get_team_form(aid)
        hh = get_h2h(hid, aid)
        data = format_data(m, hf, af, hh)

        tag = "🟢 UNDERGROUND | " if is_ug else "🔴 LIGA TOP | "

        prompt = (
            tag + "Eres experto en CORNERS. Analiza estos datos y responde EXACTAMENTE en este formato (max 600 chars):\n\n"
            + data
            + "\n\nFORMATO OBLIGATORIO:\n"
            "PREDICCION: [Over/Under X.5 corners]\n"
            "VALOR: [apuesta con mas valor y cuota estimada con @]\n"
            "STAKE: [1-10]\n"
            "POR QUE: [1-2 lineas maximo]"
        )

        pred_text = get_gemini_response(prompt)
        parsed = parse_prediccion(pred_text, "corners")

        save_pick(
            fecha=fecha_hoy,
            comando="corners",
            partido=f"{h} vs {a}",
            liga=lg,
            mercado="corners",
            tipo=parsed["tipo"],
            linea=parsed["linea"],
            prediccion=parsed["prediccion"],
            valor=parsed["valor"],
            stake=parsed["stake"],
            cuota_estimada=parsed["cuota"]
        )

        if parsed["stake"] >= min_stake:
            ug_tag = "🟢 " if is_ug else "🔴 "
            msg = f"{ug_tag}🚩 {h} vs {a}\n🏆 {lg}\n\n{pred_text}"
            await update.message.reply_text(msg)
            mostrados += 1

    if mostrados == 0 and min_stake > 0:
        await update.message.reply_text(f"📊 Ningún pick cumple con stake ≥ {min_stake}. Usa /config min_stake 0 para ver todos.")

async def goles(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Análisis de goles - busca en TODAS las ligas"""
    await update.message.reply_text("⚽ Analizando goles (TODAS las ligas)...")
    matches = get_matches(underground_only=False)
    if not matches:
        await update.message.reply_text("No hay partidos hoy.")
        return

    selected = matches[:2]
    fecha_hoy = datetime.now().strftime("%Y-%m-%d")
    min_stake = USER_CONFIG.get("min_stake", 0)
    mostrados = 0

    for m in selected:
        h = m["teams"]["home"]["name"]
        a = m["teams"]["away"]["name"]
        hid = m["teams"]["home"]["id"]
        aid = m["teams"]["away"]["id"]
        lg = m["league"]["name"]
        is_ug = m["league"]["id"] not in LIGAS_TOP_IDS
        hf = get_team_form(hid)
        af = get_team_form(aid)
        hh = get_h2h(hid, aid)
        data = format_data(m, hf, af, hh)

        tag = "🟢 UNDERGROUND | " if is_ug else "🔴 LIGA TOP | "

        prompt = (
            tag + "Eres experto en OVER/UNDER GOLES. Analiza estos datos y responde EXACTAMENTE en este formato (max 600 chars):\n\n"
            + data
            + "\n\nFORMATO OBLIGATORIO:\n"
            "PREDICCION: [Over/Under X.5 goles]\n"
            "BTTS: [Si/No]\n"
            "VALOR: [apuesta con mas valor y cuota estimada con @]\n"
            "STAKE: [1-10]\n"
            "POR QUE: [1-2 lineas maximo]"
        )

        pred_text = get_gemini_response(prompt)
        parsed = parse_prediccion(pred_text, "goles")

        save_pick(
            fecha=fecha_hoy,
            comando="goles",
            partido=f"{h} vs {a}",
            liga=lg,
            mercado="goles",
            tipo=parsed["tipo"],
            linea=parsed["linea"],
            prediccion=parsed["prediccion"],
            valor=parsed["valor"],
            stake=parsed["stake"],
            cuota_estimada=parsed["cuota"]
        )

        if parsed["stake"] >= min_stake:
            ug_tag = "🟢 " if is_ug else "🔴 "
            msg = f"{ug_tag}⚽ {h} vs {a}\n🏆 {lg}\n\n{pred_text}"
            await update.message.reply_text(msg)
            mostrados += 1

    if mostrados == 0 and min_stake > 0:
        await update.message.reply_text(f"📊 Ningún pick cumple con stake ≥ {min_stake}. Usa /config min_stake 0 para ver todos.")

async def predecir(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Análisis completo de un partido específico - busca en TODAS las ligas"""
    if len(context.args) < 3:
        await update.message.reply_text("❌ Uso: /predecir EquipoA vs EquipoB")
        return

    texto = " ".join(context.args)
    if " vs " not in texto:
        await update.message.reply_text("❌ Usa 'vs'. Ej: /predecir Aalesund vs Start")
        return

    e1, e2 = texto.split(" vs ", 1)
    e1 = e1.strip()
    e2 = e2.strip()
    await update.message.reply_text(f"🔍 Analizando {e1} vs {e2}...")

    today = datetime.now().strftime("%Y-%m-%d")
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    all_matches = get_matches(today, underground_only=False) + get_matches(tomorrow, underground_only=False)

    match = None
    for m in all_matches:
        hn = m["teams"]["home"]["name"].lower()
        an = m["teams"]["away"]["name"].lower()
        if (e1.lower() in hn or e1.lower() in an) and (e2.lower() in hn or e2.lower() in an):
            match = m
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
            lt = "🟢 UNDERGROUND (errores en cuotas)"
        else:
            lt = "🔴 LIGA TOP (mercado eficiente)"

        prompt = (
            lt + " | Eres experto en VALOR en apuestas. Analiza estos datos y responde EXACTAMENTE en este formato (max 700 chars):\n\n"
            + data
            + "\n\nFORMATO OBLIGATORIO:\n"
            "1X2: [prediccion]\n"
            "CORNERS: [Over/Under X.5]\n"
            "GOLES: [Over/Under X.5]\n"
            "BTTS: [Si/No]\n"
            "MEJOR VALOR: [apuesta con mas valor y cuota estimada con @]\n"
            "STAKE: [1-10]\n"
            "RIESGO: [1 linea]"
        )

        pred_text = get_gemini_response(prompt)
        parsed = parse_prediccion(pred_text, "mix")

        save_pick(
            fecha=today,
            comando="predecir",
            partido=f"{h} vs {a}",
            liga=lg,
            mercado="mix",
            tipo=parsed["tipo"],
            linea=parsed["linea"],
            prediccion=parsed["prediccion"],
            valor=parsed["valor"],
            stake=parsed["stake"],
            cuota_estimada=parsed["cuota"]
        )

        await update.message.reply_text(f"🔮 {e1} vs {e2}\n🏆 {lg}\n{lt}\n\n{pred_text}")
    else:
        await update.message.reply_text(f"❌ No encontré {e1} vs {e2}. Usa /partidos para ver disponibles.")

async def actualizar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Actualiza resultados automáticamente"""
    await update.message.reply_text("🔄 Actualizando resultados...")

    picks = get_picks_pendientes()
    if not picks:
        await update.message.reply_text("✅ No hay picks pendientes.")
        return

    actualizados = 0
    ganados = 0
    perdidos = 0

    for pick in picks:
        pick_id, partido, mercado, tipo, linea = pick

        if " vs " in partido:
            eq1, eq2 = partido.split(" vs ", 1)
        else:
            continue

        today = datetime.now().strftime("%Y-%m-%d")
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

        found = False
        for d in [yesterday, today]:
            matches = api_request("fixtures", {"date": d, "timezone": "America/Caracas"})
            for m in matches:
                hn = m["teams"]["home"]["name"].lower()
                an = m["teams"]["away"]["name"].lower()
                if (eq1.lower() in hn or eq1.lower() in an) and (eq2.lower() in hn or eq2.lower() in an):
                    status = m["fixture"]["status"]["short"]
                    if status in ("FT", "AET", "PEN"):
                        gh = m["goals"]["home"] or 0
                        ga = m["goals"]["away"] or 0

                        resultado = evaluar_resultado(mercado, tipo, linea, gh, ga)
                        if resultado == "pendiente" and mercado == "corners":
                            continue

                        update_resultado(pick_id, resultado, gh, ga)
                        actualizados += 1
                        if resultado == "ganado":
                            ganados += 1
                        elif resultado == "perdido":
                            perdidos += 1
                        found = True
                    break
            if found:
                break

    msg = f"📊 Actualización completada:\n"
    msg += f"🔄 {actualizados} picks actualizados\n"
    msg += f"🟢 {ganados} ganados\n"
    msg += f"🔴 {perdidos} perdidos\n"
    if actualizados > 0:
        winrate = round(100 * ganados / actualizados, 1)
        msg += f"📈 Winrate: {winrate}%"

    await update.message.reply_text(msg)

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra estadísticas del bot"""
    min_stake = USER_CONFIG.get("min_stake", 0)
    stats_data, por_stake = get_stats(min_stake)

    total, ganados, perdidos, pendientes, avg_stake, winrate = stats_data

    msg = f"📊 ESTADÍSTICAS DEL BOT\n"
    msg += f"Filtro: stake ≥ {min_stake}\n\n"
    msg += f"📋 Total picks: {total}\n"
    msg += f"⏳ Pendientes: {pendientes}\n"
    msg += f"✅ Ganados: {ganados}\n"
    msg += f"❌ Perdidos: {perdidos}\n"

    if ganados + perdidos > 0:
        msg += f"📈 Winrate: {winrate}%\n"
        msg += f"🎯 Stake promedio: {avg_stake}\n"

    if por_stake:
        msg += f"\n📊 POR RANGO DE STAKE:\n"
        for rango, r_total, r_ganados, r_perdidos, r_winrate in por_stake:
            msg += f"Stake {rango}: {r_total} picks → {r_winrate}% acierto\n"

    await update.message.reply_text(msg)

# ============ MAIN ============
async def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("partidos", partidos))
    app.add_handler(CommandHandler("underground", underground))
    app.add_handler(CommandHandler("corners", corners))
    app.add_handler(CommandHandler("goles", goles))
    app.add_handler(CommandHandler("predecir", predecir))
    app.add_handler(CommandHandler("vivo", vivo))
    app.add_handler(CommandHandler("live", live))
    app.add_handler(CommandHandler("actualizar", actualizar))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("config", config))
    print("Bot Underground iniciado!")
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
