# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the app

```bash
py app.py
# Abre http://127.0.0.1:5000
```

Requires a `.env` file with `GOOGLE_PLACES_API_KEY`.

## Architecture

Flask app (`app.py`) con SQLite (`leads.db`). Un hilo de fondo (`worker_enriquecimiento`) ejecuta las 5 fases en secuencia automáticamente al añadir leads. Procesamiento paralelo con `ThreadPoolExecutor`.

### Pipeline de fases

```
PENDIENTE_F2 → PENDIENTE_F3 → PENDIENTE_F4 → COMPLETADO_F4
```

| Fase | Archivo | Qué hace |
|------|---------|----------|
| F1 | `fase1_prospeccion.py` | Google Places API → leads básicos + Google rating/Maps. Guarda estado `PENDIENTE_F2` |
| F2 | `fase2_enriquecimiento.py` | Scraping web: email, NIF, LinkedIn, Facebook, Trustpilot. Fallback NIF: InfoEmpresa → DuckDuckGo. Usa **Playwright** para webs JS-heavy |
| F3 | `fase3_borme.py` | Administrador vía LibreBORME → fallback InfoEmpresa → fallback Einforma |
| F4 | `fase4_subvenciones.py` | Subvenciones BDNS vía API oficial (`/api/concesiones/busqueda?nifCif=`) |
| F5 | `fase5_cualificacion.py` | Puntuación de relevancia extremeña 0–10 (`puntuacion` + `puntuacion_detalle`) |

### Schema de base de datos

```
b2b_leads: id, nombre_empresa, direccion, telefono, sitio_web,
           email, linkedin_empresa, nif, administrador, estado_proceso, actualizado_en,
           subvenciones_count, subvenciones_importe, subvenciones_resumen,
           facebook_url, google_maps_url, google_rating, google_reviews_count, trustpilot_url,
           puntuacion, puntuacion_detalle,
           estado_contacto, notas

b2b_busquedas: id, sector, ubicacion, leads_encontrados, buscado_en
```

**Importante:** Las columnas se añadieron incrementalmente con `ALTER TABLE` en `inicializar_base_de_datos()`. Nunca usar `SELECT *` — siempre nombrar columnas explícitamente.

### Índices de columna en el template (Jinja2)

La función `obtener_leads_paginados()` devuelve tuplas en este orden:

`lead[0]`=id, `[1]`=nombre, `[2]`=direccion, `[3]`=telefono, `[4]`=sitio_web, `[5]`=email, `[6]`=linkedin, `[7]`=nif, `[8]`=administrador, `[9]`=estado_proceso, `[10]`=actualizado_en, `[11]`=subvenciones_count, `[12]`=subvenciones_importe, `[13]`=subvenciones_resumen, `[14]`=facebook_url, `[15]`=google_maps_url, `[16]`=google_rating, `[17]`=google_reviews_count, `[18]`=trustpilot_url, `[19]`=puntuacion, `[20]`=puntuacion_detalle, `[21]`=estado_contacto, `[22]`=notas

### Paginación y filtros

La ruta `/` acepta query params: `?page=N&email=1&nif=1&subv=1&alta=1&q=texto`. Los filtros son server-side — se pasan a `obtener_leads_paginados()`. El template recibe `toggle_url` y `page_url` (funciones Python) para generar URLs de navegación.

### Frontend

Tailwind CSS v4 CDN. Las clases responsivas `md:` y `lg:` **no funcionan** con esta versión CDN — usar CSS explícito en `<style>` con media queries (ver `#leads-grid` en el template).

El polling de estado (`/estado`) recarga la página automáticamente cuando cambia el hash de estados mientras hay leads procesando.

### Rutas principales

| Ruta | Método | Qué hace |
|------|--------|----------|
| `/` | GET | Lista paginada con filtros y estadísticas |
| `/buscar` | POST | Nueva búsqueda (sector + ubicación) |
| `/buscar-rapido` | GET | Repetir búsqueda del historial |
| `/estado` | GET | JSON con conteo de pendientes/completados |
| `/lead/<id>/contacto` | POST | Actualizar estado CRM y notas |
| `/exportar` | GET | CSV con todos los campos incluyendo CRM |
| `/eliminar/<id>` | GET | Eliminar lead |
| `/eliminar_todo` | GET | Vaciar base de datos |
| `/investigar_borme` | GET | Reintento manual F3 |
