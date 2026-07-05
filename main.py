import os
import asyncio
import threading
import requests
import json
import sqlite3
import re
import unicodedata
import time
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
                WHEN stake >= 4 THEN '4-5'
                WHEN stake >= 2 THEN '2-3'
                ELSE '1'
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
USER_CONFIG = {"min_stake": 1, "min_odds": 0.0, "max_odds": 999.0}

# ============ FUNCIONES ============

def get_gemini_response(prompt: str, max_retries: int = 2) -> str:
    headers = {"Content-Type": "application/json"}
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    url = f"{GEMINI_URL}?key={GEMINI_API_KEY}"

    for attempt in range(max_retries):
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=30)
            data = response.json()

            if "error" in data:
                error_msg = data["error"].get("message", "Error desconocido de Gemini")
                print(f"[Gemini Error] Intento {attempt+1}/{max_retries}: {error_msg}")
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                return f"Error Gemini: {error_msg}"

            if "candidates" in data and len(data["candidates"]) > 0:
                text = data["candidates"][0]["content"]["parts"][0]["text"]
                if text and text.strip():
                    return text
                return "Gemini devolvió respuesta vacía."

            print(f"[Gemini] Intento {attempt+1}: Sin candidatos. Respuesta: {json.dumps(data)[:200]}")
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
                continue
            return "Gemini no pudo generar respuesta (sin candidatos)."

        except requests.exceptions.Timeout:
            print(f"[Gemini Timeout] Intento {attempt+1}/{max_retries}")
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
                continue
            return "Error: Gemini timeout después de 2 reintentos."

        except Exception as e:
            print(f"[Gemini Exception] Intento {attempt+1}/{max_retries}: {str(e)}")
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
                continue
            return f"Error: {str(e)}"

    return "Gemini no pudo generar respuesta después de reintentos."

def api_request(endpoint: str, params: dict = None):
    headers = {"x-apisports-key": FOOTBALL_API_KEY}
    try:
        r = requests.get(f"{FOOTBALL_URL}/{endpoint}", headers=headers, params=params, timeout=15)
        return r.json().get("response", [])
    except:
        return []

def get_today_str():
    return datetime.now(ZoneInfo(USER_TIMEZONE)).strftime("%Y-%m-%d")

def get_matches(date_str: str = None, underground_only: bool = False, status_filter: tuple = ("NS", "TBD")):
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
        "analisis": ""
    }

    if "NO HAY VALOR" in texto.upper():
        resultado["stake"] = 0
        return resultado

    lines = texto.split("\n")
    for line in lines:
        line = line.strip()
        if not line:
            continue

        if line.upper().startswith("PREDICCION:"):
            resultado["prediccion"] = line.split(":", 1)[1].strip()
            pred_lower = resultado["prediccion"].lower()
            if "corner" in pred_lower or "corners" in pred_lower:
                resultado["mercado"] = "corners"
            elif "gol" in pred_lower or "goles" in pred_lower or "over" in pred_lower or "under" in pred_lower:
                resultado["mercado"] = "goles"

            if "over" in pred_lower:
                resultado["tipo"] = "over"
            elif "under" in pred_lower:
                resultado["tipo"] = "under"
            elif "si" in pred_lower or "yes" in pred_lower:
                resultado["tipo"] = "si"
            elif "no" in pred_lower:
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

        elif line.upper().startswith("POR QUE:"):
            resultado["analisis"] = line.split(":", 1)[1].strip()

    return resultado

def pick_es_valido(parsed, min_stake):
    """Valida que un pick tenga datos minimos para mostrarse"""
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
        "Sistema de unidades profesional (1-5):\n"
        "• 1-2u = Pick estándar\n"
        "• 3u = Buen valor\n"
        "• 4-5u = Máxima confianza (raro)\n\n"
        "/hoy - Picks del día (mejor mercado)\n"
        "/live - Picks en vivo ahora\n"
        "/predecir [A] vs [B] - Análisis de un partido\n"
        "/stake [1-5] - Filtro de unidades mínimas\n"
        "/odds [min,max] - Filtro de rango de cuotas\n"
        "/stats - Tu rendimiento"
    )
    await update.effective_message.reply_text(msg)

