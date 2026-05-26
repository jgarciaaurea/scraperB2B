import os
import time
import sqlite3
import requests
from urllib.parse import urlparse
from dotenv import load_dotenv

# Cargamos configuración
load_dotenv()
API_KEY = os.getenv("GOOGLE_PLACES_API_KEY")
DB_FILE = "leads.db"  # Se creará automáticamente en tu carpeta

if not API_KEY:
    raise ValueError("❌ ERROR: No se encontró la GOOGLE_PLACES_API_KEY en el archivo .env")

def inicializar_base_de_datos():
    """Crea la tabla en SQLite si no existe todavía."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    # Tabla adaptada a SQLite (usamos TEXT en lugar de ENUM)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS b2b_leads (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nombre_empresa TEXT NOT NULL,
        direccion TEXT,
        telefono TEXT,
        sitio_web TEXT UNIQUE, -- Evita que la misma web entre dos veces
        email TEXT DEFAULT NULL,
        linkedin_empresa TEXT DEFAULT NULL,
        nif TEXT DEFAULT NULL,
        estado_proceso TEXT DEFAULT 'PENDIENTE_F2',
        actualizado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)
    conn.commit()
    conn.close()

def limpiar_url(url):
    """Elimina parámetros de rastreo (como ?utm_source=...) y barras finales sueltas."""
    if not url:
        return None
    parsed_url = urlparse(url)
    # Reconstruimos la URL solo con el protocolo (http/https) y el dominio/ruta base
    url_limpia = f"{parsed_url.scheme}://{parsed_url.netloc}{parsed_url.path}"
    # Quitamos la barra del final si la tiene para homogeneizar (ej: de .com/ a .com)
    return url_limpia.rstrip('/')

def guardar_leads_en_sqlite(leads):
    """Inserta los leads filtrados en la base de datos local ignorando duplicados."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    query = """
    INSERT OR IGNORE INTO b2b_leads (nombre_empresa, direccion, sitio_web, telefono, estado_proceso)
    VALUES (?, ?, ?, ?, 'PENDIENTE_F2')
    """
    
    nuevos_introducidos = 0
    for lead in leads:
        cursor.execute(query, (lead['nombre'], lead['direccion'], lead['web'], lead['telefono']))
        if cursor.rowcount > 0:
            nuevos_introducidos += 1
            
    conn.commit()
    conn.close()
    return nuevos_introducidos

def buscar_empresas_en_profundidad(sector, ubicacion, max_resultados=40):
    """Busca en Google Places, limpia datos y devuelve la lista."""
    url = "https://places.googleapis.com/v1/places:searchText"
    query_texto = f"{sector} en {ubicacion}"
    
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": API_KEY,
        "X-Goog-FieldMask": "places.displayName,places.formattedAddress,places.websiteUri,places.nationalPhoneNumber,nextPageToken"
    }
    payload = {"textQuery": query_texto, "languageCode": "es"}
    
    leads_totales = []
    paginas_procesadas = 0
    
    while url and len(leads_totales) < max_resultados:
        print(f"Solicitando página {paginas_procesadas + 1} a Google Places...")
        response = requests.post(url, json=payload, headers=headers)
        
        if response.status_code != 200:
            print(f"Error en la API: {response.text}")
            break
            
        data = response.json()
        places = data.get("places", [])
        
        for place in places:
            web_sucia = place.get("websiteUri")
            if web_sucia:
                web_limpia = limpiar_url(web_sucia) # <--- LIMPIEZA AQUÍ
                leads_totales.append({
                    "nombre": place.get("displayName", {}).get("text", "N/A"),
                    "direccion": place.get("formattedAddress", "N/A"),
                    "web": web_limpia,
                    "telefono": place.get("nationalPhoneNumber", "N/A")
                })
        
        next_token = data.get("nextPageToken")
        if next_token and len(leads_totales) < max_resultados:
            payload["pageToken"] = next_token
            paginas_procesadas += 1
            time.sleep(1.5)
        else:
            break 
            
    return leads_totales

if __name__ == "__main__":
    # 1. Aseguramos que la base de datos existe
    inicializar_base_de_datos()
    
    # 2. Lanzamos la prospección
    sector_test = "Clínicas dentales"
    ubicacion_test = "Sevilla"
    
    print(f"Iniciando prospección para '{sector_test}' en '{ubicacion_test}'...")
    resultados = buscar_empresas_en_profundidad(sector_test, ubicacion_test, max_resultados=40)
    
    # 3. Guardamos los resultados limpios en SQLite
    guardados = guardar_leads_en_sqlite(resultados)
    
    print(f"\nProceso finalizado:")
    print(f"  - Leads encontrados con web: {len(resultados)}")
    print(f"  - Nuevos leads guardados en SQLite: {guardados} (los duplicados se ignoraron automáticamente)")               