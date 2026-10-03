import streamlit as st
from streamlit_paste_button import paste_image_button
from io import BytesIO
import gspread
from google import genai
import time
import json
import os
import tempfile
from datetime import datetime

# --- CONFIGURACIÓN INICIAL ---
st.set_page_config(page_title="Gestor de Gastos", page_icon="💸", layout="wide")

GEMINI_API_KEY = st.secrets["GEMINI_API_KEY"]
cliente_ai = genai.Client(api_key=GEMINI_API_KEY)

gc = gspread.service_account_from_dict(st.secrets["gcp_service_account"])
archivo = gc.open("Gastos")
hoja_recepcion = archivo.worksheet("Apple Pay")
hoja_visual = archivo.worksheet("2026")

CATEGORIAS_PERMITIDAS = [
    "OXXO", "Comida", "Cafetería", "Farmacia", 
    "Gasolina", "Super", "Cine", "Helado"
]

# --- FUNCIONES NÚCLEO ---

def limpiar_monto(valor):
    """
    Convierte cualquier formato de moneda (con comas, puntos, signos $) 
    a un número matemático puro para que Google Sheets lo pueda sumar.
    """
    if isinstance(valor, (int, float)):
        return float(valor)
        
    texto = str(valor).replace("$", "").replace("'", "").replace(" ", "").strip()
    
    # Caso 1: Tiene comas y puntos (ej. 1,000.50 o 1.000,50)
    if "," in texto and "." in texto:
        if texto.rfind(",") > texto.rfind("."):
            texto = texto.replace(".", "").replace(",", ".")
        else:
            texto = texto.replace(",", "")
            
    # Caso 2: Solo tiene coma (ej. 150,50 o 1,500)
    elif "," in texto:
        partes = texto.split(",")
        if len(partes[-1]) in [1, 2]: 
            texto = texto.replace(",", ".")
        else:
            texto = texto.replace(",", "")
            
    try:
        return float(texto)
    except ValueError:
        return valor

def extraer_gastos_de_documento(archivo_bytes, mime_type, instrucciones=""):
    prompt = """
    Analiza este estado de cuenta o ticket. Extrae todos los gastos y devuélvelos en formato JSON estricto.
    El JSON debe ser una lista de diccionarios con las llaves: "fecha" (formato DD/MM/YY), "comercio" (nombre limpio), "monto" (solo número sin símbolos).
    Ignora depósitos, pagos de tarjeta o abonos, solo quiero los gastos/compras.
    """
    
    if instrucciones:
        prompt += f"\nINSTRUCCIONES MUY IMPORTANTES DEL USUARIO: {instrucciones}\nDebes cumplir estas instrucciones estrictamente al filtrar o procesar los datos."
        
    prompt += '\nEjemplo de salida esperada: [{"fecha": "23/09/26", "comercio": "Starbucks", "monto": 150.50}]'
    
    ext = ".pdf" if "pdf" in mime_type else ".jpg"
    with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as temp_file:
        temp_file.write(archivo_bytes)
        temp_path = temp_file.name

    try:
        archivo_gemini = cliente_ai.files.upload(file=temp_path)
        
        mensaje_espera = st.empty()
        mensaje_espera.info("Documento subido. Esperando a que Google termine de prepararlo...")
        
        while True:
            archivo_gemini = cliente_ai.files.get(name=archivo_gemini.name)
            estado = str(archivo_gemini.state).upper()
            
            if "ACTIVE" in estado:
                mensaje_espera.success("¡Documento listo! Analizando gastos...")
                break
            elif "FAILED" in estado:
                mensaje_espera.error("Google falló al intentar leer este archivo.")
                return []
                
            time.sleep(2) 
            
        max_reintentos = 3
        resultado = []
        
        for intento in range(max_reintentos):
            try:
                response = cliente_ai.models.generate_content(
                    model='gemini-3.5-flash-lite',
                    contents=[archivo_gemini, prompt]
                )
                texto_json = response.text.replace("```json", "").replace("```", "").strip()
                resultado = json.loads(texto_json)
                break
                
            except Exception as e:
                error_msg = str(e)
                if "503" in error_msg or "UNAVAILABLE" in error_msg or "429" in error_msg:
                    if intento < max_reintentos - 1:
                        time.sleep(30)
                        continue
                st.error(f"Error en la IA: {error_msg}")
                break
        
        try:
            cliente_ai.files.delete(name=archivo_gemini.name)
        except:
            pass
            
        mensaje_espera.empty() 
        return resultado
        
    except Exception as e:
        st.error(f"Error general: {e}")
        return []
        
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)