async def stake(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) == 1:
        try:
            valor = int(context.args[0])
            if 1 <= valor <= 5:
                USER_CONFIG["min_stake"] = valor
                await update.effective_message.reply_text(f"✅ Unidades mínimas: {valor}")
            else:
                await update.effective_message.reply_text("❌ Unidades entre 1 y 5")
        except:
            await update.effective_message.reply_text("❌ Uso: /stake 3")
    else:
        await update.effective_message.reply_text(f"⚙️ Unidades mínimas: {USER_CONFIG['min_stake']}\nUso: /stake 3")

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
                await update.effective_message.reply_text(f"✅ Cuotas: {min_odds} - {max_odds}")
            else:
                await update.effective_message.reply_text("❌ Uso: /odds 1.5,3.0")
        except:
            await update.effective_message.reply_text("❌ Uso: /odds 1.5,3.0")
    else:
        await update.effective_message.reply_text(
            f"⚙️ Cuotas: {USER_CONFIG['min_odds']} - {USER_CONFIG['max_odds']}\n"
            f"Uso: /odds 1.5,3.0"
        )

def build_prompt_hoy(h, a, lg, data, ug_tag):
    """Prompt con sistema de unidades profesional (1-5)"""
    return (
        f"Eres un tipster profesional de apuestas deportivas. Tu trabajo es encontrar valor real en el mercado de GOLES o CORNERS.\n\n"
        f"Partido: {h} vs {a} | Liga: {lg} {ug_tag}\n"
        f"Datos:\n{data}\n\n"
        f"=== CRITERIOS PARA ASIGNAR UNIDADES ===\n"
        f"Evalúa estos 4 factores y asigna unidades según la tabla:\n\n"
        f"1. CONFIANZA EN DATOS (0-1.5u):\n"
        f"   - ¿Tenemos ≥4 partidos recientes de ambos equipos? → +1.5u\n"
        f"   - ¿Tenemos 2-3 partidos? → +0.5u\n"
        f"   - ¿Muy pocos datos? → +0u\n\n"
        f"2. CONSISTENCIA ESTADÍSTICA (0-1.5u):\n"
        f"   - ¿Todos los indicadores apuntan en la MISMA dirección? (ej: forma, H2H, tendencia de liga) → +1.5u\n"
        f"   - ¿Mayoría apunta en la misma dirección? → +1u\n"
        f"   - ¿Datos contradictorios? → +0u\n\n"
        f"3. EDGE DE VALOR (0-1.5u):\n"
        f"   - ¿La cuota estimada es claramente superior a lo que dictan las stats? → +1.5u\n"
        f"   - ¿Hay valor moderado? → +1u\n"
        f"   - ¿Valor marginal o dudoso? → +0u\n\n"
        f"4. CUOTA Y VOLATILIDAD (0-1.5u):\n"
        f"   - Cuota 1.70-2.20 + datos sólidos → +1.5u\n"
        f"   - Cuota 1.50-1.69 o 2.21-3.00 → +1u\n"
        f"   - Cuota <1.50 o >3.00 → +0u (muy riesgoso o poco rentable)\n\n"
        f"=== TABLA DE UNIDADES ===\n"
        f"Suma los 4 factores y redondea al entero más cercano:\n"
        f"• 1u = Valor marginal. Apuesta estándar. (≈1% bankroll)\n"
        f"• 2u = Valor decente. Confianza moderada. (≈2% bankroll)\n"
        f"• 3u = Buen valor. Confianza alta. (≈3% bankroll)\n"
        f"• 4u = Gran valor. Confianza muy alta. (≈4% bankroll)\n"
        f"• 5u = Valor excepcional. Máxima confianza. (≈5% bankroll)\n\n"
        f"REGLAS:\n"
        f"- Si suma <1u → responde EXACTAMENTE: 'NO HAY VALOR'\n"
        f"- Si suma ≥1u → genera pick con las unidades redondeadas\n"
        f"- Elige el mercado (goles o corners) con MAYOR suma de unidades\n"
        f"- NO inventes estadísticas. Usa solo los datos proporcionados.\n\n"
        f"=== FORMATO OBLIGATORIO ===\n"
        f"PREDICCION: [Over/Under X.5 goles O Over/Under X.5 corners]\n"
        f"VALOR: [cuota estimada con @]\n"
        f"STAKE: [1-5 unidades]\n"
        f"POR QUE: [2 líneas máximo con estadística CONCRETA del dato que más peso tuvo]"
    )

