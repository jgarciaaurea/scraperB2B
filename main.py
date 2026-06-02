import os
import csv
import io
import json
import smtplib
import threading
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from fastapi import FastAPI, Request, Form, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse, Response, JSONResponse
from fastapi.templating import Jinja2Templates
import aiosqlite

from fase1_prospeccion import inicializar_base_de_datos, buscar_empresas_solo_google, guardar_leads_basicos
from fase2_enriquecimiento import enriquecer_pendientes, buscar_nif_infoempresa, buscar_nif_duckduckgo
from fase3_borme import ejecutar_fase3_borme
from fase4_subvenciones import ejecutar_fase4_subvenciones
from fase5_cualificacion import ejecutar_fase5_cualificacion

basedir = os.path.abspath(os.path.dirname(__file__))
DB_FILE = os.path.join(basedir, "leads.db")

app = FastAPI()
templates = Jinja2Templates(directory="templates")

_hilo_activo = False

PER_PAGE = 30
COLS = """id, nombre_empresa, direccion, telefono, sitio_web,
          email, linkedin_empresa, nif, administrador, estado_proceso, actualizado_en,
          subvenciones_count, subvenciones_importe, subvenciones_resumen,
          facebook_url, google_maps_url, google_rating, google_reviews_count, trustpilot_url,
          puntuacion, puntuacion_detalle, estado_contacto, notas"""


def _build_where(f_email, f_nif, f_subv, f_alta, q):
    clauses, params = [], []
    if f_email:
        clauses.append("email IS NOT NULL AND email != ''")
    if f_nif:
        clauses.append("nif IS NOT NULL AND nif != ''")
    if f_subv:
        clauses.append("subvenciones_count > 0")
    if f_alta:
        clauses.append("puntuacion >= 8")
    if q:
        clauses.append("nombre_empresa LIKE ?")
        params.append(f"%{q}%")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def _sync_db(fn):
    import sqlite3
    conn = sqlite3.connect(DB_FILE)
    result = fn(conn)
    conn.close()
    return result


def obtener_leads_paginados(page, f_email, f_nif, f_subv, f_alta, q):
    import sqlite3
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
        print(f"Error paginación: {e}")
        return [], 0


def calcular_estadisticas_db():
    import sqlite3
    try:
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute("""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN estado_proceso='COMPLETADO_F4' THEN 1 ELSE 0 END),
                SUM(CASE WHEN email IS NOT NULL AND email!='' THEN 1 ELSE 0 END),
                SUM(CASE WHEN nif IS NOT NULL AND nif!='' THEN 1 ELSE 0 END),
                SUM(CASE WHEN administrador IS NOT NULL AND administrador!='' THEN 1 ELSE 0 END),
                AVG(CASE WHEN puntuacion IS NOT NULL THEN puntuacion END),
                SUM(CASE WHEN puntuacion>=8 THEN 1 ELSE 0 END),
                SUM(COALESCE(subvenciones_count,0)),
                SUM(COALESCE(subvenciones_importe,0)),
                SUM(CASE WHEN estado_contacto='cliente' THEN 1 ELSE 0 END),
                SUM(CASE WHEN estado_contacto='interesado' THEN 1 ELSE 0 END)
            FROM b2b_leads
        """)
        r = cur.fetchone()
        conn.close()
        if not r or r[0] == 0:
            return None
        total = r[0]
        return {
            'total': total,
            'completados': r[1] or 0,
            'con_email': r[2] or 0,
            'pct_email': round((r[2] or 0) * 100 / total),
            'con_nif': r[3] or 0,
            'pct_nif': round((r[3] or 0) * 100 / total),
            'con_admin': r[4] or 0,
            'pct_admin': round((r[4] or 0) * 100 / total),
            'puntuacion_media': round(r[5], 1) if r[5] else None,
            'alta_relevancia': r[6] or 0,
            'subvenciones_count': r[7] or 0,
            'subvenciones_importe': r[8] or 0,
            'clientes': r[9] or 0,
            'interesados': r[10] or 0,
        }
    except Exception as e:
        print(f"Error stats: {e}")
        return None


def obtener_historial():
    import sqlite3
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
    except Exception as e:
        print(f"Error historial: {e}")
        return []


def contar_sin_nif():
    import sqlite3
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
    except Exception:
        return 0


