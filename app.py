import os
import csv
import sqlite3
import threading
import io
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from flask import Flask, render_template, request, redirect, url_for, jsonify, Response

basedir = os.path.abspath(os.path.dirname(__file__))
DB_FILE = os.path.join(basedir, "leads.db")

from fase1_prospeccion import (
    inicializar_base_de_datos,
    buscar_empresas_solo_google,
    guardar_leads_basicos
)
from fase2_enriquecimiento import enriquecer_pendientes, buscar_nif_infoempresa, buscar_nif_duckduckgo
from fase3_borme import ejecutar_fase3_borme
from fase4_subvenciones import ejecutar_fase4_subvenciones
from fase5_cualificacion import ejecutar_fase5_cualificacion

app = Flask(__name__)

_hilo_activo = False

PER_PAGE = 30
COLS = """id, nombre_empresa, direccion, telefono, sitio_web,
          email, linkedin_empresa, nif, administrador, estado_proceso, actualizado_en,
          subvenciones_count, subvenciones_importe, subvenciones_resumen,
          facebook_url, google_maps_url, google_rating, google_reviews_count, trustpilot_url,
          puntuacion, puntuacion_detalle, estado_contacto, notas"""

def contar_sin_nif():
    try:
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute("""
            SELECT COUNT(*) FROM b2b_leads
            WHERE estado_proceso = 'COMPLETADO_F4'
            AND (nif IS NULL OR nif = '')
        """)
        n = cur.fetchone()[0]
        conn.close()
        return n
    except sqlite3.Error:
        return 0

def worker_reintentar_nif():
    from urllib.parse import urlparse
    print("[NIF] Reintentando NIFs para leads sin NIF...")
    try:
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute("""
            SELECT id, nombre_empresa, sitio_web FROM b2b_leads
            WHERE estado_proceso = 'COMPLETADO_F4'
            AND (nif IS NULL OR nif = '')
        """)
        leads = cur.fetchall()
        conn.close()
    except sqlite3.Error as e:
        print(f"❌ [NIF] Error: {e}")
        return

    encontrados = 0
    for lead_id, nombre, sitio_web in leads:
        dominio = None
        if sitio_web:
            parsed = urlparse(sitio_web)
            dominio = parsed.netloc.replace('www.', '') or None

        nif = buscar_nif_infoempresa(nombre, dominio)
        if not nif:
            nif = buscar_nif_duckduckgo(nombre, dominio)

        if nif:
            try:
                conn = sqlite3.connect(DB_FILE)
                conn.execute(
                    "UPDATE b2b_leads SET nif = ?, estado_proceso = 'PENDIENTE_F4' WHERE id = ?",
                    (nif, lead_id)
                )
                conn.commit()
                conn.close()
                encontrados += 1
                print(f"[NIF] {nombre}: {nif} -> relanzando F4")
            except sqlite3.Error as e:
                print(f"❌ [NIF] Error actualizando {nombre}: {e}")

    print(f"[NIF] Reintento terminado. NIFs encontrados: {encontrados}/{len(leads)}")

    if encontrados > 0:
        print("[NIF] Lanzando F4 para leads recien NIFados...")
        while True:
            procesados = ejecutar_fase4_subvenciones(limite=20, workers=4)
            if procesados == 0:
                break
        ejecutar_fase5_cualificacion()
        print("[NIF] F4 + F5 completados.")

def guardar_busqueda(sector, ubicacion, leads_encontrados):
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.execute(
            "INSERT INTO b2b_busquedas (sector, ubicacion, leads_encontrados) VALUES (?, ?, ?)",
            (sector, ubicacion, leads_encontrados)
        )
        conn.commit()
        conn.close()
    except sqlite3.Error as e:
        print(f"❌ Error historial: {e}")

def obtener_historial():
    try:
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute("""
            SELECT sector, ubicacion, SUM(leads_encontrados), MAX(buscado_en), COUNT(*)
            FROM b2b_busquedas
            GROUP BY sector, ubicacion
            ORDER BY MAX(buscado_en) DESC
            LIMIT 20
        """)
        rows = cur.fetchall()
        conn.close()
        return rows
    except sqlite3.Error as e:
        print(f"❌ Error historial: {e}")
        return []