async def hoy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("🔍 Analizando picks del día...")

    matches = get_matches(underground_only=False)
    if not matches:
        await update.effective_message.reply_text("No hay partidos programados para hoy.")
        return

    valid_matches = []
    for m in matches:
        hid = m.get("teams", {}).get("home", {}).get("id", 0)
        aid = m.get("teams", {}).get("away", {}).get("id", 0)
        if hid and aid:
            valid_matches.append(m)

    if not valid_matches:
        await update.effective_message.reply_text("No hay partidos con datos suficientes para hoy.")
        return

    # PRIORIZAR LIGAS TOP primero, luego por hora
    def sort_key(match):
        league_id = match.get("league", {}).get("id", 0)
        is_top = 0 if league_id in LIGAS_TOP_IDS else 1
        timestamp = match.get("fixture", {}).get("timestamp", 0)
        return (is_top, timestamp)

    valid_matches.sort(key=sort_key)

    fecha_hoy = get_today_str()
    min_stake = USER_CONFIG.get("min_stake", 1)
    min_odds = USER_CONFIG.get("min_odds", 0)
    max_odds = USER_CONFIG.get("max_odds", 999)

    picks_enviados = 0
    partidos_revisados = 0
    idx = 0

    while picks_enviados < 4 and idx < len(valid_matches) and partidos_revisados < 15:
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

        prompt = build_prompt_hoy(h, a, lg, data, ug_tag)
        pred_text = get_gemini_response(prompt)

        # Delay para rate limit
        time.sleep(3)

        parsed = parse_prediccion(pred_text, "goles")

        # Log para debug
        print(f"[Hoy] {h} vs {a} | stake={parsed['stake']}u | mercado={parsed['mercado']} | pred={parsed['prediccion'][:40]}")

        save_pick(fecha_hoy, "hoy", f"{h} vs {a}", lg, parsed["mercado"], parsed["tipo"], parsed["linea"],
                    parsed["prediccion"], parsed["valor"], parsed["stake"], parsed["cuota"])

        if pick_es_valido(parsed, min_stake):
            cumple_odds = (parsed["cuota"] is None or (min_odds <= parsed["cuota"] <= max_odds))
            if parsed["stake"] >= min_stake and cumple_odds:
                emoji = "⚽" if parsed["mercado"] == "goles" else "🚩"
                # Tag de confianza visual
                if parsed["stake"] >= 4:
                    conf_tag = "🔥"
                elif parsed["stake"] >= 3:
                    conf_tag = "✅"
                elif parsed["stake"] >= 2:
                    conf_tag = "👍"
                else:
                    conf_tag = "📝"
                msg = (
                    f"📅 PICK DEL DÍA {conf_tag}\n"
                    f"{ug_tag} {h} vs {a}\n"
                    f"🏆 {lg}\n"
                    f"⏰ {match_time} (tu hora)\n\n"
                    f"{emoji} {parsed['prediccion']}\n"
                    f"💰 {parsed['valor']}\n"
                    f"🎯 Stake: {parsed['stake']}u | Bankroll: ~{parsed['stake']}%\n\n"
                    f"📝 Análisis:\n{parsed['analisis']}\n\n"
                    f"⚠️ Cuota estimada por IA (no real). Verifica en tu casa de apuestas."
                )
                await update.effective_message.reply_text(msg)
                picks_enviados += 1

    if picks_enviados == 0:
        await update.effective_message.reply_text(
            f"📊 Ningún pick válido encontrado hoy.\n"
            f"Filtro: ≥{min_stake}u | Cuotas {min_odds}-{max_odds}\n"
            f"Revisados: {partidos_revisados} partidos.\n"
            f"💡 Prueba: /stake 1 para ver picks con valor marginal."
        )
    else:
        await update.effective_message.reply_text(
            f"✅ {picks_enviados} picks enviados\n"
            f"💡 Filtro: ≥{min_stake}u | Cuotas {min_odds}-{max_odds}\n"
            f"📋 Revisados: {partidos_revisados} partidos\n"
            f"🟢 = Liga menor | 🔴 = Liga top"
        )

