import os
import re
import sqlite3
import httpx
import requests
import urllib3
from urllib.parse import quote as _quote
from urllib.parse import urlparse
from bs4 import BeautifulSoup

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

basedir = os.path.abspath(os.path.dirname(__file__))
DB_FILE = os.path.join(basedir, "leads.db")

EMAIL_REGEX = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,4}'

# Emails que no son de contacto real
EMAIL_BASURA = {
    'noreply', 'no-reply', 'donotreply', 'mailer', 'bounce', 'postmaster',
    'webmaster', 'hostmaster', 'abuse', 'spam', 'example', 'test', 'demo',
    'soporte', 'support', 'help', 'ayuda', 'notificaciones', 'notificacion',
    'notif', 'newsletter', 'news', 'marketing', 'publicidad', 'ventas-auto',
    'autoresponder', 'robot', 'bot', 'system', 'sistema', 'admin-auto'
}

# Dominios de imágenes/archivos que no son emails reales
EXTENSIONES_FALSAS = ('.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', '.ico', '.pdf', '.zip')

# CIF: letra + 8 dígitos (ej: B12345678)
NIF_PATRON_CIF = r'\b([A-HJ-NP-SU-WVa-hj-np-su-wv][\s\-.]?[0-9][\s\-.]?[0-9][\s\-.]?[0-9][\s\-.]?[0-9][\s\-.]?[0-9][\s\-.]?[0-9][\s\-.]?[0-9][\s\-.]?[0-9A-Za-z])\b'
# DNI/NIF persona física: 8 dígitos + letra (ej: 80051614E)
NIF_PATRON_DNI = r'\b([0-9]{8}[\s\-.]?[A-Za-z])\b'


def es_email_valido(email):
    """Descarta emails basura y de imágenes."""
    if email.endswith(EXTENSIONES_FALSAS):
        return False
    parte_local = email.split('@')[0].lower()
    if any(basura in parte_local for basura in EMAIL_BASURA):
        return False
    # Descarta emails con números de versión tipo 1.0@, 2.5@
    if re.match(r'^\d+\.\d+$', parte_local):
        return False
    return True


def extraer_nifs_de_texto(texto_bruto):
    if not texto_bruto:
        return set()
    try:
        soup = BeautifulSoup(texto_bruto, 'html.parser')
        texto = soup.get_text(separator=' ')
    except Exception:
        texto = texto_bruto

    nifs = set()

    # CIF: letra inicial + 8 caracteres alfanuméricos
    for match in re.findall(NIF_PATRON_CIF, texto):
        limpio = re.sub(r'[\s\-.]', '', match).upper()
        if len(limpio) == 9 and limpio[0].isalpha():
            nifs.add(limpio)

    # DNI/NIF persona física: 8 dígitos + letra final
    for match in re.findall(NIF_PATRON_DNI, texto):
        limpio = re.sub(r'[\s\-.]', '', match).upper()
        if len(limpio) == 9 and limpio[:8].isdigit() and limpio[8].isalpha():
            nifs.add(limpio)

    return nifs


def buscar_nif_infoempresa(nombre_empresa, dominio=None):
    """
    Busca el NIF en InfoEmpresa.com — muy fiable para empresas españolas.
    Intenta primero por dominio web, luego por nombre.
    """
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}

    intentos = []

    # Intento 1: buscar por dominio web (muy preciso)
    if dominio:
        dominio_limpio = dominio.replace('www.', '').strip('/')
        intentos.append(f"https://www.infoempresa.com/es-es/es/buscar-empresas?q={_quote(dominio_limpio)}")

    # Intento 2: buscar por nombre
    if nombre_empresa:
        intentos.append(f"https://www.infoempresa.com/es-es/es/buscar-empresas?q={_quote(nombre_empresa)}")

    for url_busqueda in intentos:
        try:
            res = requests.get(url_busqueda, headers=headers, timeout=8)
            if res.status_code != 200:
                continue

            soup = BeautifulSoup(res.text, 'html.parser')

            # El primer resultado de la lista
            primer_resultado = soup.select_one('a[href*="/empresa/"]')
            if not primer_resultado:
                continue

            url_empresa = primer_resultado.get('href', '')
            if not url_empresa.startswith('http'):
                url_empresa = 'https://www.infoempresa.com' + url_empresa

            res2 = requests.get(url_empresa, headers=headers, timeout=8)
            if res2.status_code != 200:
                continue

            soup2 = BeautifulSoup(res2.text, 'html.parser')
            texto = soup2.get_text(separator=' ')

            nifs = extraer_nifs_de_texto(texto)
            if nifs:
                print(f"✅ [F2] NIF encontrado en InfoEmpresa: {list(nifs)[0]}")
                return list(nifs)[0]

        except Exception as e:
            print(f"⚠️ [F2] InfoEmpresa error: {e}")
            continue

    return None


