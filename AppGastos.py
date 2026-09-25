import streamlit as st
import gspread
from google import genai
import time
import json
import os
import tempfile
from datetime import datetime

# --- CONFIGURACIÓN INICIAL ---
st.set_page_config(page_title="Gestor de Gastos", page_icon="💸", layout="centered")

GEMINI_API_KEY = st.secrets["GEMINI_API_KEY"]
cliente_ai = genai.Client(api_key=GEMINI_API_KEY)

gc = gspread.service_account_from_dict(st.secrets["gcp_service_account"])
archivo = gc.open("Gastos")
hoja_recepcion = archivo.worksheet("Apple Pay")
hoja_visual = archivo.worksheet("2026")

# Lista limpia y simplificada de categorías
CATEGORIAS_PERMITIDAS = [
    "OXXO", "Comida", "Cafetería", "Farmacia", 
    "Gasolina", "Super", "Cine", "Helado"
]

# --- FUNCIONES NÚCLEO ---

def extraer_gastos_de_documento(archivo_bytes, mime_type, instrucciones=""):
    """Usa Gemini mediante la File API con espera de procesamiento activa y el modelo Lite"""
    prompt = """
    Analiza este estado de cuenta o ticket. Extrae todos los gastos y devuélvelos en formato JSON estricto.
    El JSON debe ser una lista de diccionarios con las llaves: "fecha" (formato DD/MM/YY), "comercio" (nombre limpio), "monto" (solo número sin símbolos).
    Ignora depósitos, pagos de tarjeta o abonos, solo quiero los gastos/compras.
    """
    
    if instrucciones:
        prompt += f"\nINSTRUCCIONES MUY IMPORTANTES DEL USUARIO: {instrucciones}\nDebes cumplir estas instrucciones estrictamente al filtrar o procesar los datos."
        
    prompt += '\nEjemplo de salida esperada: [{"fecha": "23/09/26", "comercio": "Starbucks", "monto": 150}]'
    
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
                    model='gemini-3.5-flash-lite', # Modelo ligero con 500 peticiones diarias
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
    """Envía todos los comercios en una sola petición para evitar límites de velocidad."""
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
    Ejemplo de salida:
    [
        {{"comercio": "Oxxo Naranjos", "categoria": "OXXO"}},
        {{"comercio": "Zara", "categoria": "Zara"}}
    ]
    """
    try:
        response = cliente_ai.models.generate_content(
            model='gemini-3.5-flash-lite', # Modelo ligero con 500 peticiones diarias
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
    status_text.info(f"Enviando un paquete de {len(filas_a_procesar)} gastos a la IA...")
    
    nombres_comercios = [item["comercio"] for item in filas_a_procesar]
    resultados_ia = clasificar_gastos_en_lote(nombres_comercios)
    
    # Mapeo: Si la IA falla en clasificar, usa el nombre del comercio original.
    mapa_categorias = {r.get("comercio", ""): r.get("categoria", r.get("comercio", "")) for r in resultados_ia}
    
    procesados = 0
    progress_bar = st.progress(0)
    
    # 3. Acomodar los resultados en Google Sheets
    for i, item in enumerate(filas_a_procesar):
        comercio = item["comercio"]
        categoria = mapa_categorias.get(comercio, comercio)
        
        status_text.text(f"Acomodando en matriz visual: {comercio} -> {categoria}")
        
        try:
            mes = int(item["fecha"].split('-')[1]) if '-' in item["fecha"] else int(item["fecha"].split('/')[1])
        except:
            continue
            
        columnas_mes = {1: 1, 2: 4, 3: 7, 4: 10, 5: 13, 6: 16, 7: 19, 8: 22, 9: 25, 10: 28, 11: 31, 12: 34}
        col_inicial = columnas_mes.get(mes)
        
        if col_inicial:
            col_letra = gspread.utils.rowcol_to_a1(1, col_inicial)[0]
            valores_mes = hoja_visual.get(f"{col_letra}3:{col_letra}38")
            fila_destino = 3 + len([v for v in valores_mes if v])
            
            try:
                # Quitamos signos de $, comas y apóstrofos rebeldes
                monto_limpio = str(item["monto"]).replace("$", "").replace(",", "").replace("'", "").strip()
                monto_numerico = float(monto_limpio)
            except ValueError:
                monto_numerico = item["monto"] # Respaldo por si hay un error extraño
            
            if fila_destino <= 38:
                hoja_visual.update(
                    values=[[categoria, item["fecha"], monto_numerico]],
                    range_name=f"{col_letra}{fila_destino}",
                    value_input_option="USER_ENTERED" 
                )
                hoja_recepcion.update_cell(item["index"], 4, "Listo")
                procesados += 1
        
        # Freno exclusivo para respetar los límites de la API de Google Sheets
        time.sleep(1)
        progress_bar.progress(min((i + 1) / len(filas_a_procesar), 1.0))
        
    status_text.text("¡Procesamiento finalizado!")
    return procesados

# --- INTERFAZ VISUAL ---

# 1. Inyección de CSS (Diseño Tech-Finance: Azul Marino y Verde Esmeralda)
st.markdown("""
<style>
    /* Fondo general */
    .stApp {
        background-color: #F8FAFC;
    }
    
    /* Tipografía y encabezados */
    h1, h2, h3 {
        color: #0F172A !important;
        font-family: 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
        font-weight: 700;
    }
    
    /* Diseño de las Pestañas (Tabs) */
    .stTabs [data-baseweb="tab-list"] {
        gap: 8px;
        padding-bottom: 5px;
    }
    .stTabs [data-baseweb="tab"] {
        background-color: #E2E8F0;
        border-radius: 8px 8px 0px 0px;
        padding: 12px 24px;
        color: #475569;
        font-weight: 600;
    }
    .stTabs [aria-selected="true"] {
        background-color: #1E293B !important; /* Azul marino */
        color: #FFFFFF !important;
        border-bottom: 4px solid #10B981 !important; /* Acento verde */
    }
    
    /* Botones primarios */
    .stButton>button[kind="primary"] {
        background-color: #10B981;
        color: white;
        border-radius: 8px;
        border: none;
        font-weight: 700;
        padding: 0.5rem 1rem;
        transition: all 0.2s ease-in-out;
    }
    .stButton>button[kind="primary"]:hover {
        background-color: #059669;
        box-shadow: 0 4px 12px rgba(16, 185, 129, 0.3);
    }
    
    /* Botones secundarios */
    .stButton>button[kind="secondary"] {
        border: 2px solid #1E293B;
        color: #1E293B;
        border-radius: 8px;
        font-weight: 600;
        transition: all 0.2s ease-in-out;
    }
    .stButton>button[kind="secondary"]:hover {
        background-color: #1E293B;
        color: white;
    }
    
    /* Contenedores y formularios tipo tarjeta */
    div[data-testid="stForm"] {
        background-color: #FFFFFF;
        border-radius: 12px;
        padding: 24px;
        box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.05), 0 2px 4px -1px rgba(0, 0, 0, 0.03);
        border: 1px solid #F1F5F9;
    }
    
    /* Cajas de alerta (Info y Success) */
    div[data-testid="stInfo"] {
        background-color: #F0FDFA;
        border-left: 5px solid #0D9488;
        color: #115E59;
    }
    div[data-testid="stSuccess"] {
        background-color: #ECFDF5;
        border-left: 5px solid #10B981;
        color: #065F46;
    }