async def live(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("⚡ Analizando partidos en vivo...")

    matches = api_request("fixtures", {"live": "all", "timezone": USER_TIMEZONE})
    if not matches:
        await update.effective_message.reply_text("No hay partidos en vivo.")
        return

    min_stake = USER_CONFIG.get("min_stake", 1)
    min_odds = USER_CONFIG.get("min_odds", 0)
    max_odds = USER_CONFIG.get("max_odds", 999)
    fecha_hoy = get_today_str()

    live_matches = [m for m in matches if m["fixture"]["status"]["elapsed"] and 30 <= m["fixture"]["status"]["elapsed"] <= 75]

    if not live_matches:
        await update.effective_message.reply_text("No hay partidos en ventana óptima (30-75 min).")
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
            f"=== CRITERIOS LIVE (1-5 unidades) ===\n"
            f"1. MOMENTO ACTUAL (0-1.5u): ¿Quién presiona más? ¿Hay tiros al palo?\n"
            f"2. ESTADÍSTICAS 1T (0-1.5u): tiros, posesión, corners, xG\n"
            f"3. H2H 2DO TIEMPO (0-1u): ¿Suelen haber goles en 2T?\n"
            f"4. NECESIDAD DEL MARCADOR (0-1u): ¿Alguien necesita empatar/ganar?\n"
            f"5. EDGE CUOTA LIVE (0-1u): ¿Hay valor en las cuotas en vivo?\n\n"
            f"REGLAS:\n"
            f"- Suma <1u → 'NO HAY VALOR'\n"
            f"- Suma ≥1u → pick con unidades redondeadas\n"
            f"- Stake = unidades (1u≈1%, 3u≈3%, 5u≈5% bankroll)\n\n"
            f"FORMATO OBLIGATORIO:\n"
            f"PREDICCION: [ej: Over 1.5 goles 2do tiempo]\n"
            f"VALOR: [cuota estimada con @]\n"
            f"STAKE: [1-5 unidades]\n"
            f"POR QUE: [2 líneas máximo con estadística CONCRETA]"
        )

        pred_text = get_gemini_response(prompt)
        parsed = parse_prediccion(pred_text, "live")

        save_pick(fecha_hoy, "live", f"{h} vs {a}", lg, "live", parsed["tipo"], parsed["linea"],
                  parsed["prediccion"], parsed["valor"], parsed["stake"], parsed["cuota"])

        if pick_es_valido(parsed, min_stake):
            cumple_odds = (parsed["cuota"] is None or (min_odds <= parsed["cuota"] <= max_odds))
            if parsed["stake"] >= min_stake and cumple_odds:
                if parsed["stake"] >= 4:
                    conf_tag = "🔥"
                elif parsed["stake"] >= 3:
                    conf_tag = "✅"
                elif parsed["stake"] >= 2:
                    conf_tag = "👍"
                else:
                    conf_tag = "📝"
                msg = (
                    f"⚡ LIVE PICK {conf_tag}\n"
                    f"{ug_tag} {h} {hg}-{ag} {a} ({mn}')\n"
                    f"🏆 {lg}\n\n"
                    f"🎯 {parsed['prediccion']}\n"
                    f"💰 {parsed['valor']}\n"
                    f"📊 Stake: {parsed['stake']}u | Bankroll: ~{parsed['stake']}%\n\n"
                    f"📝 Análisis:\n{parsed['analisis']}\n\n"
                    f"⚠️ Cuota estimada por IA (no real). Verifica en tu casa de apuestas."
                )
                await update.effective_message.reply_text(msg)
                picks_enviados += 1

    if picks_enviados == 0:
        await update.effective_message.reply_text(
            f"📊 Ningún live pick válido encontrado.\n"
            f"Filtro: ≥{min_stake}u | Cuotas {min_odds}-{max_odds}"
        )
    else:
        await update.effective_message.reply_text(
            f"✅ {picks_enviados} live picks enviados\n"
            f"💡 Filtro: ≥{min_stake}u | Cuotas {min_odds}-{max_odds}\n"
            f"🟢 = Liga menor | 🔴 = Liga top"
        )