def buscar_nif_duckduckgo(nombre_empresa, dominio=None):
    """
    Busca el NIF en DuckDuckGo como último recurso.
    Lanza varias queries y extrae NIFs de los snippets de resultados.
    """
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept-Language': 'es-ES,es;q=0.9',
    }

    queries = []
    if nombre_empresa:
        queries.append(f'{nombre_empresa} CIF NIF site:.es')
        queries.append(f'{nombre_empresa} "aviso legal" CIF')
    if dominio:
        queries.append(f'site:{dominio} CIF NIF')

    for query in queries:
        try:
            url = f'https://html.duckduckgo.com/html/?q={_quote(query)}'
            res = requests.get(url, headers=headers, timeout=8)
            if res.status_code != 200:
                continue

            soup = BeautifulSoup(res.text, 'html.parser')

            # Extraemos texto de títulos + snippets de resultados
            texto_resultados = ' '.join([
                el.get_text(separator=' ')
                for el in soup.select('.result__snippet, .result__title, .result__url')
            ])

            nifs = extraer_nifs_de_texto(texto_resultados)

            # Filtrar NIFs genéricos o muy cortos que pueden ser falsos positivos
            nifs_validos = {n for n in nifs if len(n) == 9}
            if nifs_validos:
                nif = sorted(nifs_validos)[0]
                print(f"✅ [F2] NIF encontrado en DuckDuckGo ({query[:40]}...): {nif}")
                return nif

        except Exception as e:
            print(f"⚠️ [F2] DuckDuckGo error: {e}")
            continue

    return None


def extraer_redes_sociales(soup):
    """Extrae Facebook y Trustpilot de los enlaces de una página."""
    facebook = None
    trustpilot = None

    for link in soup.find_all('a', href=True):
        href = link['href']
        if not facebook and 'facebook.com/' in href:
            # Descartar links genéricos de compartir
            if not any(x in href for x in ['sharer', 'share', 'dialog', 'plugins']):
                facebook = href.split('?')[0].rstrip('/')
        if not trustpilot and 'trustpilot.com/review/' in href:
            trustpilot = href.split('?')[0]

    return facebook, trustpilot


def _es_pagina_js(html, texto):
    """Detecta si una página necesita JS para mostrar su contenido real."""
    if len(texto.strip()) < 300:
        return True
    # Señales típicas de SPA/SSR vacío
    indicadores = ['<app-root>', '<div id="app"></div>', '<div id="root"></div>',
                   'window.__NUXT__', 'window.__NEXT_DATA__']
    return any(i in html for i in indicadores)


def _scrape_con_playwright(url):
    """Renderiza la página con Chromium headless y devuelve el HTML completo."""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, timeout=20000, wait_until='domcontentloaded')
            page.wait_for_timeout(2000)  # esperar JS
            html = page.content()
            browser.close()
            return html
    except Exception as e:
        print(f"⚠️ [F2] Playwright error en {url}: {e}")
        return None


def _parsear_html(html):
    """Extrae todos los datos de un bloque HTML."""
    emails, nifs = set(), set()
    linkedin = facebook = trustpilot = None

    soup = BeautifulSoup(html, 'html.parser')
    texto = soup.get_text(separator=' ')

    for email in re.findall(EMAIL_REGEX, texto):
        if es_email_valido(email):
            emails.add(email.lower())

    for tag in soup.find_all('a', href=True):
        href = tag['href']
        if href.startswith('mailto:'):
            email = href.replace('mailto:', '').split('?')[0].strip().lower()
            if email and es_email_valido(email):
                emails.add(email)
        if not linkedin and ('linkedin.com/company/' in href or 'linkedin.com/in/' in href):
            linkedin = href
        if not facebook and 'facebook.com/' in href:
            if not any(x in href for x in ['sharer', 'share', 'dialog', 'plugins']):
                facebook = href.split('?')[0].rstrip('/')
        if not trustpilot and 'trustpilot.com/review/' in href:
            trustpilot = href.split('?')[0]

    nifs.update(extraer_nifs_de_texto(html))
    return emails, nifs, linkedin, facebook, trustpilot, soup, texto


