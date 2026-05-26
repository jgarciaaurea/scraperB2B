from flask import Flask, render_template, request, redirect, url_for
import sqlite3
# Importamos la función de búsqueda que pulimos en el archivo anterior
from fase1_prospeccion import buscar_empresas_en_profundidad, guardar_leads_en_sqlite, inicializar_base_de_datos

app = Flask(__name__)
DB_FILE = "leads.db"

def obtener_todos_los_leads():
    """Trae todos los leads guardados en SQLite para mostrarlos en la tabla."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    # Traemos los leads ordenados por el último añadido
    cursor.execute("SELECT * FROM b2b_leads ORDER BY id DESC")
    leads = cursor.fetchall()
    conn.close()
    return leads

@app.route('/')
def index():
    # Nos aseguramos de que la tabla exista al entrar
    inicializar_base_de_datos()
    # Leemos la DB para pintar la tabla
    leads = obtener_todos_los_leads()
    # Si venimos de una búsqueda, podemos capturar un mensaje de éxito opcional
    mensaje = request.args.get('mensaje')
    return render_template('index.html', leads=leads, mensaje=mensaje)

@app.route('/buscar', methods=['POST'])
def buscar():
    # Recogemos lo que el usuario ha escrito en la web
    sector = request.form.get('sector')
    ubicacion = request.form.get('ubicacion')
    
    # Ejecutamos la Fase 1 en segundo plano
    print(f" Web interactiva: Buscando '{sector}' en '{ubicacion}'...")
    resultados = buscar_empresas_en_profundidad(sector, ubicacion, max_resultados=40)
    
    # Guardamos en la base de datos local
    guardados = guardar_leads_en_sqlite(resultados)
    
    mensaje_exito = f"¡Búsqueda completada! Se encontraron {len(resultados)} empresas con web y se añadieron {guardados} nuevas a la base de datos."
    
    # Redirigimos a la página principal mostrando los nuevos datos
    return redirect(url_for('index', mensaje=mensaje_exito))

if __name__ == '__main__':
    # Lanzamos el servidor local en modo depuración (debug)
    app.run(debug=True, port=5000)