async def predecir(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 3:
        await update.effective_message.reply_text("❌ Uso: /predecir EquipoA vs EquipoB")
        return

    texto = " ".join(context.args)
    if " vs " not in texto:
        await update.effective_message.reply_text("❌ Usa 'vs'. Ej: /predecir Mexico vs England")
        return

    e1_raw, e2_raw = texto.split(" vs ", 1)
    e1 = e1_raw.strip()
    e2 = e2_raw.strip()
    e1_norm = normalize_text(e1)
    e2_norm = normalize_text(e2)

    await update.effective_message.reply_text(f"🔍 Analizando {e1} vs {e2}...")

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
        data = format_data(match, hf, af, hh)

        ug_tag = "🟢 UNDERGROUND" if is_ug else "🔴 LIGA TOP"

        datos_insuficientes = len(hf) < 2 and len(af) < 2 and len(hh) < 2

        if datos_insuficientes:
            prompt = (
                f"Analista de apuestas. Torneo corto, datos limitados. "
                f"Mejor análisis posible con lo disponible. "
                f"NO inventes estadísticas ni lesiones. "
                f"SÍ puedes usar estilos de juego históricos y patrones de torneos.\n\n"
                f"Partido: {h} vs {a} | Torneo: {lg}\n"
                f"Hora: {match_time}\n"
                f"Datos disponibles:\n{data}\n\n"
                f"=== CRITERIOS (1-5 unidades) ===\n"
                f"1. Datos disponibles (0-1.5u)\n"
                f"2. Consistencia de lo que sabemos (0-1.5u)\n"
                f"3. Edge de valor (0-1.5u)\n"
                f"4. Cuota y riesgo (0-1.5u)\n\n"
                f"REGLAS: suma <1u → 'NO HAY VALOR'. Stake = unidades redondeadas.\n\n"
                f"FORMATO:\n"
                f"1X2: [pred]\n"
                f"GOLES: [Over/Under X.5]\n"
                f"CORNERS: [Over/Under X.5]\n"
                f"MEJOR VALOR: [apuesta + cuota @]\n"
                f"STAKE: [1-5 unidades]\n"
                f"POR QUE: [2 líneas, indica qué datos usaste y qué limitaciones]\n"
                f"CONFIANZA: [Alta/Media/Baja]"
            )
        else:
            prompt = (
                f"Analista de apuestas. Analiza TODOS los mercados. "
                f"NO frases genéricas. Usa estadísticas concretas.\n\n"
                f"Partido: {h} vs {a} | Liga: {lg}\n"
                f"Hora: {match_time}\n"
                f"Datos:\n{data}\n\n"
                f"=== CRITERIOS PROFESIONALES (1-5 unidades) ===\n"
                f"1. Datos históricos (0-1.5u): ¿≥4 partidos recientes? ¿Consistentes?\n"
                f"2. Alineación de indicadores (0-1.5u): ¿Forma + H2H + tendencia liga coinciden?\n"
                f"3. Edge de valor (0-1.5u): ¿Cuota estimada vs cuota implícita?\n"
                f"4. Cuota óptima (0-1.5u): ¿1.70-2.20 con datos sólidos?\n\n"
                f"REGLAS: suma <1u → 'NO HAY VALOR'. Stake = unidades redondeadas.\n\n"
                f"FORMATO:\n"
                f"1X2: [pred]\n"
                f"GOLES: [Over/Under X.5]\n"
                f"CORNERS: [Over/Under X.5]\n"
                f"MEJOR VALOR: [apuesta + cuota @]\n"
                f"STAKE: [1-5 unidades]\n"
                f"POR QUE: [2 líneas con estadística CONCRETA]"
            )

        pred_text = get_gemini_response(prompt)
        print(f"[Predecir] Gemini respuesta para {h} vs {a}: {pred_text[:100]}...")

        parsed = parse_prediccion(pred_text, "mix")

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
            await update.effective_message.reply_text(msg)
        else:
            await update.effective_message.reply_text(
                f"🔮 {e1} vs {e2}\n"
                f"🏆 {lg}\n\n"
                f"{pred_text}\n\n"
                f"⚠️ Cuotas estimadas por IA (no reales)."
            )
    else:
        await update.effective_message.reply_text(f"❌ No encontré {e1} vs {e2} en los próximos 7 días. Verifica los nombres.")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    min_stake = USER_CONFIG.get("min_stake", 1)
    min_odds = USER_CONFIG.get("min_odds", 0)
    max_odds = USER_CONFIG.get("max_odds", 999)

    stats_data, por_stake = get_stats(min_stake, min_odds, max_odds)

    total, ganados, perdidos, pendientes, avg_stake, winrate = stats_data

    msg = f"📊 TU RENDIMIENTO\n"
    msg += f"Filtro: ≥{min_stake}u | Cuotas {min_odds}-{max_odds}\n\n"
    msg += f"📋 Total picks: {total}\n"
    msg += f"⏳ Pendientes: {pendientes}\n"
    msg += f"✅ Ganados: {ganados}\n"
    msg += f"❌ Perdidos: {perdidos}\n"

    if ganados + perdidos > 0:
        msg += f"📈 Winrate: {winrate}%\n"
        msg += f"🎯 Stake promedio: {avg_stake}u\n"

    if por_stake:
        msg += f"\n📊 POR UNIDADES:\n"
        for rango, r_total, r_ganados, r_perdidos, r_winrate in por_stake:
            msg += f"Stake {rango}: {r_total}p → {r_winrate}%✅\n"

    await update.effective_message.reply_text(msg)

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
