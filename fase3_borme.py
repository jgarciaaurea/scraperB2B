import os
import re
import time
import sqlite3
import httpx as requests
from urllib.parse import quote as _quote
from bs4 import BeautifulSoup

basedir = os.path.abspath(os.path.dirname(__file__))
DB_FILE = os.path.join(basedir, "leads.db")

HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}


def limpiar_nombre_para_busqueda(nombre):
    """Extrae las palabras clave del nombre comercial para buscar en registros."""
    if not nombre:
        return ""

    nombre_limpio = re.split(r'[-–—/|(]', nombre)[0].strip()
    palabras = nombre_limpio.lower().split()

    basura = {
        'clinica', 'clínica', 'dental', 'dentista', 'dentistas', 'centro',
        'médico', 'medico', 'dra', 'dr', 'doctor', 'doctora', 'instituto',
        'salon', 'salón', 'restaurante', 'bar', 'sl', 'slu', 'sa', 'slp',
        'sociedad', 'limitada', 'anonima', 'anónima', 'taller', 'talleres',
        'servicios', 'grupo', 'comercial', 'el', 'la', 'los', 'las', 'de', 'del'
    }

    palabras_filtradas = [p for p in palabras if p not in basura and len(p) > 2]
    return " ".join(palabras_filtradas[:3]).strip() if palabras_filtradas else " ".join(palabras[:2])


def consultar_libreborme(nif, nombre_empresa):
    """Busca administrador en LibreBORME — fuente principal."""
    try:
        if nif and len(nif.strip()) == 9:
            nif_limpio = re.sub(r'[^A-Z0-9]', '', nif.upper())
            url = f"https://libreborme.net/borme/api/v1/empresa/{nif_limpio}/"
            res = requests.get(url, headers=HEADERS, timeout=6)
            if res.status_code == 200:
                cargos = res.json().get('cargos_actuales', [])
                if cargos:
                    return cargos[0].get('name', '').title()

        if nombre_empresa:
            nombre = limpiar_nombre_para_busqueda(nombre_empresa)
            if len(nombre) < 3:
                return None
            url = f"https://libreborme.net/borme/api/v1/empresa/search/?q={_quote(nombre)}"
            res = requests.get(url, headers=HEADERS, timeout=6)
            if res.status_code == 200:
                resultados = res.json().get('objects', [])
                if resultados:
                    url_empresa = resultados[0].get('resource_uri')
                    if url_empresa:
                        res2 = requests.get(f"https://libreborme.net{url_empresa}", headers=HEADERS, timeout=6)
                        if res2.status_code == 200:
                            cargos = res2.json().get('cargos_actuales', [])
                            if cargos:
                                return cargos[0].get('name', '').title()
    except Exception as e:
        print(f"⚠️ [F3] LibreBORME error: {e}")

    return None


def consultar_infoempresa(nif, nombre_empresa):
    """
    Busca NIF y administrador en InfoEmpresa.com — muy completo para España.
    Devuelve (nif_encontrado, administrador).
    """
    intentos = []
    if nif and len(nif.strip()) == 9:
        intentos.append(f"https://www.infoempresa.com/es-es/es/empresa/{nif.strip().upper()}")
    if nombre_empresa:
        nombre = limpiar_nombre_para_busqueda(nombre_empresa)
        if len(nombre) >= 3:
            intentos.append(f"https://www.infoempresa.com/es-es/es/buscar-empresas?q={_quote(nombre)}")

    for url in intentos:
        try:
            res = requests.get(url, headers=HEADERS, timeout=8)
            if res.status_code != 200:
                continue

            soup = BeautifulSoup(res.text, 'html.parser')

            # Si es página de búsqueda, ir al primer resultado
            if 'buscar-empresas' in url:
                primer = soup.select_one('a[href*="/empresa/"]')
                if not primer:
                    continue
                url_empresa = primer.get('href', '')
                if not url_empresa.startswith('http'):
                    url_empresa = 'https://www.infoempresa.com' + url_empresa
                res = requests.get(url_empresa, headers=HEADERS, timeout=8)
                if res.status_code != 200:
                    continue
                soup = BeautifulSoup(res.text, 'html.parser')

            texto = soup.get_text(separator=' ')

            # Buscar NIF en la página si no lo teníamos
            nif_encontrado = nif
            if not nif_encontrado:
                patron = r'\b([A-HJ-NP-SU-WV][0-9]{8})\b'
                match = re.search(patron, texto)
                if match:
                    nif_encontrado = match.group(1)

            # Buscar administrador — InfoEmpresa lo pone en secciones de cargos
            admin = None
            for selector in ['[class*="cargo"]', '[class*="admin"]', '[class*="directiv"]', 'dt', 'th']:
                elementos = soup.select(selector)
                for el in elementos:
                    txt = el.get_text().lower().strip()
                    if any(k in txt for k in ['administrador', 'gerente', 'consejero', 'director']):
                        siguiente = el.find_next_sibling()
                        if siguiente:
                            nombre_admin = siguiente.get_text().strip()
                            if nombre_admin and len(nombre_admin) > 3:
                                admin = nombre_admin.title()
                                break
                if admin:
                    break

            if nif_encontrado or admin:
                print(f"✅ [F3] InfoEmpresa: NIF={nif_encontrado}, Admin={admin}")
                return nif_encontrado, admin

        except Exception as e:
            print(f"⚠️ [F3] InfoEmpresa error en {url}: {e}")
            continue

    return nif, None


