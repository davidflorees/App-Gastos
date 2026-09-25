import streamlit as st
import gspread
from google import genai
import time
import json
import os
import tempfile
from datetime import datetime

# --- CONFIGURACIÓN INICIAL ---
st.set_page_config(page_title="Gestor de Gastos", page_icon="💸", layout="wide", initial_sidebar_state="collapsed")

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
    status_text.info(f"Enviando un paquete de {len(filas_a_procesar)} gastos a la IA...")
    
    nombres_comercios = [item["comercio"] for item in filas_a_procesar]
    resultados_ia = clasificar_gastos_en_lote(nombres_comercios)
    mapa_categorias = {r.get("comercio", ""): r.get("categoria", r.get("comercio", "")) for r in resultados_ia}
    
    procesados = 0
    progress_bar = st.progress(0)
    
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
            
            monto_numerico = limpiar_monto(item["monto"])
            
            if fila_destino <= 38:
                hoja_visual.update(
                    values=[[categoria, item["fecha"], monto_numerico]],
                    range_name=f"{col_letra}{fila_destino}",
                    value_input_option="USER_ENTERED"
                )
                hoja_recepcion.update_cell(item["index"], 4, "Listo")
                procesados += 1
        
        time.sleep(1)
        progress_bar.progress(min((i + 1) / len(filas_a_procesar), 1.0))
        
    status_text.text("¡Procesamiento finalizado!")
    return procesados



