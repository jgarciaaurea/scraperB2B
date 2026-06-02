import os
import time
import sqlite3
import requests
from urllib.parse import urlparse
from dotenv import load_dotenv

# Importamos el motor de la Fase 2 corregido
try:
    from fase2_enriquecimiento import extraer_datos_de_url
except ImportError:
    print("⚠️ No se encontró fase2_enriquecimiento.py. El enrichment automático fallará.")

# --- CONFIGURACIÓN DE RUTAS ABSOLUTAS ---
basedir = os.path.abspath(os.path.dirname(__file__))
DB_FILE = os.path.join(basedir, "leads.db")

load_dotenv()
API_KEY = os.getenv("GOOGLE_PLACES_API_KEY")

if not API_KEY:
    raise ValueError("❌ ERROR: No se encontró la GOOGLE_PLACES_API_KEY en el archivo .env")

def inicializar_base_de_datos():
    """Crea la tabla con todos los campos necesarios para las 4 fases."""
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS b2b_leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre_empresa TEXT NOT NULL,
            direccion TEXT,
            telefono TEXT,
            sitio_web TEXT UNIQUE,
            email TEXT DEFAULT NULL,
            linkedin_empresa TEXT DEFAULT NULL,
            nif TEXT DEFAULT NULL,
            administrador TEXT DEFAULT NULL, -- NUEVO CAMPO FASE 3
            estado_proceso TEXT DEFAULT 'PENDIENTE_F2',
            actualizado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        
        # Parche de seguridad: añadir columnas nuevas sin borrar datos existentes
        columnas_nuevas = [
            "ALTER TABLE b2b_leads ADD COLUMN administrador TEXT DEFAULT NULL",
            "ALTER TABLE b2b_leads ADD COLUMN subvenciones_count INTEGER DEFAULT 0",
            "ALTER TABLE b2b_leads ADD COLUMN subvenciones_importe REAL DEFAULT NULL",
            "ALTER TABLE b2b_leads ADD COLUMN subvenciones_resumen TEXT DEFAULT NULL",
            "ALTER TABLE b2b_leads ADD COLUMN facebook_url TEXT DEFAULT NULL",
            "ALTER TABLE b2b_leads ADD COLUMN google_maps_url TEXT DEFAULT NULL",
            "ALTER TABLE b2b_leads ADD COLUMN google_rating REAL DEFAULT NULL",
            "ALTER TABLE b2b_leads ADD COLUMN google_reviews_count INTEGER DEFAULT NULL",
            "ALTER TABLE b2b_leads ADD COLUMN trustpilot_url TEXT DEFAULT NULL",
            "ALTER TABLE b2b_leads ADD COLUMN puntuacion INTEGER DEFAULT NULL",
            "ALTER TABLE b2b_leads ADD COLUMN puntuacion_detalle TEXT DEFAULT NULL",
            "ALTER TABLE b2b_leads ADD COLUMN estado_contacto TEXT DEFAULT 'sin_contactar'",
            "ALTER TABLE b2b_leads ADD COLUMN notas TEXT DEFAULT NULL",
        ]
        for sql in columnas_nuevas:
            try:
                cursor.execute(sql)
            except sqlite3.OperationalError:
                pass

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS b2b_busquedas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sector TEXT NOT NULL,
            ubicacion TEXT NOT NULL,
            leads_encontrados INTEGER DEFAULT 0,
            buscado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)

        conn.commit()
        conn.close()
    except sqlite3.Error as e:
        print(f"❌ Error DB: {e}")

def limpiar_url(url):
    if not url: return None
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}{p.path}".rstrip('/')


# =====================================================================
# ⚡ NUEVAS FUNCIONES PARA EL FLUJO ASÍNCRONO (TIEMPO REAL)
# =====================================================================

def buscar_empresas_solo_google(sector, ubicacion):
    """Obtiene la lista de Google Places a máxima velocidad sin entrar en las webs todavía."""
    url = "https://places.googleapis.com/v1/places:searchText"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": API_KEY,
        "X-Goog-FieldMask": "places.displayName,places.formattedAddress,places.websiteUri,places.nationalPhoneNumber,places.rating,places.userRatingCount,places.googleMapsUri"
    }
    payload = {"textQuery": f"{sector} en {ubicacion}", "languageCode": "es"}
    
    leads_basicos = []
    try:
        response = requests.post(url, json=payload, headers=headers)
        if response.status_code == 200:
            places = response.json().get("places", [])
            for place in places:
                web = limpiar_url(place.get("websiteUri"))
                if web:  # Solo nos interesan negocios con página web para poder raspar luego
                    leads_basicos.append({
                        "nombre": place.get("displayName", {}).get("text", "N/A"),
                        "direccion": place.get("formattedAddress", "N/A"),
                        "web": web,
                        "telefono": place.get("nationalPhoneNumber", "N/A"),
                        "google_maps_url": place.get("googleMapsUri"),
                        "google_rating": place.get("rating"),
                        "google_reviews": place.get("userRatingCount")
                    })
        else:
            print(f"❌ Error API Google: {response.text}")
    except Exception as e:
        print(f"❌ Error al consultar Google Places: {e}")
    return leads_basicos