def consultar_einforma(nombre_empresa):
    """Busca administrador en Einforma.com — fuente de respaldo."""
    if not nombre_empresa:
        return None

    nombre = limpiar_nombre_para_busqueda(nombre_empresa)
    if len(nombre) < 3:
        return None

    try:
        url = f"https://www.einforma.com/servlet/app/portal/LISTA_EMPRESAS/razonsocial/{_quote(nombre)}"
        res = requests.get(url, headers=HEADERS, timeout=8)
        if res.status_code != 200:
            return None

        soup = BeautifulSoup(res.text, 'html.parser')
        primer = soup.select_one('a[href*="/informacion-empresa/"]')
        if not primer:
            return None

        url_empresa = primer.get('href', '')
        if not url_empresa.startswith('http'):
            url_empresa = 'https://www.einforma.com' + url_empresa

        res2 = requests.get(url_empresa, headers=HEADERS, timeout=8)
        if res2.status_code != 200:
            return None

        soup2 = BeautifulSoup(res2.text, 'html.parser')
        texto = soup2.get_text(separator=' ')

        # Patrón: busca "Administrador" seguido de un nombre
        match = re.search(r'(?:Administrador|Gerente|Director)[^\n:]*[:\s]+([A-ZÁÉÍÓÚÑ][a-záéíóúñ]+(?:\s+[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+){1,3})', texto)
        if match:
            admin = match.group(1).strip()
            print(f"✅ [F3] Einforma: Admin={admin}")
            return admin

    except Exception as e:
        print(f"⚠️ [F3] Einforma error: {e}")

    return None


def _procesar_lead_f3(lead):
    """Función de trabajo para un hilo — solo consultas externas, sin SQLite."""
    lead_id, nif, nombre = lead
    print(f"⚖️ [F3] Investigando: {nombre} (NIF: {nif})")

    nif_principal = nif.split(',')[0].strip() if nif and nif.lower() not in ('none', '') else None
    admin = None
    nif_actualizado = nif_principal

    try:
        admin = consultar_libreborme(nif_principal, nombre)

        if not admin or not nif_actualizado:
            nif_actualizado, admin_ie = consultar_infoempresa(nif_principal, nombre)
            if not admin and admin_ie:
                admin = admin_ie

        if not admin:
            admin = consultar_einforma(nombre)

    except Exception as e:
        print(f"❌ [F3] Error en lead {lead_id} ({nombre}): {e}")

    return (lead_id, admin, nif_actualizado)


def ejecutar_fase3_borme(limite=10, workers=3):
    from concurrent.futures import ThreadPoolExecutor, as_completed

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    cursor.execute(
        "SELECT id, nif, nombre_empresa FROM b2b_leads WHERE estado_proceso = 'PENDIENTE_F3' LIMIT ?",
        (limite,)
    )
    leads = cursor.fetchall()

    if not leads:
        conn.close()
        return 0

    resultados = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futuros = {pool.submit(_procesar_lead_f3, lead): lead for lead in leads}
        for futuro in as_completed(futuros):
            try:
                resultados.append(futuro.result())
            except Exception as e:
                lead = futuros[futuro]
                print(f"❌ [F3] Excepción en lead {lead[0]}: {e}")
                resultados.append((lead[0], None, None))

    for lead_id, admin, nif_actualizado in resultados:
        cursor.execute("""
            UPDATE b2b_leads
            SET administrador = ?, nif = COALESCE(NULLIF(?, ''), nif), estado_proceso = 'PENDIENTE_F4'
            WHERE id = ?
        """, (admin, nif_actualizado, lead_id))

    conn.commit()
    conn.close()
    print(f"✅ [F3] Lote de {len(resultados)} leads procesado en paralelo (workers={workers})")
    return len(resultados)