def _build_where(f_email, f_nif, f_subv, f_alta, q):
    clauses, params = [], []
    if f_email: clauses.append("email IS NOT NULL AND email != ''")
    if f_nif:   clauses.append("nif IS NOT NULL AND nif != ''")
    if f_subv:  clauses.append("subvenciones_count > 0")
    if f_alta:  clauses.append("puntuacion >= 8")
    if q:
        clauses.append("nombre_empresa LIKE ?")
        params.append(f"%{q}%")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params

def obtener_leads_paginados(page, f_email, f_nif, f_subv, f_alta, q):
    try:
        where, params = _build_where(f_email, f_nif, f_subv, f_alta, q)
        offset = (page - 1) * PER_PAGE
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute(f"SELECT COUNT(*) FROM b2b_leads {where}", params)
        total = cur.fetchone()[0]
        cur.execute(
            f"SELECT {COLS} FROM b2b_leads {where} ORDER BY puntuacion DESC, id DESC LIMIT ? OFFSET ?",
            params + [PER_PAGE, offset]
        )
        leads = cur.fetchall()
        conn.close()
        return leads, total
    except sqlite3.Error as e:
        print(f"❌ Error paginación: {e}")
        return [], 0

def calcular_estadisticas_db():
    try:
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute("""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN estado_proceso='COMPLETADO_F4' THEN 1 ELSE 0 END) AS completados,
                SUM(CASE WHEN email IS NOT NULL AND email!='' THEN 1 ELSE 0 END) AS con_email,
                SUM(CASE WHEN nif IS NOT NULL AND nif!='' THEN 1 ELSE 0 END) AS con_nif,
                SUM(CASE WHEN administrador IS NOT NULL AND administrador!='' THEN 1 ELSE 0 END) AS con_admin,
                AVG(CASE WHEN puntuacion IS NOT NULL THEN puntuacion END) AS pMedia,
                SUM(CASE WHEN puntuacion>=8 THEN 1 ELSE 0 END) AS alta_rel,
                SUM(COALESCE(subvenciones_count,0)) AS subv_count,
                SUM(COALESCE(subvenciones_importe,0)) AS subv_importe,
                SUM(CASE WHEN estado_contacto='cliente' THEN 1 ELSE 0 END) AS clientes,
                SUM(CASE WHEN estado_contacto='interesado' THEN 1 ELSE 0 END) AS interesados
            FROM b2b_leads
        """)
        r = cur.fetchone()
        conn.close()
        if not r or r[0] == 0:
            return None
        total = r[0]
        pMedia = round(r[5], 1) if r[5] is not None else None
        return {
            'total': total,
            'completados': r[1] or 0,
            'con_email': r[2] or 0,
            'pct_email': round((r[2] or 0) * 100 / total),
            'con_nif': r[3] or 0,
            'pct_nif': round((r[3] or 0) * 100 / total),
            'con_admin': r[4] or 0,
            'pct_admin': round((r[4] or 0) * 100 / total),
            'puntuacion_media': pMedia,
            'alta_relevancia': r[6] or 0,
            'subvenciones_count': r[7] or 0,
            'subvenciones_importe': r[8] or 0,
            'clientes': r[9] or 0,
            'interesados': r[10] or 0,
        }
    except sqlite3.Error as e:
        print(f"❌ Error stats: {e}")
        return None