def guardar_busqueda(sector, ubicacion, leads_encontrados):
    import sqlite3
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.execute(
            "INSERT INTO b2b_busquedas (sector, ubicacion, leads_encontrados) VALUES (?, ?, ?)",
            (sector, ubicacion, leads_encontrados)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Error historial: {e}")


def worker_enriquecimiento():
    global _hilo_activo
    _hilo_activo = True
    try:
        while True:
            if enriquecer_pendientes(limite=20, workers=4) == 0:
                break
        while True:
            if ejecutar_fase3_borme(limite=15, workers=3) == 0:
                break
        while True:
            if ejecutar_fase4_subvenciones(limite=20, workers=4) == 0:
                break
        ejecutar_fase5_cualificacion()
    except Exception as e:
        print(f"Error en worker: {e}")
    finally:
        _hilo_activo = False


def worker_reintentar_nif():
    import sqlite3
    from urllib.parse import urlparse
    try:
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute("""
            SELECT id, nombre_empresa, sitio_web FROM b2b_leads
            WHERE estado_proceso = 'COMPLETADO_F4' AND (nif IS NULL OR nif = '')
        """)
        leads = cur.fetchall()
        conn.close()
    except Exception:
        return

    encontrados = 0
    for lead_id, nombre, sitio_web in leads:
        dominio = None
        if sitio_web:
            parsed = urlparse(sitio_web)
            dominio = parsed.netloc.replace('www.', '') or None

        nif = buscar_nif_infoempresa(nombre, dominio) or buscar_nif_duckduckgo(nombre, dominio)
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
            except Exception:
                pass

    if encontrados > 0:
        while ejecutar_fase4_subvenciones(limite=20, workers=4) > 0:
            pass
        ejecutar_fase5_cualificacion()


# ─── Rutas ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request, page: int = 1, email: int = 0, nif: int = 0,
                subv: int = 0, alta: int = 0, q: str = "", mensaje: str = ""):
    inicializar_base_de_datos()
    page = max(1, page)
    leads, total_filtrado = obtener_leads_paginados(page, email, nif, subv, alta, q)
    stats = calcular_estadisticas_db()
    total_pages = max(1, (total_filtrado + PER_PAGE - 1) // PER_PAGE)
    if page > total_pages:
        page = total_pages

    filtros = {'email': email, 'nif': nif, 'subv': subv, 'alta': alta, 'q': q}

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

    return templates.TemplateResponse("index.html", {
        "request": request,
        "leads": leads, "mensaje": mensaje, "stats": stats,
        "page": page, "total_pages": total_pages, "total_filtrado": total_filtrado,
        "filtros": filtros, "per_page": PER_PAGE,
        "toggle_url": toggle_url, "page_url": page_url,
        "historial": obtener_historial(), "sin_nif": contar_sin_nif(),
    })


@app.post("/buscar")
async def buscar(sector: str = Form(...), ubicacion: str = Form(...)):
    global _hilo_activo
    resultados = buscar_empresas_solo_google(sector, ubicacion)
    guardados = guardar_leads_basicos(resultados)
    guardar_busqueda(sector, ubicacion, guardados)
    if not _hilo_activo:
        threading.Thread(target=worker_enriquecimiento, daemon=True).start()
    mensaje = f"Se han volcado {guardados} empresas al panel. Los emails, NIFs y directivos aparecerán de forma progresiva."
    return RedirectResponse(url=f"/?mensaje={mensaje}", status_code=303)


@app.get("/buscar-rapido")
async def buscar_rapido(sector: str = "", ubicacion: str = ""):
    global _hilo_activo
    if not sector or not ubicacion:
        return RedirectResponse(url="/", status_code=303)
    resultados = buscar_empresas_solo_google(sector, ubicacion)
    guardados = guardar_leads_basicos(resultados)
    guardar_busqueda(sector, ubicacion, guardados)
    if not _hilo_activo:
        threading.Thread(target=worker_enriquecimiento, daemon=True).start()
    mensaje = f"Se han volcado {guardados} empresas al panel ({sector} · {ubicacion})."
    return RedirectResponse(url=f"/?mensaje={mensaje}", status_code=303)


@app.get("/estado")
async def estado():
    import sqlite3
    try:
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute("SELECT estado_proceso, COUNT(*) FROM b2b_leads GROUP BY estado_proceso")
        conteos = {f[0]: f[1] for f in cur.fetchall()}
        conn.close()
        pendientes = sum(conteos.get(k, 0) for k in ('PENDIENTE_F2', 'PENDIENTE_F3', 'PENDIENTE_F4'))
        return {
            'hilo_activo': _hilo_activo,
            'pendientes_f2': conteos.get('PENDIENTE_F2', 0),
            'pendientes_f3': conteos.get('PENDIENTE_F3', 0),
            'pendientes_f4': conteos.get('PENDIENTE_F4', 0),
            'completados': conteos.get('COMPLETADO_F4', 0),
            'hay_cambios': pendientes > 0,
        }
    except Exception as e:
        return JSONResponse({'error': str(e)}, status_code=500)


@app.get("/eliminar/{lead_id}")
async def eliminar_lead(lead_id: int):
    import sqlite3
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.execute("DELETE FROM b2b_leads WHERE id = ?", (lead_id,))
        conn.commit()
        conn.close()
    except Exception as e:
        return RedirectResponse(url=f"/?mensaje=Error: {e}", status_code=303)
    return RedirectResponse(url="/?mensaje=Lead eliminado correctamente.", status_code=303)


@app.get("/eliminar_todo")
async def eliminar_todo():
    import sqlite3
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.execute("DELETE FROM b2b_leads")
        conn.commit()
        conn.close()
    except Exception as e:
        return RedirectResponse(url=f"/?mensaje=Error: {e}", status_code=303)
    return RedirectResponse(url="/?mensaje=Base de datos vaciada por completo.", status_code=303)


@app.get("/investigar_borme")
async def investigar_borme():
    def worker():
        while ejecutar_fase3_borme(limite=5) > 0:
            pass
    threading.Thread(target=worker, daemon=True).start()
    return RedirectResponse(url="/?mensaje=Reintento BORME lanzado en segundo plano.", status_code=303)


@app.get("/reintentar-nif")
async def reintentar_nif():
    threading.Thread(target=worker_reintentar_nif, daemon=True).start()
    n = contar_sin_nif()
    return RedirectResponse(url=f"/?mensaje=Buscando NIFs para {n} leads sin NIF.", status_code=303)


@app.post("/lead/{lead_id}/contacto")
async def actualizar_contacto(lead_id: int, request: Request):
    import sqlite3
    datos = await request.json()
    estado = datos.get('estado_contacto')
    notas = datos.get('notas')
    ESTADOS_VALIDOS = {'sin_contactar', 'contactado', 'interesado', 'descartado', 'cliente'}
    if estado not in ESTADOS_VALIDOS:
        return JSONResponse({'error': 'Estado no válido'}, status_code=400)
    try:
        conn = sqlite3.connect(DB_FILE)
        conn.execute(
            "UPDATE b2b_leads SET estado_contacto = ?, notas = ? WHERE id = ?",
            (estado, notas, lead_id)
        )
        conn.commit()
        conn.close()
        return {'ok': True}
    except Exception as e:
        return JSONResponse({'error': str(e)}, status_code=500)


@app.get("/exportar")
async def exportar_csv():
    import sqlite3
    try:
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute("""
            SELECT nombre_empresa, sitio_web, telefono, email, nif, administrador,
                   direccion, linkedin_empresa, facebook_url, google_maps_url,
                   google_rating, google_reviews_count, trustpilot_url,
                   subvenciones_count, subvenciones_importe, puntuacion,
                   estado_contacto, notas
            FROM b2b_leads ORDER BY puntuacion DESC, id DESC
        """)
        leads = cur.fetchall()
        conn.close()
    except Exception as e:
        return Response(f"Error: {e}", status_code=500)

    columnas = [
        'Empresa', 'Web', 'Teléfono', 'Email', 'NIF/CIF', 'Administrador',
        'Dirección', 'LinkedIn', 'Facebook', 'Google Maps',
        'Valoración Google', 'Nº Reseñas Google', 'Trustpilot',
        'Nº Subvenciones', 'Importe Subvenciones (€)', 'Puntuación (0-10)',
        'Estado contacto', 'Notas',
    ]
    output = io.StringIO()
    writer = csv.writer(output, delimiter=';', quoting=csv.QUOTE_ALL)
    writer.writerow(columnas)
    for lead in leads:
        writer.writerow([v if v is not None else '' for v in lead])

    return Response(
        output.getvalue().encode('utf-8-sig'),
        media_type='text/csv',
        headers={'Content-Disposition': 'attachment; filename=leads_b2b.csv'},
    )


@app.get("/api/subvenciones/{nif}")
async def api_subvenciones(nif: str):
    import re
    from fase4_subvenciones import consultar_subvenciones
    nif_limpio = re.sub(r'[^A-Z0-9]', '', nif.strip().upper())
    if len(nif_limpio) != 9:
        return JSONResponse({'error': 'NIF inválido'}, status_code=400)
    count, importe, concesiones = consultar_subvenciones(nif_limpio)
    return {'count': count, 'importe': importe, 'concesiones': concesiones}


@app.post("/enviar-email")
async def enviar_email(request: Request):
    datos = await request.json()
    email_destino = (datos.get('email') or '').strip()
    scope = datos.get('scope', 'todos')
    filtros_json = datos.get('filtros', {})

    if not email_destino:
        return JSONResponse({'error': 'Email destino requerido'}, status_code=400)

    email_user = os.getenv('EMAIL_USER', '').strip()
    email_pass = os.getenv('EMAIL_PASSWORD', '').strip()
    email_smtp = os.getenv('EMAIL_SMTP', 'mail.grupoaurea.es').strip()
    email_port = int(os.getenv('EMAIL_PORT', '465'))

    if not email_user or not email_pass:
        return JSONResponse({'error': 'Configura EMAIL_USER y EMAIL_PASSWORD en el archivo .env'}, status_code=500)

    import sqlite3
    if scope == 'filtrados':
        f = filtros_json
        where, params = _build_where(f.get('email', 0), f.get('nif', 0), f.get('subv', 0), f.get('alta', 0), f.get('q', ''))
        try:
            conn = sqlite3.connect(DB_FILE)
            cur = conn.cursor()
            cur.execute(f"SELECT {COLS} FROM b2b_leads {where} ORDER BY puntuacion DESC, id DESC", params)
            leads = cur.fetchall()
            conn.close()
        except Exception as e:
            return JSONResponse({'error': str(e)}, status_code=500)
    else:
        try:
            conn = sqlite3.connect(DB_FILE)
            cur = conn.cursor()
            cur.execute(f"SELECT {COLS} FROM b2b_leads ORDER BY puntuacion DESC, id DESC")
            leads = cur.fetchall()
            conn.close()
        except Exception as e:
            return JSONResponse({'error': str(e)}, status_code=500)

    if not leads:
        return JSONResponse({'error': 'No hay leads para enviar'}, status_code=400)

    csv_buf = io.StringIO()
    writer = csv.writer(csv_buf, delimiter=';', quoting=csv.QUOTE_ALL)
    writer.writerow(['Empresa', 'Web', 'Teléfono', 'Email', 'NIF/CIF', 'Administrador',
                     'Dirección', 'LinkedIn', 'Facebook', 'Google Maps',
                     'Valoración Google', 'Nº Reseñas', 'Trustpilot',
                     'Nº Subvenciones', 'Importe Subvenciones (€)', 'Puntuación',
                     'Estado contacto', 'Notas'])
    for l in leads:
        writer.writerow([l[1], l[4], l[3], l[5], l[7], l[8], l[2], l[6],
                         l[14], l[15], l[16], l[17], l[18], l[11], l[12], l[19], l[21], l[22] or ''])
    csv_bytes = csv_buf.getvalue().encode('utf-8-sig')

    filas_html = ''
    for l in leads:
        puntuacion = l[19] or 0
        color = '#16a34a' if puntuacion >= 8 else ('#d97706' if puntuacion >= 5 else '#dc2626')
        subv = f"{l[11]} subv. · {l[12]:,.0f} €".replace(',', '.') if l[11] and l[11] > 0 else '—'
        filas_html += f"""
        <tr style="border-bottom:1px solid #f1f5f9">
            <td style="padding:8px 12px;font-weight:600">{l[1] or ''}</td>
            <td style="padding:8px 12px;font-size:12px">{l[5] or '—'}</td>
            <td style="padding:8px 12px;font-size:12px">{l[7] or '—'}</td>
            <td style="padding:8px 12px;font-size:12px">{l[8] or '—'}</td>
            <td style="padding:8px 12px;font-size:12px">{subv}</td>
            <td style="padding:8px 12px;text-align:center"><span style="font-weight:700;color:{color}">{puntuacion}/10</span></td>
        </tr>"""

    html_body = f"""
    <div style="font-family:Arial,sans-serif;max-width:900px;margin:0 auto">
        <div style="background:#4f46e5;padding:24px 32px;border-radius:12px 12px 0 0">
            <h1 style="color:white;margin:0;font-size:20px">Leads B2B</h1>
            <p style="color:#c7d2fe;margin:4px 0 0;font-size:13px">{len(leads)} empresas exportadas</p>
        </div>
        <div style="background:white;border:1px solid #e2e8f0;border-top:none;border-radius:0 0 12px 12px">
            <table style="width:100%;border-collapse:collapse;font-size:13px">
                <thead><tr style="background:#f8fafc;border-bottom:2px solid #e2e8f0">
                    <th style="padding:10px 12px;text-align:left">Empresa</th>
                    <th style="padding:10px 12px;text-align:left">Email</th>
                    <th style="padding:10px 12px;text-align:left">NIF</th>
                    <th style="padding:10px 12px;text-align:left">Administrador</th>
                    <th style="padding:10px 12px;text-align:left">Subvenciones</th>
                    <th style="padding:10px 12px;text-align:center">Punt.</th>
                </tr></thead>
                <tbody>{filas_html}</tbody>
            </table>
        </div>
    </div>"""

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
        return {'ok': True, 'enviados': len(leads)}
    except smtplib.SMTPAuthenticationError:
        return JSONResponse({'error': 'Error de autenticación SMTP'}, status_code=500)
    except Exception as e:
        return JSONResponse({'error': str(e)}, status_code=500)


if __name__ == '__main__':
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