def extraer_datos_de_url(url, _nombre_empresa=None):
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    }
    parsed = urlparse(url)
    dominio = parsed.netloc.replace('www.', '')

    html_principal = None

    # Intento 1: httpx
    try:
        res = httpx.get(url, headers=headers, timeout=15, follow_redirects=True, verify=False)
        if res.status_code == 200:
            html_principal = res.text
    except httpx.TimeoutException:
        print(f"⏱️ [F2] Timeout en {url}, reintentando con más tiempo...")
        try:
            res = httpx.get(url, headers=headers, timeout=25, follow_redirects=True, verify=False)
            if res.status_code == 200:
                html_principal = res.text
        except Exception:
            pass
    except Exception as e:
        print(f"❌ [F2] Error visitando {url}: {e}")

    # Intento 2: Playwright si la página parece JS o no cargó
    if not html_principal:
        print(f"🎭 [F2] Usando Playwright para {url}")
        html_principal = _scrape_con_playwright(url)
    else:
        soup_test = BeautifulSoup(html_principal, 'html.parser')
        texto_test = soup_test.get_text(separator=' ')
        if _es_pagina_js(html_principal, texto_test):
            print(f"🎭 [F2] Web con JS detectada, usando Playwright: {url}")
            html_pw = _scrape_con_playwright(url)
            if html_pw:
                html_principal = html_pw

    if not html_principal:
        return None, None, None, None, None

    emails, nifs, linkedin, facebook, trustpilot, soup, texto = _parsear_html(html_principal)

    # Fallback 1: InfoEmpresa por dominio
    if not nifs:
        nif_externo = buscar_nif_infoempresa(None, dominio)
        if nif_externo:
            nifs.add(nif_externo)

    # Fallback 2: DuckDuckGo con nombre + dominio
    if not nifs and _nombre_empresa:
        nif_externo = buscar_nif_duckduckgo(_nombre_empresa, dominio)
        if nif_externo:
            nifs.add(nif_externo)

    # Priorizar emails con el dominio propio de la empresa
    emails_propios = {e for e in emails if dominio and dominio in e}
    emails_finales = emails_propios if emails_propios else emails

    email_final = ", ".join(sorted(emails_finales)) if emails_finales else None
    nif_final = ", ".join(sorted(nifs)) if nifs else None

    return email_final, linkedin, nif_final, facebook, trustpilot


def _procesar_lead_f2(lead):
    """Función de trabajo para un hilo — solo red, sin tocar SQLite."""
    lead_id, url, nombre = lead

    if not url or not url.startswith('http'):
        return (lead_id, None, None, None, None, None)

    print(f"--> [F2] Analizando: {url}")
    email, linkedin, nif, facebook, trustpilot = extraer_datos_de_url(url, _nombre_empresa=nombre)
    return (lead_id, email, linkedin, nif, facebook, trustpilot)


def enriquecer_pendientes(limite=20, workers=4):
    from concurrent.futures import ThreadPoolExecutor, as_completed

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    cursor.execute(
        "SELECT id, sitio_web, nombre_empresa FROM b2b_leads WHERE estado_proceso = 'PENDIENTE_F2' LIMIT ?",
        (limite,)
    )
    leads = cursor.fetchall()

    if not leads:
        conn.close()
        return 0

    # Procesamos en paralelo con pool de N workers
    resultados = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futuros = {pool.submit(_procesar_lead_f2, lead): lead for lead in leads}
        for futuro in as_completed(futuros):
            try:
                resultados.append(futuro.result())
            except Exception as e:
                lead = futuros[futuro]
                print(f"❌ [F2] Excepción en lead {lead[0]}: {e}")
                resultados.append((lead[0], None, None, None, None, None))

    # Escritura en bloque — hilo principal, sin concurrencia
    for lead_id, email, linkedin, nif, facebook, trustpilot in resultados:
        cursor.execute("""
            UPDATE b2b_leads
            SET email = ?, linkedin_empresa = ?, nif = ?, facebook_url = ?, trustpilot_url = ?,
                estado_proceso = 'PENDIENTE_F3'
            WHERE id = ?
        """, (email, linkedin, nif, facebook, trustpilot, lead_id))

    conn.commit()
    conn.close()
    print(f"✅ [F2] Lote de {len(resultados)} leads procesado en paralelo (workers={workers})")
    return len(resultados)
