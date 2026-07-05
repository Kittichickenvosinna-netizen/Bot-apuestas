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
DB_PATH = "picks.db"

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
    if pick_exists(fecha, partido, mercado):
        return False
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO picks (fecha, comando, partido, liga, mercado, tipo, linea, prediccion, valor, stake, cuota_estimada)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (fecha, comando, partido, liga, mercado, tipo, linea, prediccion, valor, stake, cuota_estimada))
    conn.commit()
    conn.close()
    return True

def pick_exists(fecha, partido, mercado):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id FROM picks WHERE fecha = ? AND partido = ? AND mercado = ?", 
              (fecha, partido, mercado))
    exists = c.fetchone() is not None
    conn.close()
    return exists

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

# ============ FUNCIONES AUXILIARES ============

def normalize_text(text):
    """Quita tildes y pasa a mayusculas para comparacion flexible"""
    text = text.upper()
    replacements = {'A': 'A', 'E': 'E', 'I': 'I', 'O': 'O', 'U': 'U', 'U': 'U', 'N': 'N'}
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text

def extract_field(text, keywords):
    """Busca una linea que contenga alguna keyword y devuelve el valor despues de ':'"""
    for line in text.split('\n'):
        norm_line = normalize_text(line)
        for kw in keywords:
            if kw in norm_line and ':' in line:
                return line.split(':', 1)[1].strip()
    return ""

def extract_analysis(text):
    """Extrae el analisis de forma robusta"""
    keywords = ['POR QUE', 'PORQUE', 'RAZON', 'RAZON', 'JUSTIFICACION', 'JUSTIFICACION', 'ANALISIS', 'ANALISIS', 'MOTIVO', 'EXPLICACION', 'EXPLICACION']
    for line in text.split('\n'):
        norm_line = normalize_text(line)
        if any(kw in norm_line for kw in keywords):
            if ':' in line:
                return line.split(':', 1)[1].strip()
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    return '\n'.join(lines[-2:]) if len(lines) >= 2 else text.strip()

def safe_get_goals(match, side):
    """Extrae goles de forma segura"""
    try:
        goals = match.get("goals", {})
        if goals is None:
            return 0
        val = goals.get(side)
        return val if val is not None else 0
    except:
        return 0

def teams_match(name1, name2, home_name, away_name):
    """Comparacion robusta de nombres de equipos"""
    n1 = name1.lower().strip()
    n2 = name2.lower().strip()
    h = home_name.lower().strip()
    a = away_name.lower().strip()
    return ((n1 in h or h in n1) and (n2 in a or a in n2)) or ((n1 in a or a in n1) and (n2 in h or h in n2))

# ============ FUNCIONES API ============

def get_gemini_response(prompt: str) -> str:
    headers = {"Content-Type": "application/json"}
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    url = f"{GEMINI_URL}?key={GEMINI_API_KEY}"
    try:
        response = requests.post(url, headers=headers, json=payload, timeout=30)
        data = response.json()
        if "candidates" in data and len(data["candidates"]) > 0:
            return data["candidates"][0]["content"]["parts"][0]["text"]
        return ""
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
        hg_str = str(m.get("goals", {}).get("home", "-")) if m.get("goals") else "-"
        ag_str = str(m.get("goals", {}).get("away", "-")) if m.get("goals") else "-"
        lines.append(f"{i}. {h} {hg_str}-{ag_str} {a}")

    lines.append("")
    lines.append("FORMA " + away + ":")

    for i, m in enumerate(away_form[:5], 1):
        h = m["teams"]["home"]["name"]
        a = m["teams"]["away"]["name"]
        hg_str = str(m.get("goals", {}).get("home", "-")) if m.get("goals") else "-"
        ag_str = str(m.get("goals", {}).get("away", "-")) if m.get("goals") else "-"
        lines.append(f"{i}. {h} {hg_str}-{ag_str} {a}")

    lines.append("")
    lines.append("H2H:")

    for i, m in enumerate(h2h[:5], 1):
        h = m["teams"]["home"]["name"]
        a = m["teams"]["away"]["name"]
        hg_str = str(m.get("goals", {}).get("home", "-")) if m.get("goals") else "-"
        ag_str = str(m.get("goals", {}).get("away", "-")) if m.get("goals") else "-"
        lines.append(f"{i}. {h} {hg_str}-{ag_str} {a}")

    return "\n".join(lines)