def worker_enriquecimiento():
    """
    Worker completo F2 → F3 automático.
    Primero vacía toda la cola de scraping web,
    luego lanza BORME sin intervención manual.
    """
    global _hilo_activo
    _hilo_activo = True
    print("🔄 [HILO] Worker arrancado.")
    try:
        # --- FASE 2: Scraping de webs (4 en paralelo) ---
        while True:
            procesados = enriquecer_pendientes(limite=20, workers=4)
            print(f"🔄 [HILO F2] Lote procesado: {procesados} leads.")
            if procesados == 0:
                break

        print("✅ [HILO F2] Cola vacía. Arrancando F3 automáticamente...")

        # --- FASE 3: BORME (3 en paralelo) ---
        while True:
            procesados = ejecutar_fase3_borme(limite=15, workers=3)
            print(f"⚖️ [HILO F3] Lote BORME: {procesados} leads.")
            if procesados == 0:
                break

        print("✅ [HILO F3] Investigación mercantil completada.")

        # --- FASE 4: Subvenciones BDNS (4 en paralelo) ---
        while True:
            procesados = ejecutar_fase4_subvenciones(limite=20, workers=4)
            print(f"🏛️ [HILO F4] Lote subvenciones: {procesados} leads.")
            if procesados == 0:
                break

        print("✅ [HILO F4] Consulta BDNS completada.")

        # --- FASE 5: Cualificación automática ---
        ejecutar_fase5_cualificacion()
        print("✅ [HILO F5] Cualificación completada.")

    except Exception as e:
        print(f"❌ [HILO] Error en worker: {e}")
    finally:
        _hilo_activo = False
        print("✅ [HILO] Worker completo terminado.")