# --- ESTILOS VISUALES ---
st.markdown("""
<style>
    /* ---------- Base ---------- */
    :root {
        --bg: #08111F;
        --surface: #0F1B2D;
        --surface-2: #142238;
        --border: rgba(148, 163, 184, 0.16);
        --text: #F8FAFC;
        --muted: #94A3B8;
        --green: #22C55E;
        --green-soft: rgba(34, 197, 94, 0.12);
        --cyan-soft: rgba(34, 211, 238, 0.10);
    }

    .stApp {
        background:
            radial-gradient(circle at 18% 0%, rgba(34, 197, 94, 0.10), transparent 28%),
            radial-gradient(circle at 82% 6%, rgba(56, 189, 248, 0.08), transparent 24%),
            #08111F;
    }

    .block-container {
        max-width: 1120px;
        padding-top: 2.2rem;
        padding-bottom: 3.5rem;
    }

    header[data-testid="stHeader"] {
        background: transparent;
    }

    #MainMenu, footer {
        visibility: hidden;
    }

    /* ---------- Hero ---------- */
    .finance-hero {
        position: relative;
        overflow: hidden;
        padding: 2.1rem 2.2rem 1.9rem 2.2rem;
        margin-bottom: 1.35rem;
        border: 1px solid var(--border);
        border-radius: 24px;
        background: linear-gradient(135deg, rgba(15, 27, 45, 0.98), rgba(12, 23, 39, 0.92));
        box-shadow: 0 24px 60px rgba(0, 0, 0, 0.24);
    }

    .finance-hero::after {
        content: "";
        position: absolute;
        width: 260px;
        height: 260px;
        right: -90px;
        top: -120px;
        border-radius: 50%;
        background: rgba(34, 197, 94, 0.10);
        filter: blur(4px);
    }

    .hero-badge {
        display: inline-flex;
        align-items: center;
        gap: 0.45rem;
        padding: 0.42rem 0.7rem;
        border-radius: 999px;
        border: 1px solid rgba(34, 197, 94, 0.20);
        background: var(--green-soft);
        color: #86EFAC;
        font-size: 0.74rem;
        font-weight: 700;
        letter-spacing: 0.05em;
        text-transform: uppercase;
        margin-bottom: 1rem;
    }

    .finance-hero h1 {
        margin: 0;
        color: var(--text);
        font-size: clamp(2rem, 5vw, 3.1rem);
        line-height: 1.05;
        letter-spacing: -0.045em;
        font-weight: 800;
        max-width: 720px;
    }

    .finance-hero p {
        color: #A8B6C8;
        font-size: 1rem;
        margin: 0.85rem 0 1.35rem 0;
        max-width: 700px;
    }

    .status-row {
        display: flex;
        flex-wrap: wrap;
        gap: 0.6rem;
        position: relative;
        z-index: 1;
    }

    .status-pill {
        display: inline-flex;
        align-items: center;
        gap: 0.45rem;
        padding: 0.48rem 0.68rem;
        border-radius: 10px;
        color: #CBD5E1;
        font-size: 0.78rem;
        background: rgba(255, 255, 255, 0.035);
        border: 1px solid rgba(148, 163, 184, 0.13);
    }

    .status-dot {
        width: 7px;
        height: 7px;
        border-radius: 50%;
        background: #22C55E;
        box-shadow: 0 0 0 4px rgba(34, 197, 94, 0.10);
    }

    /* ---------- Section intro ---------- */
    .section-kicker {
        display: inline-block;
        color: #86EFAC;
        font-size: 0.76rem;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: .09em;
        margin-bottom: 0.25rem;
    }

    .section-title {
        color: var(--text);
        font-size: 1.25rem;
        font-weight: 750;
        letter-spacing: -0.02em;
        margin: 0 0 0.2rem 0;
    }

    .section-copy {
        color: var(--muted);
        font-size: 0.90rem;
        margin: 0 0 1rem 0;
    }

    .helper-card {
        padding: 0.95rem 1rem;
        margin: 0.5rem 0 1.1rem 0;
        border: 1px solid rgba(56, 189, 248, 0.14);
        border-radius: 14px;
        background: var(--cyan-soft);
        color: #BFDBFE;
        font-size: 0.88rem;
        line-height: 1.55;
    }

    .process-card {
        padding: 1.15rem 1.2rem;
        margin: 0.5rem 0 1.1rem 0;
        border: 1px solid rgba(34, 197, 94, 0.16);
        border-radius: 16px;
        background: linear-gradient(135deg, rgba(34, 197, 94, 0.09), rgba(15, 27, 45, 0.72));
    }

    .process-card strong {
        color: #DCFCE7;
    }

    .process-card span {
        color: #9FB0C4;
        font-size: 0.88rem;
    }

    /* ---------- Tabs ---------- */
    .stTabs [data-baseweb="tab-list"] {
        gap: 0.45rem;
        padding: 0.35rem;
        border-radius: 15px;
        border: 1px solid var(--border);
        background: rgba(15, 27, 45, 0.84);
        margin-bottom: 1.15rem;
    }

    .stTabs [data-baseweb="tab"] {
        height: 44px;
        flex: 1;
        justify-content: center;
        padding: 0 1rem;
        border-radius: 11px;
        color: #94A3B8;
        font-weight: 650;
        border: 0;
    }

    .stTabs [aria-selected="true"] {
        color: #F0FDF4 !important;
        background: rgba(34, 197, 94, 0.13) !important;
    }

    .stTabs [data-baseweb="tab-highlight"] {
        display: none;
    }

    .stTabs [data-baseweb="tab-border"] {
        display: none;
    }

    /* ---------- Inputs ---------- */
    div[data-testid="stForm"] {
        border: 1px solid var(--border);
        border-radius: 18px;
        background: rgba(15, 27, 45, 0.74);
        padding: 1.2rem 1.25rem 0.4rem 1.25rem;
        box-shadow: 0 16px 40px rgba(0,0,0,0.13);
    }

    label, .stTextInput label, .stNumberInput label, .stDateInput label,
    .stTextArea label, .stFileUploader label {
        color: #CBD5E1 !important;
        font-weight: 600 !important;
    }

    .stTextInput input,
    .stNumberInput input,
    .stDateInput input,
    .stTextArea textarea {
        border-radius: 11px !important;
    }

    .stTextInput input:focus,
    .stNumberInput input:focus,
    .stDateInput input:focus,
    .stTextArea textarea:focus {
        border-color: rgba(34, 197, 94, 0.78) !important;
        box-shadow: 0 0 0 1px rgba(34, 197, 94, 0.35) !important;
    }

    [data-testid="stFileUploaderDropzone"] {
        border-radius: 16px;
        border: 1px dashed rgba(148, 163, 184, 0.32);
        background: rgba(15, 27, 45, 0.72);
        padding: 1rem;
    }

    [data-testid="stFileUploaderDropzone"]:hover {
        border-color: rgba(34, 197, 94, 0.55);
        background: rgba(34, 197, 94, 0.045);
    }

    /* ---------- Buttons ---------- */
    .stButton > button,
    .stFormSubmitButton > button {
        min-height: 44px;
        border-radius: 12px;
        font-weight: 750;
        transition: all 0.18s ease;
        box-shadow: none;
    }

    .stButton > button[kind="primary"],
    .stFormSubmitButton > button[kind="primary"] {
        background: linear-gradient(135deg, #22C55E, #16A34A);
        border: 1px solid rgba(134, 239, 172, 0.30);
        color: #04110A;
    }

    .stButton > button[kind="primary"]:hover,
    .stFormSubmitButton > button[kind="primary"]:hover {
        transform: translateY(-1px);
        box-shadow: 0 10px 24px rgba(34, 197, 94, 0.16);
    }

    /* ---------- Feedback / progress ---------- */
    div[data-testid="stAlert"] {
        border-radius: 14px;
        border: 1px solid rgba(148, 163, 184, 0.12);
    }

    .stProgress > div > div > div > div {
        background: linear-gradient(90deg, #22C55E, #86EFAC);
    }

    /* ---------- Expense result rows ---------- */
    .expense-row {
        display: grid;
        grid-template-columns: 100px 1fr auto;
        gap: 0.8rem;
        align-items: center;
        padding: 0.72rem 0.85rem;
        margin-bottom: 0.45rem;
        border-radius: 12px;
        border: 1px solid rgba(148, 163, 184, 0.12);
        background: rgba(255, 255, 255, 0.025);
    }

    .expense-date { color: #94A3B8; font-size: 0.83rem; }
    .expense-merchant { color: #E2E8F0; font-weight: 650; }
    .expense-amount { color: #86EFAC; font-weight: 750; font-variant-numeric: tabular-nums; }

    /* ---------- Footer ---------- */
    .app-footer {
        margin-top: 2rem;
        padding-top: 1.1rem;
        border-top: 1px solid rgba(148, 163, 184, 0.10);
        color: #64748B;
        text-align: center;
        font-size: 0.78rem;
    }

    @media (max-width: 760px) {
        .block-container { padding-top: 1rem; padding-left: 1rem; padding-right: 1rem; }
        .finance-hero { padding: 1.55rem 1.35rem; border-radius: 19px; }
        .finance-hero h1 { font-size: 2rem; }
        .stTabs [data-baseweb="tab"] { font-size: 0.79rem; padding: 0 0.45rem; }
        .expense-row { grid-template-columns: 1fr auto; }
        .expense-date { grid-column: 1 / -1; }
    }
</style>
""", unsafe_allow_html=True)


