import os
import re
import time
import json
import sqlite3
import requests

basedir = os.path.abspath(os.path.dirname(__file__))
DB_FILE = os.path.join(basedir, "leads.db")

BDNS_URL = "https://www.infosubvenciones.es/bdnstrans/api/concesiones/busqueda"
HEADERS = {
    "User-Agent": "LeadGen B2B Tool / Consulta BDNS pública",
    "Accept": "application/json",
}


def consultar_subvenciones(nif):
    """
    Consulta la API oficial BDNS para obtener concesiones de una empresa.
    Devuelve (total_count, importe_total, lista_concesiones).
    """
    if not nif:
        return 0, 0.0, []

    nif_limpio = re.sub(r'[^A-Z0-9]', '', nif.split(',')[0].strip().upper())
    if len(nif_limpio) != 9:
        return 0, 0.0, []

    params = {
        "vpd": "GE",
        "nifCif": nif_limpio,
        "page": 0,
        "pageSize": 50,
    }

    try:
        res = requests.get(BDNS_URL, params=params, headers=HEADERS, timeout=10)
        if res.status_code != 200:
            print(f"⚠️ [F4] BDNS respondió {res.status_code} para NIF {nif_limpio}")
            return 0, 0.0, []

        datos = res.json()
        concesiones_raw = datos.get("content", [])
        total_count = datos.get("totalElements", len(concesiones_raw))

        # Si hay más páginas, las traemos todas
        total_pages = datos.get("totalPages", 1)
        for p in range(1, total_pages):
            params["page"] = p
            r2 = requests.get(BDNS_URL, params=params, headers=HEADERS, timeout=10)
            if r2.status_code == 200:
                concesiones_raw += r2.json().get("content", [])
            time.sleep(0.3)

        concesiones = []
        importe_total = 0.0

        for c in concesiones_raw:
            importe = float(c.get("importe") or c.get("ayudaEquivalente") or 0)
            importe_total += importe

            concesiones.append({
                "codigo": c.get("codConcesion", ""),
                "descripcion": (c.get("convocatoria") or "")[:120],
                "organismo": c.get("nivel2") or c.get("nivel1") or "",
                "fecha": c.get("fechaConcesion", ""),
                "importe": importe,
                "instrumento": (c.get("instrumento") or "").strip(),
                "url_boe": c.get("urlBR", ""),
            })

        print(f"✅ [F4] {nif_limpio}: {total_count} subvenciones, {importe_total:.2f}€ total")
        return total_count, importe_total, concesiones

    except Exception as e:
        print(f"❌ [F4] Error consultando BDNS para {nif_limpio}: {e}")
        return 0, 0.0, []


def _procesar_lead_f4(lead):
    """Función de trabajo para un hilo — solo llamadas a BDNS, sin SQLite."""
    lead_id, nif, nombre = lead
    print(f"🏛️ [F4] Consultando subvenciones: {nombre} (NIF: {nif})")
    count, importe_total, concesiones = consultar_subvenciones(nif)
    resumen_json = json.dumps(concesiones, ensure_ascii=False) if concesiones else None
    return (lead_id, count, importe_total if importe_total > 0 else None, resumen_json)


def ejecutar_fase4_subvenciones(limite=10, workers=4):
    from concurrent.futures import ThreadPoolExecutor, as_completed

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    cursor.execute(
        "SELECT id, nif, nombre_empresa FROM b2b_leads WHERE estado_proceso = 'PENDIENTE_F4' LIMIT ?",
        (limite,)
    )
    leads = cursor.fetchall()

    if not leads:
        conn.close()
        return 0

    resultados = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futuros = {pool.submit(_procesar_lead_f4, lead): lead for lead in leads}
        for futuro in as_completed(futuros):
            try:
                resultados.append(futuro.result())
            except Exception as e:
                lead = futuros[futuro]
                print(f"❌ [F4] Excepción en lead {lead[0]}: {e}")
                resultados.append((lead[0], 0, None, None))

    for lead_id, count, importe, resumen in resultados:
        cursor.execute("""
            UPDATE b2b_leads
            SET subvenciones_count = ?,
                subvenciones_importe = ?,
                subvenciones_resumen = ?,
                estado_proceso = 'COMPLETADO_F4'
            WHERE id = ?
        """, (count, importe, resumen, lead_id))

    conn.commit()
    conn.close()
    print(f"✅ [F4] Lote de {len(resultados)} leads procesado en paralelo (workers={workers})")
    return len(resultados)