@app.route('/')
def index():
    inicializar_base_de_datos()
    page    = max(1, request.args.get('page', 1, type=int))
    f_email = request.args.get('email', 0, type=int)
    f_nif   = request.args.get('nif', 0, type=int)
    f_subv  = request.args.get('subv', 0, type=int)
    f_alta  = request.args.get('alta', 0, type=int)
    q       = request.args.get('q', '').strip()
    mensaje = request.args.get('mensaje')

    leads, total_filtrado = obtener_leads_paginados(page, f_email, f_nif, f_subv, f_alta, q)
    stats = calcular_estadisticas_db()

    total_pages = max(1, (total_filtrado + PER_PAGE - 1) // PER_PAGE)
    if page > total_pages:
        page = total_pages

    filtros = {'email': f_email, 'nif': f_nif, 'subv': f_subv, 'alta': f_alta, 'q': q}

    def toggle_url(key):
        p = dict(filtros)
        p[key] = 0 if p[key] else 1
        p['page'] = 1
        parts = [f"{k}={v}" for k, v in p.items() if v]
        return '/?' + '&'.join(parts) if parts else '/'

    def page_url(p):
        parts = [f"{k}={v}" for k, v in filtros.items() if v]
        parts.append(f"page={p}")
        return '/?' + '&'.join(parts)

    historial = obtener_historial()
    sin_nif = contar_sin_nif()

    return render_template('index.html',
        leads=leads, mensaje=mensaje, stats=stats,
        page=page, total_pages=total_pages, total_filtrado=total_filtrado,
        filtros=filtros, per_page=PER_PAGE,
        toggle_url=toggle_url, page_url=page_url,
        historial=historial, sin_nif=sin_nif
    )

@app.route('/buscar', methods=['POST'])
def buscar():
    global _hilo_activo

    sector = request.form.get('sector')
    ubicacion = request.form.get('ubicacion')

    print(f"🛰️ Buscando en Google Places: {sector} en {ubicacion}")

    resultados_basicos = buscar_empresas_solo_google(sector, ubicacion)
    guardados = guardar_leads_basicos(resultados_basicos)
    guardar_busqueda(sector, ubicacion, guardados)

    if not _hilo_activo:
        hilo = threading.Thread(target=worker_enriquecimiento, daemon=True)
        hilo.start()
    else:
        print("⚠️ [HILO] Ya hay un worker activo, no se lanza otro.")

    mensaje = (
        f"Se han volcado {guardados} empresas al panel. "
        f"Los emails, NIFs y directivos aparecerán de forma progresiva."
    )
    return redirect(url_for('index', mensaje=mensaje))

@app.route('/estado')
def estado():
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT estado_proceso, COUNT(*) 
            FROM b2b_leads 
            GROUP BY estado_proceso
        """)
        filas = cursor.fetchall()
        conn.close()

        conteos = {fila[0]: fila[1] for fila in filas}
        pendientes = (conteos.get('PENDIENTE_F2', 0) + conteos.get('PENDIENTE_F3', 0) +
                      conteos.get('PENDIENTE_F4', 0))
        completados = conteos.get('COMPLETADO_F4', 0)

        return jsonify({
            'hilo_activo': _hilo_activo,
            'pendientes_f2': conteos.get('PENDIENTE_F2', 0),
            'pendientes_f3': conteos.get('PENDIENTE_F3', 0),
            'pendientes_f4': conteos.get('PENDIENTE_F4', 0),
            'completados': completados,
            'hay_cambios': pendientes > 0
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/eliminar/<int:lead_id>')
def eliminar_lead(lead_id):
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM b2b_leads WHERE id = ?", (lead_id,))
        conn.commit()
        conn.close()
    except sqlite3.Error as e:
        return redirect(url_for('index', mensaje=f"❌ Error: {e}"))
    return redirect(url_for('index', mensaje="Lead eliminado correctamente."))

@app.route('/eliminar_todo')
def eliminar_todo():
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM b2b_leads")
        conn.commit()
        conn.close()
    except sqlite3.Error as e:
        return redirect(url_for('index', mensaje=f"❌ Error: {e}"))
    return redirect(url_for('index', mensaje="Base de datos vaciada por completo."))

@app.route('/buscar-rapido')
def buscar_rapido():
    global _hilo_activo
    sector = request.args.get('sector', '').strip()
    ubicacion = request.args.get('ubicacion', '').strip()
    if not sector or not ubicacion:
        return redirect(url_for('index'))

    resultados_basicos = buscar_empresas_solo_google(sector, ubicacion)
    guardados = guardar_leads_basicos(resultados_basicos)
    guardar_busqueda(sector, ubicacion, guardados)

    if not _hilo_activo:
        hilo = threading.Thread(target=worker_enriquecimiento, daemon=True)
        hilo.start()

    mensaje = f"Se han volcado {guardados} empresas al panel ({sector} · {ubicacion})."
    return redirect(url_for('index', mensaje=mensaje))

@app.route('/reintentar-nif')
def reintentar_nif():
    hilo = threading.Thread(target=worker_reintentar_nif, daemon=True)
    hilo.start()
    n = contar_sin_nif()
    return redirect(url_for('index', mensaje=f"Buscando NIFs para {n} leads sin NIF. F4 se lanzará automáticamente si se encuentran."))

@app.route('/investigar_borme')
def investigar_borme():
    """
    Botón de reintento manual por si algún lead se quedó atascado en PENDIENTE_F3.
    """
    def worker_borme():
        print("⚖️ [BORME] Reintento manual arrancado.")
        while True:
            procesados = ejecutar_fase3_borme(limite=5)
            if procesados == 0:
                break
        print("✅ [BORME] Reintento terminado.")

    hilo_borme = threading.Thread(target=worker_borme, daemon=True)
    hilo_borme.start()

    return redirect(url_for('index', mensaje="Reintento BORME lanzado en segundo plano."))

@app.route('/lead/<int:lead_id>/contacto', methods=['POST'])
def actualizar_contacto(lead_id):
    datos = request.get_json()
    estado = datos.get('estado_contacto')
    notas  = datos.get('notas')

    ESTADOS_VALIDOS = {'sin_contactar', 'contactado', 'interesado', 'descartado', 'cliente'}
    if estado not in ESTADOS_VALIDOS:
        return jsonify({'error': 'Estado no válido'}), 400

    try:
        conn = sqlite3.connect(DB_FILE)
        conn.execute(
            "UPDATE b2b_leads SET estado_contacto = ?, notas = ? WHERE id = ?",
            (estado, notas, lead_id)
        )
        conn.commit()
        conn.close()
        return jsonify({'ok': True})
    except sqlite3.Error as e:
        return jsonify({'error': str(e)}), 500


@app.route('/exportar')
def exportar_csv():
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT nombre_empresa, sitio_web, telefono, email, nif, administrador,
                   direccion, linkedin_empresa, facebook_url, google_maps_url,
                   google_rating, google_reviews_count, trustpilot_url,
                   subvenciones_count, subvenciones_importe, puntuacion,
                   estado_contacto, notas
            FROM b2b_leads ORDER BY puntuacion DESC, id DESC
        """)
        leads = cursor.fetchall()
        conn.close()
    except sqlite3.Error as e:
        return f"Error: {e}", 500

    columnas = [
        'Empresa', 'Web', 'Teléfono', 'Email', 'NIF/CIF', 'Administrador',
        'Dirección', 'LinkedIn', 'Facebook', 'Google Maps',
        'Valoración Google', 'Nº Reseñas Google', 'Trustpilot',
        'Nº Subvenciones', 'Importe Subvenciones (€)', 'Puntuación (0-10)',
        'Estado contacto', 'Notas'
    ]

    output = io.StringIO()
    writer = csv.writer(output, delimiter=';', quoting=csv.QUOTE_ALL)
    writer.writerow(columnas)
    for lead in leads:
        writer.writerow([v if v is not None else '' for v in lead])

    csv_bytes = output.getvalue().encode('utf-8-sig')  # utf-8-sig para que Excel lo abra bien

    return Response(
        csv_bytes,
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=leads_b2b.csv'}
    )