# ============ PARSEO ROBUSTO ============

def parse_prediccion(texto: str, mercado_default: str):
    resultado = {
        "prediccion": "",
        "mercado": mercado_default,
        "tipo": "",
        "linea": 0.0,
        "valor": "",
        "stake": 5,
        "cuota": None
    }

    if not texto or texto.startswith("Error:"):
        return resultado

    pred = extract_field(texto, ['PREDICC', 'PREDICTION', 'PRONOSTICO', 'PRONOSTICO', 'MEJOR VALOR', 'PICK'])
    if pred:
        resultado["prediccion"] = pred
        pred_lower = pred.lower()
        if "over" in pred_lower:
            resultado["tipo"] = "over"
        elif "under" in pred_lower:
            resultado["tipo"] = "under"
        elif "si" in pred_lower or "yes" in pred_lower:
            resultado["tipo"] = "si"
        elif "no" in pred_lower:
            resultado["tipo"] = "no"

        nums = re.findall(r'\d+\.\d+|\d+', pred)
        if nums:
            try:
                resultado["linea"] = float(nums[0])
            except:
                pass

    val = extract_field(texto, ['VALOR', 'VALUE', 'APUESTA', 'RECOMENDACION', 'RECOMENDACION', 'PICK'])
    if val:
        resultado["valor"] = val
        cuota_match = re.search(r'@\s*([\d.]+)', val)
        if cuota_match:
            try:
                resultado["cuota"] = float(cuota_match.group(1))
            except:
                pass

    if not resultado["valor"] and resultado["prediccion"]:
        resultado["valor"] = resultado["prediccion"]

    stake_text = extract_field(texto, ['STAKE', 'CONFIANZA', 'UNIDADES', 'UNITS'])
    if stake_text:
        nums = re.findall(r'\d+', stake_text)
        if nums:
            try:
                s = int(nums[0])
                if 1 <= s <= 10:
                    resultado["stake"] = s
            except:
                pass

    if resultado["cuota"] is None:
        cuota_text = extract_field(texto, ['CUOTA', 'ODDS', 'ODD', 'COTIZA'])
        if cuota_text:
            nums = re.findall(r'\d+\.\d+|\d+', cuota_text)
            if nums:
                try:
                    resultado["cuota"] = float(nums[0])
                except:
                    pass

    return resultado

def evaluar_resultado(mercado: str, tipo: str, linea: float, goles_home: int, goles_away: int):
    if mercado == "goles" or mercado == "live":
        total = goles_home + goles_away
        if tipo == "over":
            return "ganado" if total > linea else "perdido"
        elif tipo == "under":
            return "ganado" if total < linea else "perdido"
    elif mercado == "corners":
        return "pendiente"
    return "pendiente"

# ============ AUTO ACTUALIZAR ============

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

        today = datetime.now().strftime("%Y-%m-%d")
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

        found = False
        for d in [yesterday, today]:
            matches = api_request("fixtures", {"date": d, "timezone": "America/Caracas"})
            for m in matches:
                hn = m["teams"]["home"]["name"]
                an = m["teams"]["away"]["name"]
                if teams_match(eq1, eq2, hn, an):
                    status = m["fixture"]["status"]["short"]
                    if status in ("FT", "AET", "PEN"):
                        gh = safe_get_goals(m, "home")
                        ga = safe_get_goals(m, "away")

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

