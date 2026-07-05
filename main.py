import os
import asyncio
import threading
import requests
import json
import sqlite3
import re
import unicodedata
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
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

USER_TIMEZONE = "America/Caracas"

# ============ UTILIDADES ============
def normalize_text(text):
    """Quita acentos y normaliza para búsqueda robusta"""
    if not text:
        return ""
    return ''.join(c for c in unicodedata.normalize('NFD', text) if unicodedata.category(c) != 'Mn').lower().strip()

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
            cuota_estimada REAL,
            resultado TEXT DEFAULT 'pendiente',
            fecha_resultado TEXT,
            goles_home INTEGER,
            goles_away INTEGER
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
    """, (resultado, datetime.now(ZoneInfo(USER_TIMEZONE)).strftime("%Y-%m-%d"), goles_home, goles_away, pick_id))
    conn.commit()
    conn.close()

def get_stats(min_stake=0, min_odds=0, max_odds=999):
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
        FROM picks 
        WHERE stake >= ? AND (cuota_estimada >= ? OR cuota_estimada IS NULL) AND (cuota_estimada <= ? OR cuota_estimada IS NULL)
    """, (min_stake, min_odds, max_odds))
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
        FROM picks 
        WHERE resultado != 'pendiente' AND stake >= ? AND (cuota_estimada >= ? OR cuota_estimada IS NULL) AND (cuota_estimada <= ? OR cuota_estimada IS NULL)
        GROUP BY rango
        ORDER BY rango DESC
    """, (min_stake, min_odds, max_odds))
    por_stake = c.fetchall()
    conn.close()
    return stats, por_stake

# ============ CONFIGURACION USUARIO ============
USER_CONFIG = {"min_stake": 0, "min_odds": 0.0, "max_odds": 999.0}

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

def get_today_str():
    """Fecha de hoy en la zona horaria del usuario"""
    return datetime.now(ZoneInfo(USER_TIMEZONE)).strftime("%Y-%m-%d")

def get_matches(date_str: str = None, underground_only: bool = False, status_filter: tuple = ("NS", "TBD")):
    """Obtiene partidos de una fecha. Por defecto solo pre-partido (NS/TBD)."""
    if not date_str:
        date_str = get_today_str()
    matches = api_request("fixtures", {"date": date_str, "timezone": USER_TIMEZONE})
    if status_filter:
        matches = [m for m in matches if m.get("fixture", {}).get("status", {}).get("short", "") in status_filter]
    if underground_only:
        matches = [m for m in matches if m.get("league", {}).get("id", 0) not in LIGAS_TOP_IDS]
    matches.sort(key=lambda x: x.get("fixture", {}).get("timestamp", 0))
    return matches

def get_team_form(team_id: int):
    return api_request("fixtures", {"team": team_id, "last": 4})

def get_h2h(t1: int, t2: int):
    return api_request("fixtures/headtohead", {"h2h": f"{t1}-{t2}", "last": 4})

def format_match_time(match):
    """Formatea la hora del partido en zona horaria del usuario"""
    fixture_date = match.get("fixture", {}).get("date", "")
    try:
        dt = datetime.fromisoformat(fixture_date.replace("Z", "+00:00"))
        local_dt = dt.astimezone(ZoneInfo(USER_TIMEZONE))
        return local_dt.strftime("%H:%M")
    except:
        return "Hora desconocida"

def format_data(match, home_form, away_form, h2h):
    home = match["teams"]["home"]["name"]
    away = match["teams"]["away"]["name"]
    league = match["league"]["name"]
    match_time = format_match_time(match)

    lines = []
    lines.append("PARTIDO: " + home + " vs " + away)
    lines.append("LIGA: " + league)
    lines.append("HORA LOCAL: " + match_time)
    lines.append("")
    lines.append("FORMA " + home + " (ultimos 4):")

    for i, m in enumerate(home_form[:4], 1):
        h = m["teams"]["home"]["name"]
        a = m["teams"]["away"]["name"]
        hg = m["goals"]["home"] if m["goals"]["home"] is not None else "-"
        ag = m["goals"]["away"] if m["goals"]["away"] is not None else "-"
        lines.append(str(i) + ". " + h + " " + str(hg) + "-" + str(ag) + " " + a)

    lines.append("")
    lines.append("FORMA " + away + " (ultimos 4):")

    for i, m in enumerate(away_form[:4], 1):
        h = m["teams"]["home"]["name"]
        a = m["teams"]["away"]["name"]
        hg = m["goals"]["home"] if m["goals"]["home"] is not None else "-"
        ag = m["goals"]["away"] if m["goals"]["away"] is not None else "-"
        lines.append(str(i) + ". " + h + " " + str(hg) + "-" + str(ag) + " " + a)

    lines.append("")
    lines.append("H2H (ultimos 4):")

    for i, m in enumerate(h2h[:4], 1):
        h = m["teams"]["home"]["name"]
        a = m["teams"]["away"]["name"]
        hg = m["goals"]["home"] if m["goals"]["home"] is not None else "-"
        ag = m["goals"]["away"] if m["goals"]["away"] is not None else "-"
        lines.append(str(i) + ". " + h + " " + str(hg) + "-" + str(ag) + " " + a)

    return "\n".join(lines)

def parse_prediccion(texto: str, mercado_default: str):
    """Parsea la respuesta de Gemini de forma robusta"""
    resultado = {
        "prediccion": "",
        "mercado": mercado_default,
        "tipo": "",
        "linea": 0.0,
        "valor": "",
        "stake": 0,
        "cuota": None,
        "puntuacion": 0,
        "analisis": ""
    }

    # Si Gemini dice NO HAY VALOR, marcar como invalido
    if "NO HAY VALOR" in texto.upper():
        resultado["puntuacion"] = 0
        return resultado

    lines = texto.split("\n")
    for line in lines:
        line = line.strip()
        if not line:
            continue

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

        elif line.upper().startswith("PUNTUACION:"):
            # Busca patrones como "7/10", "7 / 10", "7"
            punt_match = re.search(r'(\d+)[\s/]*(?:10)?', line.split(":", 1)[1].strip())
            if punt_match:
                try:
                    resultado["puntuacion"] = int(punt_match.group(1))
                except:
                    pass

        elif line.upper().startswith("POR QUE:"):
            resultado["analisis"] = line.split(":", 1)[1].strip()

    # Si no hay stake explicito pero hay puntuacion, usar puntuacion como stake
    if resultado["stake"] == 0 and resultado["puntuacion"] > 0:
        resultado["stake"] = resultado["puntuacion"]

    return resultado

def pick_es_valido(parsed, min_stake=5):
    """Valida que un pick tenga datos minimos para mostrarse"""
    if parsed["puntuacion"] < min_stake:
        return False
    if not parsed["prediccion"]:
        return False
    if not parsed["valor"]:
        return False
    if not parsed["analisis"]:
        return False
    if parsed["stake"] < min_stake:
        return False
    return True

def evaluar_resultado(mercado: str, tipo: str, linea: float, goles_home: int, goles_away: int):
    if mercado == "goles":
        total = goles_home + goles_away
        if tipo == "over":
            return "ganado" if total > linea else "perdido"
        elif tipo == "under":
            return "ganado" if total < linea else "perdido"
    elif mercado == "corners":
        return "pendiente"
    return "pendiente"

async def auto_actualizar(context: ContextTypes.DEFAULT_TYPE):
    picks = get_picks_pendientes()
    if not picks:
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

        today = get_today_str()
        yesterday = (datetime.now(ZoneInfo(USER_TIMEZONE)) - timedelta(days=1)).strftime("%Y-%m-%d")

        found = False
        for d in [yesterday, today]:
            matches = api_request("fixtures", {"date": d, "timezone": USER_TIMEZONE})
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

    if actualizados > 0:
        print(f"[Auto] {actualizados} picks actualizados | {ganados}G {perdidos}P")

# ============ COMANDOS ============

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "🤖 Bot de Apuestas - Akinator Ronaldinho\n\n"
        "/hoy - Picks del día (goles + corners)\n"
        "/live - Picks en vivo ahora\n"
        "/predecir [A] vs [B] - Análisis de un partido\n"
        "/stake [1-10] - Filtro de confianza mínima\n"
        "/odds [min,max] - Filtro de rango de cuotas\n"
        "/stats - Tu rendimiento"
    )
    await update.message.reply_text(msg)

async def stake(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) == 1:
        try:
            valor = int(context.args[0])
            if 1 <= valor <= 10:
                USER_CONFIG["min_stake"] = valor
                await update.message.reply_text(f"✅ Stake mínimo: {valor}")
            else:
                await update.message.reply_text("❌ Stake entre 1 y 10")
        except:
            await update.message.reply_text("❌ Uso: /stake 7")
    else:
        await update.message.reply_text(f"⚙️ Stake mínimo: {USER_CONFIG['min_stake']}\nUso: /stake 7")

async def odds(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) == 1:
        try:
            rango = context.args[0].replace(" ", "")
            partes = rango.split(",")
            if len(partes) == 2:
                min_odds = float(partes[0])
                max_odds = float(partes[1])
                USER_CONFIG["min_odds"] = min_odds
                USER_CONFIG["max_odds"] = max_odds
                await update.message.reply_text(f"✅ Cuotas: {min_odds} - {max_odds}")
            else:
                await update.message.reply_text("❌ Uso: /odds 1.5,3.0")
        except:
            await update.message.reply_text("❌ Uso: /odds 1.5,3.0")
    else:
        await update.message.reply_text(
            f"⚙️ Cuotas: {USER_CONFIG['min_odds']} - {USER_CONFIG['max_odds']}\n"
            f"Uso: /odds 1.5,3.0"
        )

def build_prompt_goles(h, a, lg, data, ug_tag):
    return (
        f"Eres un analista de apuestas deportivas. Evalúa el mercado de GOLES (Over/Under) para este partido específico y decide:\n"
        f"1. ¿Over o Under?\n"
        f"2. ¿Qué línea? (0.5, 1.5, 2.5, 3.5, 4.5)\n\n"
        f"RÚBRICA DE PUNTUACIÓN (0-10 puntos). Evalúa con los datos proporcionados:\n\n"
        f"1. PROMEDIO GOLES LOCAL (últimos 4):\n"
        f"   - >3.0 goles/partido = 2 pts\n"
        f"   - 2.0-3.0 = 1 pt\n"
        f"   - <2.0 = 0 pts\n\n"
        f"2. PROMEDIO GOLES VISITANTE (últimos 4):\n"
        f"   - >3.0 = 2 pts\n"
        f"   - 2.0-3.0 = 1 pt\n"
        f"   - <2.0 = 0 pts\n\n"
        f"3. H2H PROMEDIO GOLES (últimos 4):\n"
        f"   - >3.5 = 2 pts\n"
        f"   - 2.0-3.5 = 1 pt\n"
        f"   - <2.0 = 0 pts\n\n"
        f"4. TENDENCIA DE LIGA (0-1 pt):\n"
        f"   - ¿La liga tiene fama de alta/baja en goles?\n\n"
        f"5. EDGE DE CUOTA (0-2 pts):\n"
        f"   - Cuota estimada ≥1.70 = 2 pts, ≥1.50 = 1 pt, <1.50 = 0 pts\n\n"
        f"6. NIVEL DE DATOS (0-1 pt):\n"
        f"   - ¿Tenemos ≥4 partidos recientes? = 1 pt\n\n"
        f"REGLAS PARA DECIDIR LA LÍNEA:\n"
        f"- Si total ≥7 Y ambos promedios >2.5 → Over 3.5 (o 4.5 si es extremo)\n"
        f"- Si total ≥7 Y un promedio >2.5, otro <2.0 → Over 2.5\n"
        f"- Si total 5-6 Y ambos promedios 2.0-2.5 → Over 2.5\n"
        f"- Si total 5-6 Y ambos promedios <2.0 → Under 1.5 o Under 2.5\n"
        f"- Si total <5 → responde 'NO HAY VALOR' y NO generes pick\n\n"
        f"REGLAS PARA STAKE (el stake DEBE ser igual a la puntuación):\n"
        f"- 5-6 pts → Stake 5-6\n"
        f"- 7-8 pts → Stake 7-8\n"
        f"- 9-10 pts → Stake 9-10 (excepcional, raro)\n\n"
        f"FORMATO OBLIGATORIO:\n"
        f"PREDICCION: [Over/Under X.5 goles]\n"
        f"VALOR: [cuota estimada con @]\n"
        f"STAKE: [1-10]\n"
        f"PUNTUACION: [X/10]\n"
        f"POR QUE: [2 líneas máximo con estadística CONCRETA]\n\n"
        f"Datos del partido:\n{data}"
    )

def build_prompt_corners(h, a, lg, data, ug_tag):
    return (
        f"Eres un analista de apuestas deportivas. Evalúa el mercado de CORNERS (Over/Under) para este partido específico y decide:\n"
        f"1. ¿Over o Under?\n"
        f"2. ¿Qué línea? (8.5, 9.5, 10.5, 11.5, 12.5)\n\n"
        f"RÚBRICA DE PUNTUACIÓN (0-10 puntos). Evalúa con los datos proporcionados:\n\n"
        f"1. PROMEDIO CORNERS LOCAL (últimos 4):\n"
        f"   - >11 = 2 pts\n"
        f"   - 9-11 = 1 pt\n"
        f"   - <9 = 0 pts\n\n"
        f"2. PROMEDIO CORNERS VISITANTE (últimos 4):\n"
        f"   - >11 = 2 pts\n"
        f"   - 9-11 = 1 pt\n"
        f"   - <9 = 0 pts\n\n"
        f"3. H2H CORNERS (últimos 4):\n"
        f"   - >11 = 2 pts\n"
        f"   - 9-11 = 1 pt\n"
        f"   - <9 = 0 pts\n\n"
        f"4. ESTILO DE JUEGO (0-2 pts):\n"
        f"   - ¿Ambos equipos atacan por bandas / centran mucho?\n\n"
        f"5. NIVEL DE DATOS (0-2 pts):\n"
        f"   - ¿Tenemos ≥4 partidos con datos de corners? = 2 pts\n\n"
        f"REGLAS PARA DECIDIR LA LÍNEA:\n"
        f"- Si total ≥7 Y ambos promedios >11 → Over 11.5 (o 12.5 si es extremo)\n"
        f"- Si total ≥7 Y ambos promedios 9-11 → Over 10.5\n"
        f"- Si total 5-6 Y ambos promedios 9-10 → Over 9.5\n"
        f"- Si total 5-6 Y ambos promedios <9 → Under 8.5 o Under 9.5\n"
        f"- Si total <5 → responde 'NO HAY VALOR' y NO generes pick\n\n"
        f"REGLAS PARA STAKE (el stake DEBE ser igual a la puntuación):\n"
        f"- 5-6 pts → Stake 5-6\n"
        f"- 7-8 pts → Stake 7-8\n"
        f"- 9-10 pts → Stake 9-10 (excepcional, raro)\n\n"
        f"FORMATO OBLIGATORIO:\n"
        f"PREDICCION: [Over/Under X.5 corners]\n"
        f"VALOR: [cuota estimada con @]\n"
        f"STAKE: [1-10]\n"
        f"PUNTUACION: [X/10]\n"
        f"POR QUE: [2 líneas máximo con estadística CONCRETA]\n\n"
        f"Datos del partido:\n{data}"
    )

async def hoy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Analizando picks del día...")

    matches = get_matches(underground_only=False)
    if not matches:
        await update.message.reply_text("No hay partidos programados para hoy.")
        return

    # Filtro inteligente: partidos con IDs válidos, ordenados por hora
    valid_matches = []
    for m in matches:
        hid = m.get("teams", {}).get("home", {}).get("id", 0)
        aid = m.get("teams", {}).get("away", {}).get("id", 0)
        if hid and aid:
            valid_matches.append(m)

    if not valid_matches:
        await update.message.reply_text("No hay partidos con datos suficientes para hoy.")
        return

    fecha_hoy = get_today_str()
    min_stake = USER_CONFIG.get("min_stake", 0)
    min_odds = USER_CONFIG.get("min_odds", 0)
    max_odds = USER_CONFIG.get("max_odds", 999)

    picks_enviados = 0
    partidos_revisados = 0
    idx = 0

    # Revisar partidos hasta obtener 4 picks válidos o agotar la lista
    while picks_enviados < 4 and idx < len(valid_matches) and partidos_revisados < 8:
        m = valid_matches[idx]
        idx += 1
        partidos_revisados += 1

        h = m["teams"]["home"]["name"]
        a = m["teams"]["away"]["name"]
        hid = m["teams"]["home"]["id"]
        aid = m["teams"]["away"]["id"]
        lg = m["league"]["name"]
        is_ug = m["league"]["id"] not in LIGAS_TOP_IDS
        match_time = format_match_time(m)

        hf = get_team_form(hid)
        af = get_team_form(aid)
        hh = get_h2h(hid, aid)
        data = format_data(m, hf, af, hh)

        ug_tag = "🟢" if is_ug else "🔴"

        # === GOLES ===
        prompt_goles = build_prompt_goles(h, a, lg, data, ug_tag)
        pred_goles = get_gemini_response(prompt_goles)
        parsed_g = parse_prediccion(pred_goles, "goles")

        save_pick(fecha_hoy, "hoy", f"{h} vs {a}", lg, "goles", parsed_g["tipo"], parsed_g["linea"],
                  parsed_g["prediccion"], parsed_g["valor"], parsed_g["stake"], parsed_g["cuota"])

        # Validar pick de goles
        if pick_es_valido(parsed_g):
            cumple_odds = (parsed_g["cuota"] is None or (min_odds <= parsed_g["cuota"] <= max_odds))
            if parsed_g["stake"] >= min_stake and cumple_odds:
                msg = (
                    f"📅 PICK DEL DÍA\n"
                    f"{ug_tag} {h} vs {a}\n"
                    f"🏆 {lg}\n"
                    f"⏰ {match_time} (tu hora)\n\n"
                    f"⚽ {parsed_g['prediccion']}\n"
                    f"💰 {parsed_g['valor']}\n"
                    f"🎯 Stake: {parsed_g['stake']} | 📊 Rúbrica: {parsed_g['puntuacion']}/10\n\n"
                    f"📝 Análisis:\n{parsed_g['analisis']}\n\n"
                    f"⚠️ Cuota estimada por IA (no real). Verifica en tu casa de apuestas."
                )
                await update.message.reply_text(msg)
                picks_enviados += 1

        # Si ya tenemos 4 picks, no seguir con corners de este partido
        if picks_enviados >= 4:
            continue

        # === CORNERS ===
        prompt_corners = build_prompt_corners(h, a, lg, data, ug_tag)
        pred_corners = get_gemini_response(prompt_corners)
        parsed_c = parse_prediccion(pred_corners, "corners")

        save_pick(fecha_hoy, "hoy", f"{h} vs {a}", lg, "corners", parsed_c["tipo"], parsed_c["linea"],
                  parsed_c["prediccion"], parsed_c["valor"], parsed_c["stake"], parsed_c["cuota"])

        if pick_es_valido(parsed_c):
            cumple_odds_c = (parsed_c["cuota"] is None or (min_odds <= parsed_c["cuota"] <= max_odds))
            if parsed_c["stake"] >= min_stake and cumple_odds_c:
                msg = (
                    f"📅 PICK DEL DÍA\n"
                    f"{ug_tag} {h} vs {a}\n"
                    f"🏆 {lg}\n"
                    f"⏰ {match_time} (tu hora)\n\n"
                    f"🚩 {parsed_c['prediccion']}\n"
                    f"💰 {parsed_c['valor']}\n"
                    f"🎯 Stake: {parsed_c['stake']} | 📊 Rúbrica: {parsed_c['puntuacion']}/10\n\n"
                    f"📝 Análisis:\n{parsed_c['analisis']}\n\n"
                    f"⚠️ Cuota estimada por IA (no real). Verifica en tu casa de apuestas."
                )
                await update.message.reply_text(msg)
                picks_enviados += 1

    if picks_enviados == 0:
        await update.message.reply_text(
            f"📊 Ningún pick válido encontrado hoy.\n"
            f"Filtros: stake ≥ {min_stake} (mínimo recomendado: 5) | Cuotas {min_odds}-{max_odds}\n"
            f"Los partidos de hoy no tenían datos suficientes o valor real."
        )
    else:
        await update.message.reply_text(
            f"✅ {picks_enviados} picks enviados\n"
            f"💡 Filtros: stake ≥ {min_stake} | Cuotas {min_odds}-{max_odds}\n"
            f"🟢 = Liga menor | 🔴 = Liga top"
        )

async def live(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⚡ Analizando partidos en vivo...")

    matches = api_request("fixtures", {"live": "all", "timezone": USER_TIMEZONE})
    if not matches:
        await update.message.reply_text("No hay partidos en vivo.")
        return

    min_stake = USER_CONFIG.get("min_stake", 0)
    min_odds = USER_CONFIG.get("min_odds", 0)
    max_odds = USER_CONFIG.get("max_odds", 999)
    fecha_hoy = get_today_str()

    live_matches = [m for m in matches if m["fixture"]["status"]["elapsed"] and 30 <= m["fixture"]["status"]["elapsed"] <= 75]

    if not live_matches:
        await update.message.reply_text("No hay partidos en ventana óptima (30-75 min).")
        return

    selected = live_matches[:2]
    picks_enviados = 0

    for m in selected:
        h = m["teams"]["home"]["name"]
        a = m["teams"]["away"]["name"]
        hid = m["teams"]["home"]["id"]
        aid = m["teams"]["away"]["id"]
        lg = m["league"]["name"]
        mn = m["fixture"]["status"]["elapsed"]
        hg = m["goals"]["home"] if m["goals"]["home"] is not None else 0
        ag = m["goals"]["away"] if m["goals"]["away"] is not None else 0
        is_ug = m["league"]["id"] not in LIGAS_TOP_IDS

        hf = get_team_form(hid)
        af = get_team_form(aid)
        hh = get_h2h(hid, aid)
        data = format_data(m, hf, af, hh)

        ug_tag = "🟢" if is_ug else "🔴"

        prompt = (
            f"Eres un experto en LIVE BETTING. Partido va {hg}-{ag} al minuto {mn}. "
            f"Analiza SOLO el RESTO del partido. "
            f"NO uses frases genéricas. Basado en datos concretos del partido.\n\n"
            f"Partido: {h} vs {a} | Liga: {lg}\n"
            f"Marcador actual: {h} {hg}-{ag} {a} ({mn}')\n"
            f"Datos:\n{data}\n\n"
            f"RÚBRICA DE PUNTUACIÓN (0-10):\n"
            f"1. Momentum actual (0-2 pts): ¿Quién presiona más?\n"
            f"2. Estadísticas del 1T (0-2 pts): tiros, posesión, corners\n"
            f"3. H2H 2do tiempo (0-2 pts): ¿Suelen haber goles en 2T?\n"
            f"4. Necesidad del marcador (0-2 pts): ¿Alguien necesita empatar/ganar?\n"
            f"5. Edge de cuota en vivo (0-2 pts): ¿Hay valor en las cuotas live?\n\n"
            f"REGLAS: total <5 → 'NO HAY VALOR'. Stake DEBE ser igual a la puntuación.\n\n"
            f"FORMATO OBLIGATORIO:\n"
            f"PREDICCION: [ej: Over 1.5 goles 2do tiempo]\n"
            f"VALOR: [cuota estimada con @]\n"
            f"STAKE: [1-10]\n"
            f"PUNTUACION: [X/10]\n"
            f"POR QUE: [2 líneas máximo con estadística CONCRETA]"
        )

        pred_text = get_gemini_response(prompt)
        parsed = parse_prediccion(pred_text, "live")

        save_pick(fecha_hoy, "live", f"{h} vs {a}", lg, "live", parsed["tipo"], parsed["linea"],
                  parsed["prediccion"], parsed["valor"], parsed["stake"], parsed["cuota"])

        if pick_es_valido(parsed):
            cumple_odds = (parsed["cuota"] is None or (min_odds <= parsed["cuota"] <= max_odds))
            if parsed["stake"] >= min_stake and cumple_odds:
                msg = (
                    f"⚡ LIVE PICK\n"
                    f"{ug_tag} {h} {hg}-{ag} {a} ({mn}')\n"
                    f"🏆 {lg}\n\n"
                    f"🎯 {parsed['prediccion']}\n"
                    f"💰 {parsed['valor']}\n"
                    f"📊 Stake: {parsed['stake']} | Rúbrica: {parsed['puntuacion']}/10\n\n"
                    f"📝 Análisis:\n{parsed['analisis']}\n\n"
                    f"⚠️ Cuota estimada por IA (no real). Verifica en tu casa de apuestas."
                )
                await update.message.reply_text(msg)
                picks_enviados += 1

    if picks_enviados == 0:
        await update.message.reply_text(
            f"📊 Ningún live pick válido encontrado.\n"
            f"Filtros: stake ≥ {min_stake} | Cuotas {min_odds}-{max_odds}"
        )
    else:
        await update.message.reply_text(
            f"✅ {picks_enviados} live picks enviados\n"
            f"💡 Filtros: stake ≥ {min_stake} | Cuotas {min_odds}-{max_odds}\n"
            f"🟢 = Liga menor | 🔴 = Liga top"
        )

async def predecir(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 3:
        await update.message.reply_text("❌ Uso: /predecir EquipoA vs EquipoB")
        return

    texto = " ".join(context.args)
    if " vs " not in texto:
        await update.message.reply_text("❌ Usa 'vs'. Ej: /predecir Mexico vs England")
        return

    e1_raw, e2_raw = texto.split(" vs ", 1)
    e1 = e1_raw.strip()
    e2 = e2_raw.strip()
    e1_norm = normalize_text(e1)
    e2_norm = normalize_text(e2)

    await update.message.reply_text(f"🔍 Analizando {e1} vs {e2}...")

    # Buscar en los próximos 7 días, sin filtrar por status (cualquier estado)
    all_matches = []
    base_date = datetime.now(ZoneInfo(USER_TIMEZONE))
    for i in range(7):
        d = (base_date + timedelta(days=i)).strftime("%Y-%m-%d")
        all_matches.extend(get_matches(d, underground_only=False, status_filter=None))

    match = None
    for m in all_matches:
        hn = normalize_text(m["teams"]["home"]["name"])
        an = normalize_text(m["teams"]["away"]["name"])
        if (e1_norm in hn or e1_norm in an or e2_norm in hn or e2_norm in an):
            # Verificar que ambos equipos estén presentes
            if (e1_norm in hn or e1_norm in an) and (e2_norm in hn or e2_norm in an):
                match = m
                break

    if match:
        h = match["teams"]["home"]["name"]
        a = match["teams"]["away"]["name"]
        hid = match["teams"]["home"]["id"]
        aid = match["teams"]["away"]["id"]
        lg = match["league"]["name"]
        is_ug = match["league"]["id"] not in LIGAS_TOP_IDS
        match_time = format_match_time(match)
        hf = get_team_form(hid)
        af = get_team_form(aid)
        hh = get_h2h(hid, aid)
        data = format_data(m, hf, af, hh)

        ug_tag = "🟢 UNDERGROUND" if is_ug else "🔴 LIGA TOP"

        # Detectar si hay pocos datos (forma vacía o muy corta)
        datos_insuficientes = len(hf) < 2 and len(af) < 2 and len(hh) < 2

        if datos_insuficientes:
            # Prompt para torneos cortos con datos limitados
            prompt = (
                f"Eres un analista de apuestas deportivas con experiencia en torneos internacionales. "
                f"Este partido es de un torneo corto (Mundial, Copa América, etc.) donde los datos recientes de forma son limitados o inexistentes. "
                f"Haz el MEJOR ANÁLISIS POSIBLE con la información disponible, usando tu conocimiento general del modelo.\n\n"
                f"REGLAS ESTRICTAS:\n"
                f"1. NO inventes estadísticas que no estén en los datos proporcionados.\n"
                f"2. NO inventes lesiones, alineaciones o clima actual. Tu conocimiento se corta en enero 2025.\n"
                f"3. SI puedes usar: estilos de juego históricos, fama de equipos, patrones generales de torneos.\n"
                f"4. SI los datos de forma están vacíos, indícalo claramente y baja el stake.\n"
                f"5. Stake honesto basado en CONFIANZA del análisis:\n"
                f"   - 1-3: Muy incierto, casi sin datos\n"
                f"   - 4-5: Análisis moderado, pocos datos pero patrones claros\n"
                f"   - 6-7: Buena confianza, algunos datos o conocimiento sólido\n"
                f"   - 8-10: Muy raro sin datos recientes\n\n"
                f"Partido: {h} vs {a} | Torneo: {lg}\n"
                f"Hora: {match_time}\n"
                f"Datos disponibles:\n{data}\n\n"
                f"FORMATO OBLIGATORIO:\n"
                f"1X2: [prediccion]\n"
                f"GOLES: [Over/Under X.5]\n"
                f"CORNERS: [Over/Under X.5]\n"
                f"MEJOR VALOR: [apuesta con más valor y cuota estimada con @]\n"
                f"STAKE: [1-10]\n"
                f"PUNTUACION: [X/10]\n"
                f"POR QUE: [2 líneas con tu razonamiento, indicando qué datos usaste y qué limitaciones tuviste]\n"
                f"ADVERTENCIA DATOS: [Alta/Media/Baja] - [explicación breve]"
            )
        else:
            prompt = (
                f"Eres un experto en análisis de apuestas deportivas. "
                f"Analiza TODOS los mercados para este partido específico. "
                f"NO uses frases genéricas. Usa las estadísticas concretas proporcionadas.\n\n"
                f"Partido: {h} vs {a} | Liga: {lg}\n"
                f"Hora: {match_time}\n"
                f"Datos:\n{data}\n\n"
                f"RÚBRICA DE PUNTUACIÓN (0-10) por mercado:\n"
                f"1. Consistencia local (0-2 pts): ¿Cumple la línea en ≥60% de últimos 4?\n"
                f"2. Consistencia visitante (0-2 pts): Igual\n"
                f"3. H2H reciente (0-2 pts): ¿Cumple en ≥3/4 últimos?\n"
                f"4. Tendencia de liga (0-1 pt)\n"
                f"5. Edge de cuota (0-2 pts): ≥1.70 = 2 pts\n"
                f"6. Nivel de datos (0-1 pt): ≥4 partidos = 1 pt\n\n"
                f"REGLAS: total <5 → 'NO HAY VALOR' para ese mercado. Stake DEBE ser igual a la puntuación.\n\n"
                f"FORMATO OBLIGATORIO:\n"
                f"1X2: [prediccion]\n"
                f"GOLES: [Over/Under X.5]\n"
                f"CORNERS: [Over/Under X.5]\n"
                f"MEJOR VALOR: [apuesta con más valor y cuota estimada con @]\n"
                f"STAKE: [1-10]\n"
                f"PUNTUACION: [X/10]\n"
                f"POR QUE: [2 líneas máximo con estadística CONCRETA]"
            )

        pred_text = get_gemini_response(prompt)
        parsed = parse_prediccion(pred_text, "mix")

        # Para /predecir, aceptar cualquier stake >= 1 si hay predicción y valor
        tiene_datos = parsed["stake"] >= 1 and parsed["prediccion"] and parsed["valor"]

        save_pick(get_today_str(), "predecir", f"{h} vs {a}", lg, "mix", parsed["tipo"], parsed["linea"],
                  parsed["prediccion"], parsed["valor"], parsed["stake"], parsed["cuota"])

        if tiene_datos:
            confianza_tag = "🔴 Baja" if datos_insuficientes else "🟢 Buena"
            msg = (
                f"🔮 {e1} vs {e2}\n"
                f"🏆 {lg}\n"
                f"{ug_tag} | ⏰ {match_time}\n"
                f"📊 Confianza en datos: {confianza_tag}\n\n"
                f"{pred_text}\n\n"
                f"⚠️ Cuotas estimadas por IA (no reales). Verifica en tu casa de apuestas."
            )
            await update.message.reply_text(msg)
        else:
            await update.message.reply_text(
                f"🔮 {e1} vs {e2}\n"
                f"🏆 {lg}\n\n"
                f"{pred_text}\n\n"
                f"⚠️ Cuotas estimadas por IA (no reales)."
            )
    else:
        await update.message.reply_text(f"❌ No encontré {e1} vs {e2} en los próximos 7 días. Verifica los nombres.")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    min_stake = USER_CONFIG.get("min_stake", 0)
    min_odds = USER_CONFIG.get("min_odds", 0)
    max_odds = USER_CONFIG.get("max_odds", 999)

    stats_data, por_stake = get_stats(min_stake, min_odds, max_odds)

    total, ganados, perdidos, pendientes, avg_stake, winrate = stats_data

    msg = f"📊 TU RENDIMIENTO\n"
    msg += f"Filtros: stake ≥ {min_stake} | Cuotas {min_odds}-{max_odds}\n\n"
    msg += f"📋 Total picks: {total}\n"
    msg += f"⏳ Pendientes: {pendientes}\n"
    msg += f"✅ Ganados: {ganados}\n"
    msg += f"❌ Perdidos: {perdidos}\n"

    if ganados + perdidos > 0:
        msg += f"📈 Winrate: {winrate}%\n"
        msg += f"🎯 Stake promedio: {avg_stake}\n"

    if por_stake:
        msg += f"\n📊 POR STAKE:\n"
        for rango, r_total, r_ganados, r_perdidos, r_winrate in por_stake:
            msg += f"Stake {rango}: {r_total}p → {r_winrate}%✅\n"

    await update.message.reply_text(msg)

# ============ MAIN ============
async def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("hoy", hoy))
    app.add_handler(CommandHandler("live", live))
    app.add_handler(CommandHandler("predecir", predecir))
    app.add_handler(CommandHandler("stake", stake))
    app.add_handler(CommandHandler("odds", odds))
    app.add_handler(CommandHandler("stats", stats))

    job_queue = app.job_queue
    if job_queue:
        job_queue.run_repeating(auto_actualizar, interval=3600, first=60)
        print("[JobQueue] Auto-actualización cada 60 min activada")
    else:
        print("[JobQueue] No disponible")

    print("Bot iniciado!")
    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    asyncio.run(main())
