import os
import re
import json
import sqlite3

basedir = os.path.abspath(os.path.dirname(__file__))
DB_FILE = os.path.join(basedir, "leads.db")

# Términos geográficos de Extremadura
EXTREMADURA = [
    'extremadura', 'cáceres', 'caceres', 'badajoz', 'plasencia', 'mérida', 'merida',
    'navalmoral', 'coria', 'trujillo', 'zafra', 'almendralejo', 'don benito',
    'villanueva de la serena', 'montijo', 'miajadas', 'hervás', 'hervas',
    'moraleja', 'jaraíz', 'jaraiz', 'talayuela',
]

# Organismos de la Junta de Extremadura en BDNS
ORGANISMOS_JUNTA = [
    'junta de extremadura', 'extremadura', 'diputación de cáceres', 'diputación de badajoz',
    'diputacion de caceres', 'diputacion de badajoz', 'ayuntamiento de cáceres',
    'ayuntamiento de badajoz', 'ayuntamiento de mérida', 'ayuntamiento de plasencia',
]


def _contiene_extremadura(texto):
    if not texto:
        return False
    texto_lower = texto.lower()
    return any(t in texto_lower for t in EXTREMADURA)


def _subvenciones_de_extremadura(resumen_json):
    """Devuelve cuántas subvenciones vienen de organismos extremeños."""
    if not resumen_json:
        return 0
    try:
        concesiones = json.loads(resumen_json)
        count = 0
        for c in concesiones:
            organismo = (c.get('organismo') or '').lower()
            if any(t in organismo for t in ORGANISMOS_JUNTA):
                count += 1
        return count
    except Exception:
        return 0


def calcular_puntuacion(lead):
    """
    Calcula la puntuación de relevancia extremeña de un lead (0-10).
    Devuelve (puntuacion, detalle_dict).
    """
    (id_, nombre, direccion, telefono, sitio_web, email, linkedin,
     nif, administrador, estado, actualizado,
     subv_count, subv_importe, subv_resumen,
     facebook, google_maps, google_rating, google_reviews, trustpilot) = lead[:19]

    puntos = 0
    detalle = {}

    # --- Criterio 1: Sede en Extremadura (+3) ---
    if _contiene_extremadura(direccion):
        puntos += 3
        detalle['sede_extremadura'] = 3
    else:
        detalle['sede_extremadura'] = 0

    # --- Criterio 2: Subvenciones de organismos extremeños (+3) ---
    subv_ext = _subvenciones_de_extremadura(subv_resumen)
    if subv_ext > 0:
        puntos += 3
        detalle['subvenciones_extremadura'] = 3
    else:
        detalle['subvenciones_extremadura'] = 0

    # --- Criterio 3: Nombre o web menciona Extremadura (+1) ---
    if _contiene_extremadura(nombre) or _contiene_extremadura(sitio_web):
        puntos += 1
        detalle['menciona_extremadura'] = 1
    else:
        detalle['menciona_extremadura'] = 0

    # --- Criterio 4: Datos de contacto completos — email + teléfono (+2) ---
    datos_contacto = 0
    if email:
        datos_contacto += 1
    if telefono and telefono != 'N/A':
        datos_contacto += 1
    puntos += datos_contacto
    detalle['datos_contacto'] = datos_contacto

    # --- Criterio 5: Identificable — tiene NIF y administrador (+1) ---
    if nif and administrador:
        puntos += 1
        detalle['identificable'] = 1
    else:
        detalle['identificable'] = 0

    puntuacion_final = min(puntos, 10)
    return puntuacion_final, detalle


def ejecutar_fase5_cualificacion():
    """Recalcula la puntuación de todos los leads completados."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT id, nombre_empresa, direccion, telefono, sitio_web,
               email, linkedin_empresa, nif, administrador, estado_proceso, actualizado_en,
               subvenciones_count, subvenciones_importe, subvenciones_resumen,
               facebook_url, google_maps_url, google_rating, google_reviews_count, trustpilot_url
        FROM b2b_leads
        WHERE estado_proceso = 'COMPLETADO_F4'
    """)
    leads = cursor.fetchall()

    actualizados = 0
    for lead in leads:
        lead_id = lead[0]
        puntuacion, detalle = calcular_puntuacion(lead)
        cursor.execute("""
            UPDATE b2b_leads SET puntuacion = ?, puntuacion_detalle = ? WHERE id = ?
        """, (puntuacion, json.dumps(detalle, ensure_ascii=False), lead_id))
        actualizados += 1

    conn.commit()
    conn.close()
    print(f"[F5] Puntuacion calculada para {actualizados} leads.")
    return actualizados