</style>
""", unsafe_allow_html=True)

# 2. Encabezado principal personalizado
st.markdown("<h1><span style='color: #10B981;'>💸</span> Mi Panel Financiero</h1>", unsafe_allow_html=True)
st.markdown("<p style='color: #64748B; font-size: 1.1rem; margin-bottom: 2rem;'>Gestión inteligente con IA y sincronización en tiempo real</p>", unsafe_allow_html=True)

tab1, tab2, tab3 = st.tabs(["✍️ Ingreso Manual", "📄 Subir Documento", "🚀 Ejecutar Ahora"])

with tab1:
    st.subheader("Agregar un gasto rápido")
    with st.form("manual_form"):
        col1, col2 = st.columns(2)
        fecha_input = col1.date_input("Fecha", datetime.today())
        monto_input = col2.number_input("Monto ($)", min_value=0.0, format="%.2f")
        comercio_input = st.text_input("Comercio / Descripción")
        submit_btn = st.form_submit_button("Guardar en Fila de Espera")
        
        if submit_btn and comercio_input:
            fecha_formateada = fecha_input.strftime("%d/%m/%y")
            hoja_recepcion.append_row(
                [fecha_formateada, comercio_input, monto_input],
                value_input_option="USER_ENTERED"
            )
            st.success(f"Guardado exitosamente: {comercio_input} por ${monto_input}")

with tab2:
    st.subheader("Extraer desde Ticket o Estado de Cuenta")
    st.info("Sube una foto de un ticket o un PDF de tu banco. La inteligencia artificial extraerá y filtrará los datos automáticamente.")
    
    archivo_subido = st.file_uploader("Sube tu archivo", type=["pdf", "png", "jpg", "jpeg"])
    
    instrucciones_usuario = st.text_area(
        "Instrucciones especiales para la IA (Opcional)", 
        placeholder="Ej. Solo extrae los gastos del mes de septiembre, e ignora los retiros en efectivo..."
    )
    
    if archivo_subido is not None:
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
                        st.write(f"- 📅 {g['fecha']} | 🏢 {g['comercio']} | 💵 ${g['monto']}")
                        
                        try:
                            monto_limpio = float(str(g['monto']).replace("$", "").replace(",", "").replace("'", "").strip())
                        except ValueError:
                            monto_limpio = g['monto']
                            
                        hoja_recepcion.append_row(
                            [g['fecha'], g['comercio'], monto_limpio],
                            value_input_option="USER_ENTERED"
                        )
                    st.success("¡Todos los gastos se agregaron a la fila de espera correctamente!")
                else:
                    st.warning("No se encontraron gastos o no coincidieron con tus instrucciones.")

with tab3:
    st.subheader("Acomodar gastos pendientes")
    st.write("Presiona este botón para que la IA clasifique todos los gastos de la fila de espera en un solo bloque y los envíe a tu matriz de Google Sheets.")
    
    if st.button("🚀 Procesar Todo Ahora", type="primary"):
        with st.spinner("Despertando a la IA y acomodando celdas en el panel visual..."):
            total = procesar_pendientes()
            if total > 0:
                st.success(f"¡Listo! Se clasificaron y acomodaron {total} gastos exitosamente.")
                st.balloons()
            else:
                st.info("No hay gastos nuevos por procesar en la fila de espera.")