def clasificar_gastos_en_lote(lista_comercios):
    prompt = f"""
    Actúa como un categorizador financiero automático.
    Clasifica esta lista de comercios intentando asignar a cada uno una de estas categorías principales:
    {', '.join(CATEGORIAS_PERMITIDAS)}
    
    Instrucciones obligatorias:
    1. Si el texto dice o contiene "farmacia", devuelve 'Farmacia'.
    2. Si contiene "oxxo", devuelve 'OXXO'.
    3. Si contiene "cine", devuelve 'Cine'.
    4. Si contiene "gasolina" o "gas", devuelve 'Gasolina'.
    5. Si contiene "super", "walmart", "heb", devuelve 'Super'.
    6. REGLA DE ORO: Si el comercio no encaja claramente en ninguna de las categorías de arriba, devuelve el NOMBRE ORIGINAL del comercio tal cual como te lo envié. NO pongas 'Comida' por defecto.
    
    Comercios a clasificar:
    {json.dumps(lista_comercios)}
    
    Responde ÚNICAMENTE con un arreglo JSON válido, donde cada objeto tenga las llaves "comercio" y "categoria".
    """
    try:
        response = cliente_ai.models.generate_content(
            model='gemini-3.5-flash-lite',
            contents=prompt
        )
        texto_json = response.text.replace("```json", "").replace("```", "").strip()
        return json.loads(texto_json)
    except Exception as e:
        st.error(f"Error al clasificar el lote: {e}")
        return []

def procesar_pendientes():
    registros = hoja_recepcion.get_all_values()

    filas_a_procesar = []

    for index, fila in enumerate(registros[1:], start=2):
        if len(fila) < 4 or fila[3] != "Listo":
            if fila[0] and fila[1]:
                filas_a_procesar.append({
                    "index": index,
                    "fecha": fila[0],
                    "comercio": fila[1],
                    "monto": fila[2]
                })

    if not filas_a_procesar:
        return 0

    status_text = st.empty()
    status_text.info(
        f"Enviando un paquete de {len(filas_a_procesar)} gastos a la IA..."
    )

    # Obtener todos los comercios pendientes
    nombres_comercios = [
        item["comercio"]
        for item in filas_a_procesar
    ]

    # Clasificarlos en un solo lote
    resultados_ia = clasificar_gastos_en_lote(nombres_comercios)

    mapa_categorias = {
        r.get("comercio", ""):
        r.get("categoria", r.get("comercio", ""))
        for r in resultados_ia
    }

    procesados = 0
    progress_bar = st.progress(0)

    for i, item in enumerate(filas_a_procesar):

        comercio = item["comercio"]
        categoria = mapa_categorias.get(comercio, comercio)

        status_text.text(
            f"Acomodando en matriz visual: "
            f"{comercio} -> {categoria}"
        )

        # ---------------------------------------
        # 1. OBTENER EL MES
        # ---------------------------------------

        try:
            if "-" in item["fecha"]:
                mes = int(item["fecha"].split("-")[1])
            else:
                mes = int(item["fecha"].split("/")[1])

        except Exception:
            continue

        # ---------------------------------------
        # 2. COLUMNAS CORRESPONDIENTES A CADA MES
        # ---------------------------------------

        columnas_mes = {
            1: 1,    # Enero       A
            2: 4,    # Febrero     D
            3: 7,    # Marzo       G
            4: 10,   # Abril       J
            5: 13,   # Mayo        M
            6: 16,   # Junio       P
            7: 19,   # Julio       S
            8: 22,   # Agosto      V
            9: 25,   # Septiembre  Y
            10: 28,  # Octubre     AB
            11: 31,  # Noviembre   AE
            12: 34   # Diciembre   AH
        }

        col_inicial = columnas_mes.get(mes)

        if not col_inicial:
            continue

        # ---------------------------------------
        # 3. BUSCAR CUÁNTOS GASTOS HAY EN EL MES
        # ---------------------------------------
        #
        # IMPORTANTE:
        # Aquí ya NO convertimos AB -> A accidentalmente.
        # Se trabaja directamente con el número de columna.
        #

        celdas_mes = hoja_visual.range(
            3,
            col_inicial,
            38,
            col_inicial
        )

        filas_ocupadas = len([
            celda
            for celda in celdas_mes
            if celda.value
        ])

        fila_destino = 3 + filas_ocupadas

        # ---------------------------------------
        # 4. LIMPIAR MONTO
        # ---------------------------------------

        monto_numerico = limpiar_monto(item["monto"])

        # ---------------------------------------
        # 5. ESCRIBIR EN LA HOJA 2026
        # ---------------------------------------

        if fila_destino <= 38:

            # Crear rango exacto de 3 columnas:
            # Categoría | Fecha | Importe

            celda_inicio = gspread.utils.rowcol_to_a1(
                fila_destino,
                col_inicial
            )

            celda_fin = gspread.utils.rowcol_to_a1(
                fila_destino,
                col_inicial + 2
            )

            rango_destino = f"{celda_inicio}:{celda_fin}"

            hoja_visual.update(
                values=[[
                    categoria,
                    item["fecha"],
                    monto_numerico
                ]],
                range_name=rango_destino,
                value_input_option="USER_ENTERED"
            )

            # ---------------------------------------
            # 6. MARCAR COMO LISTO
            # ---------------------------------------

            hoja_recepcion.update_cell(
                item["index"],
                4,
                "Listo"
            )

            procesados += 1

            print(
                f"Guardado: {comercio} | "
                f"Mes: {mes} | "
                f"Columna: {col_inicial} | "
                f"Fila: {fila_destino} | "
                f"Rango: {rango_destino}"
            )

            time.sleep(1)

        # Actualizar barra de progreso
        progress_bar.progress(
            min(
                (i + 1) / len(filas_a_procesar),
                1.0
            )
        )

    status_text.text("¡Procesamiento finalizado!")

    return procesados