@app.route('/api/subvenciones/<nif>')
def api_subvenciones(nif):
    from fase4_subvenciones import consultar_subvenciones
    import re
    nif_limpio = re.sub(r'[^A-Z0-9]', '', nif.strip().upper())
    if len(nif_limpio) != 9:
        return jsonify({'error': 'NIF inválido'}), 400
    count, importe, concesiones = consultar_subvenciones(nif_limpio)
    return jsonify({'count': count, 'importe': importe, 'concesiones': concesiones})

@app.route('/enviar-email', methods=['POST'])
def enviar_email():
    datos = request.get_json()
    email_destino = (datos.get('email') or '').strip()
    scope = datos.get('scope', 'todos')  # 'todos' o 'filtrados'
    filtros_json = datos.get('filtros', {})

    if not email_destino:
        return jsonify({'error': 'Email destino requerido'}), 400

    email_user = os.getenv('EMAIL_USER', '').strip()
    email_pass = os.getenv('EMAIL_PASSWORD', '').strip()
    email_smtp = os.getenv('EMAIL_SMTP', 'mail.grupoaurea.es').strip()
    email_port = int(os.getenv('EMAIL_PORT', '465'))
    if not email_user or not email_pass:
        return jsonify({'error': 'Configura EMAIL_USER y EMAIL_PASSWORD en el archivo .env'}), 500

    # Obtener leads según scope
    if scope == 'filtrados':
        f = filtros_json
        leads, total = obtener_leads_paginados(
            page=1,
            f_email=f.get('email', 0), f_nif=f.get('nif', 0),
            f_subv=f.get('subv', 0), f_alta=f.get('alta', 0),
            q=f.get('q', '')
        )
        # Si hay más de 30 (una página), sacar todos sin límite
        if total > PER_PAGE:
            where, params = _build_where(
                f.get('email', 0), f.get('nif', 0),
                f.get('subv', 0), f.get('alta', 0), f.get('q', '')
            )
            try:
                conn = sqlite3.connect(DB_FILE)
                cur = conn.cursor()
                cur.execute(f"SELECT {COLS} FROM b2b_leads {where} ORDER BY puntuacion DESC, id DESC", params)
                leads = cur.fetchall()
                conn.close()
            except sqlite3.Error as e:
                return jsonify({'error': str(e)}), 500
    else:
        try:
            conn = sqlite3.connect(DB_FILE)
            cur = conn.cursor()
            cur.execute(f"SELECT {COLS} FROM b2b_leads ORDER BY puntuacion DESC, id DESC")
            leads = cur.fetchall()
            conn.close()
        except sqlite3.Error as e:
            return jsonify({'error': str(e)}), 500

    if not leads:
        return jsonify({'error': 'No hay leads para enviar'}), 400

    # Construir CSV
    csv_buf = io.StringIO()
    writer = csv.writer(csv_buf, delimiter=';', quoting=csv.QUOTE_ALL)
    writer.writerow(['Empresa', 'Web', 'Teléfono', 'Email', 'NIF/CIF', 'Administrador',
                     'Dirección', 'LinkedIn', 'Facebook', 'Google Maps',
                     'Valoración Google', 'Nº Reseñas', 'Trustpilot',
                     'Nº Subvenciones', 'Importe Subvenciones (€)', 'Puntuación',
                     'Estado contacto', 'Notas'])
    for l in leads:
        writer.writerow([
            l[1], l[4], l[3], l[5], l[7], l[8], l[2], l[6],
            l[14], l[15], l[16], l[17], l[18],
            l[11], l[12], l[19], l[21], l[22] or ''
        ])
    csv_bytes = csv_buf.getvalue().encode('utf-8-sig')

    # Construir HTML del email
    filas_html = ''
    for l in leads:
        puntuacion = l[19] or 0
        color = '#16a34a' if puntuacion >= 8 else ('#d97706' if puntuacion >= 5 else '#dc2626')
        subv = f"{l[11]} subv. · {l[12]:,.0f} €".replace(',', '.') if l[11] and l[11] > 0 else '—'
        filas_html += f"""
        <tr style="border-bottom:1px solid #f1f5f9">
            <td style="padding:8px 12px;font-weight:600;color:#1e293b">{l[1] or ''}</td>
            <td style="padding:8px 12px;color:#6366f1;font-size:12px">{l[5] or '—'}</td>
            <td style="padding:8px 12px;font-family:monospace;font-size:12px;color:#4f46e5">{l[7] or '—'}</td>
            <td style="padding:8px 12px;font-size:12px;color:#475569">{l[8] or '—'}</td>
            <td style="padding:8px 12px;font-size:12px;color:#d97706">{subv}</td>
            <td style="padding:8px 12px;text-align:center"><span style="font-weight:700;color:{color}">{puntuacion}/10</span></td>
        </tr>"""

    html_body = f"""
    <div style="font-family:Arial,sans-serif;max-width:900px;margin:0 auto;color:#1e293b">
        <div style="background:#4f46e5;padding:24px 32px;border-radius:12px 12px 0 0">
            <h1 style="color:white;margin:0;font-size:20px">Leads B2B</h1>
            <p style="color:#c7d2fe;margin:4px 0 0;font-size:13px">{len(leads)} empresas exportadas</p>
        </div>
        <div style="background:white;border:1px solid #e2e8f0;border-top:none;border-radius:0 0 12px 12px;overflow:hidden">
            <table style="width:100%;border-collapse:collapse;font-size:13px">
                <thead>
                    <tr style="background:#f8fafc;border-bottom:2px solid #e2e8f0">
                        <th style="padding:10px 12px;text-align:left;color:#64748b;font-size:11px;text-transform:uppercase">Empresa</th>
                        <th style="padding:10px 12px;text-align:left;color:#64748b;font-size:11px;text-transform:uppercase">Email</th>
                        <th style="padding:10px 12px;text-align:left;color:#64748b;font-size:11px;text-transform:uppercase">NIF</th>
                        <th style="padding:10px 12px;text-align:left;color:#64748b;font-size:11px;text-transform:uppercase">Administrador</th>
                        <th style="padding:10px 12px;text-align:left;color:#64748b;font-size:11px;text-transform:uppercase">Subvenciones</th>
                        <th style="padding:10px 12px;text-align:center;color:#64748b;font-size:11px;text-transform:uppercase">Punt.</th>
                    </tr>
                </thead>
                <tbody>{filas_html}</tbody>
            </table>
        </div>
        <p style="color:#94a3b8;font-size:11px;text-align:center;margin-top:16px">Generado con Leads B2B · {len(leads)} empresas</p>
    </div>"""

    # Ensamblar y enviar email
    try:
        msg = MIMEMultipart('mixed')
        msg['From'] = email_user
        msg['To'] = email_destino
        msg['Subject'] = f'Leads B2B — {len(leads)} empresas'
        msg.attach(MIMEText(html_body, 'html', 'utf-8'))

        adjunto = MIMEBase('application', 'octet-stream')
        adjunto.set_payload(csv_bytes)
        encoders.encode_base64(adjunto)
        adjunto.add_header('Content-Disposition', 'attachment', filename='leads_b2b.csv')
        msg.attach(adjunto)

        with smtplib.SMTP_SSL(email_smtp, email_port) as server:
            server.login(email_user, email_pass)
            server.sendmail(email_user, email_destino, msg.as_string())

        return jsonify({'ok': True, 'enviados': len(leads)})
    except smtplib.SMTPAuthenticationError:
        return jsonify({'error': 'Error de autenticación. Comprueba EMAIL_USER y EMAIL_PASSWORD en .env'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    app.run(debug=True, port=5000)