# --- INTERFAZ VISUAL ---

st.markdown("""
<div class="finance-hero">
    <div class="hero-badge">● Control financiero inteligente</div>
    <h1>Tu dinero, más claro.<br>Tu operación, más simple.</h1>
    <p>Registra, extrae y clasifica gastos con IA mientras mantienes tu información sincronizada con Google Sheets.</p>
    <div class="status-row">
        <div class="status-pill"><span class="status-dot"></span> Gemini conectado</div>
        <div class="status-pill"><span class="status-dot"></span> Google Sheets sincronizado</div>
        <div class="status-pill"><span class="status-dot"></span> Flujo automatizado</div>
    </div>
</div>
""", unsafe_allow_html=True)

tab1, tab2, tab3 = st.tabs(["✍️  Ingreso manual", "📄  Subir documento", "✨  Procesar gastos"])

with tab1:
    st.markdown("""
    <span class="section-kicker">Captura rápida</span>
    <div class="section-title">Agregar un gasto</div>
    <p class="section-copy">Ingresa los datos básicos y envíalos a la fila de espera para su posterior clasificación.</p>
    """, unsafe_allow_html=True)

    with st.form("manual_form"):
        col1, col2 = st.columns(2, gap="medium")
        fecha_input = col1.date_input("Fecha", datetime.today())
        monto_input = col2.number_input("Monto ($)", min_value=0.0, format="%.2f")
        comercio_input = st.text_input("Comercio / Descripción", placeholder="Ej. Starbucks, HEB, Farmacia...")
        submit_btn = st.form_submit_button("Guardar en fila de espera  →", type="primary", use_container_width=True)

        if submit_btn and comercio_input:
            fecha_formateada = fecha_input.strftime("%d/%m/%y")
            monto_limpio = limpiar_monto(monto_input)

            hoja_recepcion.append_row(
                [fecha_formateada, comercio_input, monto_limpio],
                value_input_option="USER_ENTERED"
            )
            st.success(f"Guardado exitosamente: {comercio_input} por ${monto_limpio}")