# --- INTERFAZ VISUAL ---

from finanzas import render_finanzas

st.markdown("""<style>
  .stApp {background: radial-gradient(ellipse at 80% -15%, #12382f 0, #08111f 44%, #08111f 100%); color:#eef7f5}
  .block-container {max-width:1180px; padding-top:2rem; padding-bottom:3rem}
  h1, h2, h3 {letter-spacing:-.025em}
  [data-testid="stMetric"] {background:#112338; border:1px solid #254159; border-radius:16px; padding:18px}
  [data-testid="stMetricValue"] {color:#f8fafc}
  [data-baseweb="tab-list"] {gap:.35rem; flex-wrap:wrap}
  [data-baseweb="tab"] {border-radius:12px; padding:.65rem 1rem; background:#13243a}
  [data-testid="stForm"], [data-testid="stFileUploader"] {border-radius:18px; border:1px solid #294158; background:#101e30; padding:1rem}
  .stButton button[kind="primary"], .stFormSubmitButton button[kind="primary"] {border-radius:12px; font-weight:700}
  @media(max-width:760px) {.block-container {padding:1rem .75rem 2rem}}
</style>""", unsafe_allow_html=True)

st.title("💸 Mi Panel Financiero")
st.markdown("Gestión inteligente con IA y sincronización en tiempo real")
st.divider()

tab1, tab2, tab3, tab4 = st.tabs(["✍️ Ingreso Manual", "📄 Subir Documento", "🚀 Ejecutar Ahora", "📊 Finanzas"])

with tab1:
    st.subheader("Agregar un gasto rápido")
    with st.form("manual_form"):
        col1, col2 = st.columns(2)
        fecha_input = col1.date_input("Fecha", datetime.today())
        monto_input = col2.number_input("Monto ($)", min_value=0.0, format="%.2f")
        comercio_input = st.text_input("Comercio / Descripción")
        
        # Botón primario verde
        submit_btn = st.form_submit_button("Guardar en Fila de Espera", type="primary")
        
        if submit_btn and comercio_input:
            fecha_formateada = fecha_input.strftime("%d/%m/%y")
            monto_limpio = limpiar_monto(monto_input)
            
            hoja_recepcion.append_row(
                [fecha_formateada, comercio_input, monto_limpio],
                value_input_option="USER_ENTERED"
            )
            st.success(f"Guardado exitosamente: {comercio_input} por ${monto_limpio}")

with tab2:
    st.subheader("Extraer desde Ticket o Estado de Cuenta")
    st.info("Sube una foto de un ticket o un PDF de tu banco. La inteligencia artificial extraerá y filtrará los datos automáticamente.")
    
    archivo_subido = st.file_uploader("Sube tu archivo", type=["pdf", "png", "jpg", "jpeg"])
    
    instrucciones_usuario = st.text_area(
        "Instrucciones especiales para la IA (Opcional)", 
        placeholder="Ej. Solo extrae los gastos del mes de septiembre..."
    )
    
    if archivo_subido is not None:
        # Botón primario verde
        if st.button("Analizar Documento", type="primary"):
            with st.spinner("La IA está leyendo y filtrando el documento..."):
                bytes_data = archivo_subido.getvalue()
                
                if archivo_subido.name.endswith(".pdf"):
                    mime = "application/pdf"
                elif archivo_subido.name.endswith(".png"):
                    mime = "image/png"
                else:
                    mime = "image/jpeg"
                
                gastos_extraidos = extraer_gastos_de_documento(bytes_data, mime, instrucciones_usuario)
                
                if gastos_extraidos:
                    st.write(f"**Se encontraron y filtraron {len(gastos_extraidos)} gastos:**")
                    for g in gastos_extraidos:
                        monto_limpio = limpiar_monto(g['monto'])
                        st.write(f"- 📅 {g['fecha']} | 🏢 {g['comercio']} | 💵 ${monto_limpio}")
                        
                        hoja_recepcion.append_row(
                            [g['fecha'], g['comercio'], monto_limpio],
                            value_input_option="USER_ENTERED"
                        )
                    st.success("¡Todos los gastos se agregaron a la fila de espera correctamente!")
                else:
                    st.warning("No se encontraron gastos o no coincidieron con tus instrucciones.")

with tab3:
    st.subheader("Acomodar gastos pendientes")
    st.write("Presiona este botón para que la IA clasifique todos los gastos de la fila de espera en un solo bloque.")
    
    # Botón primario verde
    if st.button("🚀 Procesar Todo Ahora", type="primary"):
        with st.spinner("Despertando a la IA y acomodando celdas en el panel visual..."):
            total = procesar_pendientes()
            if total > 0:
                st.success(f"¡Listo! Se clasificaron y acomodaron {total} gastos exitosamente.")
                st.balloons()
            else:
                st.info("No hay gastos nuevos por procesar en la fila de espera.")

with tab4:
    render_finanzas(archivo, cliente_ai)
