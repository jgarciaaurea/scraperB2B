# Leads B2B — Herramienta de Prospección

Herramienta interna para buscar, enriquecer y analizar empresas españolas como leads B2B. Extrae automáticamente email, NIF, administrador y subvenciones recibidas para cada empresa.

---

## Qué hace

Dado un sector y una ciudad, el sistema:

1. **Busca empresas** en Google Places y las guarda en la base de datos
2. **Enriquece cada empresa** visitando su web para extraer email, NIF/CIF y LinkedIn
3. **Busca el administrador** en el Registro Mercantil (LibreBORME, InfoEmpresa, Einforma)
4. **Consulta subvenciones** recibidas en la Base de Datos Nacional de Subvenciones (BDNS)

Todo el proceso corre en segundo plano de forma automática. Los resultados aparecen en tiempo real en el panel de tarjetas.

---

## Requisitos

- Python 3.10 o superior
- Una API Key de Google Places (Google Cloud Console)

---

## Instalación y puesta en marcha

### 1. Clona o descarga el proyecto

```bash
cd scraperB2B-main
```

### 2. Instala las dependencias

```bash
pip install -r requirements.txt
```

### 3. Crea el archivo `.env`

Crea un archivo llamado `.env` en la raíz del proyecto con este contenido:

```
GOOGLE_PLACES_API_KEY=TU_API_KEY_AQUI
```

Para obtener la API Key:
- Ve a [Google Cloud Console](https://console.cloud.google.com)
- Activa la API **Places API (New)**
- Crea una credencial de tipo "API Key"

### 4. Arranca el servidor

```bash
python app.py
```

### 5. Abre el navegador

```
http://127.0.0.1:5000
```

---

## Uso

1. Escribe un **sector** (ej: `Clínicas dentales`, `Talleres mecánicos`) y una **ciudad** (ej: `Sevilla`)
2. Pulsa **Buscar empresas**
3. Las tarjetas aparecen de inmediato y se van rellenando solas con los datos enriquecidos

### Indicadores de estado en las tarjetas

| Color | Significado |
|-------|-------------|
| 🟠 Naranja + rueda girando | En proceso (buscando datos) |
| 🟢 Verde + ✓ | Completado con datos |
| 🔴 Rojo + ✗ | Completado pero sin datos encontrados |

### Subvenciones

Si la empresa tiene NIF y ha recibido subvenciones públicas, aparece un badge **amarillo** con el número de ayudas y el importe total acumulado consultado directamente desde la API oficial del BDNS.

---

## Estructura del proyecto

```
app.py                    # Servidor Flask, rutas y worker de fases
fase1_prospeccion.py      # Búsqueda en Google Places
fase2_enriquecimiento.py  # Scraping web (email, NIF, LinkedIn)
fase3_borme.py            # Administrador via Registro Mercantil
fase4_subvenciones.py     # Subvenciones via API BDNS
templates/index.html      # Interfaz de usuario (Tailwind CSS)
leads.db                  # Base de datos SQLite (se crea automáticamente)
.env                      # API Keys (no incluido en el repo)
```

---

## Fuentes de datos utilizadas

| Dato | Fuente |
|------|--------|
| Empresas y teléfono | Google Places API |
| Email y NIF | Web scraping de la propia empresa + InfoEmpresa.com |
| LinkedIn | Web scraping de la propia empresa |
| Administrador | LibreBORME → InfoEmpresa.com → Einforma.com |
| Subvenciones | API oficial BDNS (infosubvenciones.es) |

---

## Notas

- La base de datos se crea automáticamente en `leads.db` al arrancar
- Si paras el servidor y lo reinicias, los leads existentes se mantienen y el procesamiento continúa donde lo dejó
- Para vaciar todos los datos, usa el botón **Vaciar** en la interfaz