async def hoy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cada pick en mensaje separado con analisis"""
    await update.message.reply_text("🔍 Analizando picks del día...")

    matches = get_matches(underground_only=False)
    if not matches:
        await update.message.reply_text("No hay partidos hoy.")
        return

    fecha_hoy = datetime.now().strftime("%Y-%m-%d")
    min_stake = USER_CONFIG.get("min_stake", 0)
    min_odds = USER_CONFIG.get("min_odds", 0)
    max_odds = USER_CONFIG.get("max_odds", 999)

    selected = matches[:3]
    picks_enviados = 0

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

        ug_tag = "🟢" if is_ug else "🔴"

        prompt_goles = (
            f"{ug_tag} Eres experto en OVER/UNDER GOLES. REGLAS ESTRICTAS:\n"
            f"1. Stake alto (8-10) SOLO si hay valor REAL y datos sólidos\n"
            f"2. Cuotas altas (>2.0) con stake alto solo si el edge es evidente\n"
            f"3. Si no hay datos suficientes, stake bajo (3-5) o di 'No hay valor'\n\n"
            f"Analiza y responde EXACTAMENTE en este formato (max 400 chars):\n\n"
            + data
            + "\n\nFORMATO OBLIGATORIO:\n"
            "PREDICCION: [Over/Under X.5 goles]\n"
            "VALOR: [apuesta con mas valor y cuota estimada con @]\n"
            "STAKE: [1-10]\n"
            "POR QUE: [2 lineas maximo con estadistica concreta]"
        )

        pred_goles = get_gemini_response(prompt_goles)

        if not pred_goles or pred_goles.startswith("Error:"):
            continue

        parsed_g = parse_prediccion(pred_goles, "goles")

        if parsed_g["prediccion"] and parsed_g["prediccion"].lower() not in ["no hay valor", "", "ninguna"]:
            save_pick(fecha_hoy, "hoy", f"{h} vs {a}", lg, "goles", parsed_g["tipo"], parsed_g["linea"], 
                      parsed_g["prediccion"], parsed_g["valor"], parsed_g["stake"], parsed_g["cuota"])

            cumple_g = (parsed_g["stake"] >= min_stake and 
                        (parsed_g["cuota"] is None or (min_odds <= parsed_g["cuota"] <= max_odds)))
            if cumple_g:
                analisis = extract_analysis(pred_goles)
                msg = (
                    f"📅 PICK DEL DÍA\n"
                    f"{ug_tag} {h} vs {a}\n"
                    f"🏆 {lg}\n\n"
                    f"⚽ {parsed_g['prediccion']}\n"
                    f"💰 {parsed_g['valor']}\n"
                    f"🎯 Stake: {parsed_g['stake']}\n\n"
                    f"📝 Análisis:\n{analisis}"
                )
                await update.message.reply_text(msg)
                picks_enviados += 1

        prompt_corners = (
            f"{ug_tag} Eres experto en CORNERS. REGLAS ESTRICTAS:\n"
            f"1. Stake alto (8-10) SOLO si hay valor REAL y datos sólidos\n"
            f"2. Cuotas altas (>2.0) con stake alto solo si el edge es evidente\n"
            f"3. Si no hay datos suficientes, stake bajo (3-5) o di 'No hay valor'\n\n"
            f"Analiza y responde EXACTAMENTE en este formato (max 400 chars):\n\n"
            + data
            + "\n\nFORMATO OBLIGATORIO:\n"
            "PREDICCION: [Over/Under X.5 corners]\n"
            "VALOR: [apuesta con mas valor y cuota estimada con @]\n"
            "STAKE: [1-10]\n"
            "POR QUE: [2 lineas maximo con estadistica concreta]"
        )

        pred_corners = get_gemini_response(prompt_corners)

        if not pred_corners or pred_corners.startswith("Error:"):
            continue

        parsed_c = parse_prediccion(pred_corners, "corners")

        if parsed_c["prediccion"] and parsed_c["prediccion"].lower() not in ["no hay valor", "", "ninguna"]:
            save_pick(fecha_hoy, "hoy", f"{h} vs {a}", lg, "corners", parsed_c["tipo"], parsed_c["linea"],
                      parsed_c["prediccion"], parsed_c["valor"], parsed_c["stake"], parsed_c["cuota"])

            cumple_c = (parsed_c["stake"] >= min_stake and
                        (parsed_c["cuota"] is None or (min_odds <= parsed_c["cuota"] <= max_odds)))
            if cumple_c:
                analisis = extract_analysis(pred_corners)
                msg = (
                    f"📅 PICK DEL DÍA\n"
                    f"{ug_tag} {h} vs {a}\n"
                    f"🏆 {lg}\n\n"
                    f"🚩 {parsed_c['prediccion']}\n"
                    f"💰 {parsed_c['valor']}\n"
                    f"🎯 Stake: {parsed_c['stake']}\n\n"
                    f"📝 Análisis:\n{analisis}"
                )
                await update.message.reply_text(msg)
                picks_enviados += 1

    if picks_enviados == 0:
        await update.message.reply_text(
            f"📊 Ningún pick cumple filtros:\n"
            f"Stake ≥ {min_stake} | Cuotas {min_odds}-{max_odds}\n"
            f"Usa /stake 0 y /odds 0,999 para ver todos."
        )
    else:
        await update.message.reply_text(
            f"✅ {picks_enviados} picks enviados\n"
            f"💡 Filtros: stake ≥ {min_stake} | Cuotas {min_odds}-{max_odds}\n"
            f"🟢 = Liga menor | 🔴 = Liga top"
        )

async def live(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Picks en vivo - cada uno en mensaje separado con prediccion real"""
    await update.message.reply_text("⚡ Analizando partidos en vivo...")

    matches = api_request("fixtures", {"live": "all", "timezone": "America/Caracas"})
    if not matches:
        await update.message.reply_text("No hay partidos en vivo.")
        return

    min_stake = USER_CONFIG.get("min_stake", 0)
    min_odds = USER_CONFIG.get("min_odds", 0)
    max_odds = USER_CONFIG.get("max_odds", 999)
    fecha_hoy = datetime.now().strftime("%Y-%m-%d")

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
        hg = safe_get_goals(m, "home")
        ag = safe_get_goals(m, "away")
        is_ug = m["league"]["id"] not in LIGAS_TOP_IDS

        hf = get_team_form(hid)
        af = get_team_form(aid)
        hh = get_h2h(hid, aid)
        data = format_data(m, hf, af, hh)

        ug_tag = "🟢" if is_ug else "🔴"

        prompt = (
            f"{ug_tag} LIVE BETTING. Partido va {hg}-{ag} al minuto {mn}. "
            f"REGLAS ESTRICTAS:\n"
            f"1. Stake alto (8-10) SOLO si hay valor REAL y datos sólidos\n"
            f"2. Cuotas altas (>2.0) con stake alto solo si el edge es evidente\n"
            f"3. Si no hay datos suficientes, stake bajo (3-5) o di 'No hay valor'\n\n"
            f"Analiza y da picks para el RESTO del partido. "
            f"Responde EXACTAMENTE en este formato (max 400 chars):\n\n"
            + data
            + f"\n\nMARCADOR: {h} {hg}-{ag} {a} ({mn}')\n"
            + "\nFORMATO OBLIGATORIO:\n"
            "PREDICCION: [ej: Over 1.5 goles 2do tiempo]\n"
            "VALOR: [apuesta con mas valor y cuota estimada con @]\n"
            "STAKE: [1-10]\n"
            "POR QUE: [2 lineas maximo con estadistica concreta]"
        )

        pred_text = get_gemini_response(prompt)

        if not pred_text or pred_text.startswith("Error:"):
            continue

        parsed = parse_prediccion(pred_text, "live")

        if parsed["prediccion"] and parsed["prediccion"].lower() not in ["no hay valor", "", "ninguna"]:
            save_pick(fecha_hoy, "live", f"{h} vs {a}", lg, "live", parsed["tipo"], parsed["linea"],
                      parsed["prediccion"], parsed["valor"], parsed["stake"], parsed["cuota"])

            cumple = (parsed["stake"] >= min_stake and
                      (parsed["cuota"] is None or (min_odds <= parsed["cuota"] <= max_odds)))
            if cumple:
                analisis = extract_analysis(pred_text)
                msg = (
                    f"⚡ LIVE PICK\n"
                    f"{ug_tag} {h} {hg}-{ag} {a} ({mn}')\n"
                    f"🏆 {lg}\n\n"
                    f"🎯 {parsed['prediccion']}\n"
                    f"💰 {parsed['valor']}\n"
                    f"📊 Stake: {parsed['stake']}\n\n"
                    f"📝 Análisis:\n{analisis}"
                )
                await update.message.reply_text(msg)
                picks_enviados += 1

    if picks_enviados == 0:
        await update.message.reply_text(
            f"📊 Ningún live pick cumple filtros:\n"
            f"Stake ≥ {min_stake} | Cuotas {min_odds}-{max_odds}"
        )
    else:
        await update.message.reply_text(
            f"✅ {picks_enviados} live picks enviados\n"
            f"💡 Filtros: stake ≥ {min_stake} | Cuotas {min_odds}-{max_odds}\n"
            f"🟢 = Liga menor | 🔴 = Liga top"
        )