with tab2:
    st.markdown("""
    <span class="section-kicker">Captura asistida por IA</span>
    <div class="section-title">Extraer gastos desde un documento</div>
    <p class="section-copy">Carga un ticket o estado de cuenta y deja que la IA identifique las compras automáticamente.</p>
    <div class="helper-card">💡 <strong>Formatos aceptados:</strong> PDF, PNG, JPG y JPEG. Puedes agregar instrucciones especiales para filtrar el análisis, por ejemplo por mes o tipo de gasto.</div>
    """, unsafe_allow_html=True)

    archivo_subido = st.file_uploader("Sube tu archivo", type=["pdf", "png", "jpg", "jpeg"])

    instrucciones_usuario = st.text_area(
        "Instrucciones especiales para la IA (Opcional)",
        placeholder="Ej. Solo extrae los gastos del mes de septiembre..."
    )

    if archivo_subido is not None:
        if st.button("Analizar documento con IA  ✨", type="primary", use_container_width=True):
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
                    st.markdown(
                        f"<div class='section-title' style='margin-top:1rem;'>Se encontraron {len(gastos_extraidos)} gastos</div>",
                        unsafe_allow_html=True
                    )
                    st.markdown("<p class='section-copy'>Estos movimientos se agregarán a la fila de espera.</p>", unsafe_allow_html=True)

                    for g in gastos_extraidos:
                        monto_limpio = limpiar_monto(g['monto'])
                        st.markdown(
                            f"""
                            <div class="expense-row">
                                <div class="expense-date">📅 {g['fecha']}</div>
                                <div class="expense-merchant">{g['comercio']}</div>
                                <div class="expense-amount">${monto_limpio}</div>
                            </div>
                            """,
                            unsafe_allow_html=True
                        )

                        hoja_recepcion.append_row(
                            [g['fecha'], g['comercio'], monto_limpio],
                            value_input_option="USER_ENTERED"
                        )
                    st.success("¡Todos los gastos se agregaron a la fila de espera correctamente!")
                else:
                    st.warning("No se encontraron gastos o no coincidieron con tus instrucciones.")

with tab3:
    st.markdown("""
    <span class="section-kicker">Automatización</span>
    <div class="section-title">Clasificar y acomodar gastos pendientes</div>
    <p class="section-copy">Procesa en bloque todo lo que está en la fila de espera y envíalo a tu matriz visual.</p>
    <div class="process-card">
        <strong>¿Qué ocurrirá?</strong><br>
        <span>La IA clasificará los comercios, ubicará cada gasto en el mes correspondiente y marcará como procesadas las filas completadas.</span>
    </div>
    """, unsafe_allow_html=True)

    if st.button("🚀  Procesar todo ahora", type="primary", use_container_width=True):
        with st.spinner("Despertando a la IA y acomodando celdas en el panel visual..."):
            total = procesar_pendientes()
            if total > 0:
                st.success(f"¡Listo! Se clasificaron y acomodaron {total} gastos exitosamente.")
                st.balloons()
            else:
                st.info("No hay gastos nuevos por procesar en la fila de espera.")

st.markdown("""
<div class="app-footer">Gestor de Gastos · IA + Google Sheets · Panel personal</div>
""", unsafe_allow_html=True)