def guardar_leads_basicos(leads):
    """
    Inserta leads nuevos como PENDIENTE_F2.
    Si la web ya existe, resetea su estado para que se vuelva a enriquecer.
    """
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        nuevos = 0
        for l in leads:
            # Intentamos insertar
            cursor.execute("""
                INSERT OR IGNORE INTO b2b_leads
                (nombre_empresa, direccion, sitio_web, telefono, google_maps_url, google_rating, google_reviews_count, estado_proceso)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDIENTE_F2')
            """, (l['nombre'], l['direccion'], l['web'], l['telefono'],
                  l.get('google_maps_url'), l.get('google_rating'), l.get('google_reviews')))

            if cursor.rowcount > 0:
                # Era nuevo
                nuevos += 1
            else:
                # Ya existía — lo reseteamos para que el worker lo reprocese
                cursor.execute("""
                    UPDATE b2b_leads
                    SET estado_proceso = 'PENDIENTE_F2',
                        email = NULL,
                        nif = NULL,
                        linkedin_empresa = NULL,
                        administrador = NULL,
                        actualizado_en = CURRENT_TIMESTAMP
                    WHERE sitio_web = ?
                """, (l['web'],))

        conn.commit()
        conn.close()
        return nuevos
    except sqlite3.Error as e:
        print(f"❌ Error al guardar leads básicos: {e}")
        return 0

# =====================================================================
# 🔄 MÉTODOS TRADICIONALES SÍNCRONOS (MANTENIDOS POR COMPATIBILIDAD)
# =====================================================================

def guardar_leads_hiper_automatico(leads):
    """Guarda los leads completamente enriquecidos en un solo bloque."""
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        query = """
        INSERT OR IGNORE INTO b2b_leads 
        (nombre_empresa, direccion, sitio_web, telefono, email, linkedin_empresa, nif, estado_proceso)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDIENTE_F3')
        """
        nuevos = 0
        for l in leads:
            cursor.execute(query, (
                l['nombre'], l['direccion'], l['web'], 
                l['telefono'], l['email'], l['linkedin'], l['nif']
            ))
            if cursor.rowcount > 0: nuevos += 1
        conn.commit()
        conn.close()
        return nuevos
    except sqlite3.Error as e:
        print(f"❌ Error al guardar en SQLite: {e}")
        return 0

def buscar_y_enriquecer_automatico(sector, ubicacion, max_resultados=20):
    """Busca y enriquece de forma síncrona (bloqueante)."""
    url = "https://places.googleapis.com/v1/places:searchText"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": API_KEY,
        "X-Goog-FieldMask": "places.displayName,places.formattedAddress,places.websiteUri,places.nationalPhoneNumber,places.rating,places.userRatingCount,places.googleMapsUri"
    }
    payload = {"textQuery": f"{sector} en {ubicacion}", "languageCode": "es"}
    
    leads_enriquecidos = []
    print(f"🚀 Iniciando búsqueda inteligente para: {sector} en {ubicacion}...")
    
    try:
        response = requests.post(url, json=payload, headers=headers)
        if response.status_code != 200: return []
        
        places = response.json().get("places", [])
        for place in places:
            web_sucia = place.get("websiteUri")
            if web_sucia:
                web = limpiar_url(web_sucia)
                nombre = place.get("displayName", {}).get("text", "N/A")
                
                print(f"🔎 Extrayendo datos de: {nombre} ({web})...")
                resultado_scraping = extraer_datos_de_url(web)
                
                if resultado_scraping and len(resultado_scraping) == 3:
                    email, linkedin, nif = resultado_scraping
                elif resultado_scraping and len(resultado_scraping) == 2:
                    email, linkedin = resultado_scraping
                    nif = None
                else:
                    email, linkedin, nif = None, None, None
                
                leads_enriquecidos.append({
                    "nombre": nombre,
                    "direccion": place.get("formattedAddress", "N/A"),
                    "web": web,
                    "telefono": place.get("nationalPhoneNumber", "N/A"),
                    "email": email,
                    "linkedin": linkedin,
                    "nif": nif
                })
                time.sleep(0.5)
    except Exception as e:
        print(f"❌ Error durante el proceso: {e}")
        
    return leads_enriquecidos