async def predecir(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = " ".join(context.args)
    if " vs " not in texto:
        await update.message.reply_text("❌ Uso: /predecir Equipo A vs Equipo B")
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
        hn = m["teams"]["home"]["name"]
        an = m["teams"]["away"]["name"]
        if teams_match(e1, e2, hn, an):
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

        ug_tag = "🟢 UNDERGROUND" if is_ug else "🔴 LIGA TOP"

        prompt = (
            f"{ug_tag} | Eres experto en VALOR. REGLAS ESTRICTAS:\n"
            f"1. Stake alto (8-10) SOLO si hay valor REAL y datos sólidos\n"
            f"2. Cuotas altas (>2.0) con stake alto solo si el edge es evidente\n"
            f"3. Si no hay datos suficientes, stake bajo (3-5) o di 'No hay valor'\n\n"
            f"Analiza y responde EXACTAMENTE en este formato (max 600 chars):\n\n"
            + data
            + "\n\nFORMATO OBLIGATORIO:\n"
            "1X2: [prediccion]\n"
            "GOLES: [Over/Under X.5]\n"
            "CORNERS: [Over/Under X.5]\n"
            "MEJOR VALOR: [apuesta con mas valor y cuota estimada con @]\n"
            "STAKE: [1-10]\n"
            "POR QUE: [2 lineas maximo con estadistica concreta]"
        )

        pred_text = get_gemini_response(prompt)

        if not pred_text or pred_text.startswith("Error:"):
            await update.message.reply_text("❌ Error al consultar Gemini. Intenta de nuevo.")
            return

        parsed = parse_prediccion(pred_text, "mix")

        if not parsed["prediccion"]:
            parsed["prediccion"] = extract_field(pred_text, ['MEJOR VALOR', '1X2', 'GOLES', 'CORNERS'])
        if not parsed["valor"]:
            parsed["valor"] = extract_field(pred_text, ['MEJOR VALOR', 'VALOR', 'APUESTA'])

        save_pick(today, "predecir", f"{h} vs {a}", lg, "mix", parsed["tipo"], parsed["linea"],
                  parsed["prediccion"], parsed["valor"], parsed["stake"], parsed["cuota"])

        await update.message.reply_text(f"🔮 {e1} vs {e2}\n🏆 {lg}\n{ug_tag}\n\n{pred_text}")
    else:
        await update.message.reply_text(f"❌ No encontré {e1} vs {e2}.")

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
def main():
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
    app.run_polling()

if __name__ == "__main__":
    main()
