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
st.set_page_config(page_title="Mis finanzas", page_icon="💸", layout="wide")

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
  .block-container {max-width:1120px; padding-top:2.2rem; padding-bottom:3rem}
  h1,h2,h3 {letter-spacing:-.035em; font-weight:700}
  h2 {font-size:1.65rem !important} h3 {font-size:1.3rem !important}
  .finance-hero {background:#123c35; color:#fff; border-radius:24px;
    padding:30px 34px; margin-bottom:22px; position:relative; overflow:hidden}
  .finance-hero .eyebrow {color:#b8e8cd; font-size:.75rem; font-weight:700;
    letter-spacing:.14em; text-transform:uppercase; margin-bottom:10px}
  .finance-hero h1 {color:#fff; font-size:2.35rem; padding:0 0 8px; line-height:1.15}
  .finance-hero p {color:#d7e9e1; margin:0; max-width:620px; line-height:1.6}
  .finance-hero .tag {display:inline-block; color:#d7e9e1; border:1px solid #53776b;
    padding:5px 12px; border-radius:30px; margin-top:20px; font-size:.8rem}
  [role="tablist"] {gap:8px; background:var(--secondary-background-color);
    border-radius:16px; padding:6px; height:auto; margin-bottom:20px}
  [role="tab"] {flex:1; min-height:48px; height:auto; border-radius:11px;
    padding:12px 14px; color:var(--text-color); white-space:normal}
  [role="tab"] p {font-weight:600; font-size:.95rem}
  [role="tab"][aria-selected="true"] {background:#17634e; color:#fff;
    box-shadow:0 3px 8px #00000012}
  [data-testid="stTabsHighlight"], [data-testid="stTabsBorder"], [data-testid="stTabsHighlight"], [data-testid="stTabsBorder"], [data-baseweb="tab-highlight"], [data-baseweb="tab-border"] {display:none}
  [role="tab"]:focus-visible, button:focus-visible {
    outline:3px solid #b78020 !important; outline-offset:3px}
  [data-testid="stForm"] {border:1px solid #82958b55; border-radius:20px;
    padding:24px; box-shadow:0 4px 24px #142e2006}
  [data-testid="stMetric"] {border:1px solid #82958b55; border-radius:18px; padding:20px}
  [data-testid="stFileUploader"] {border-radius:16px}
  [data-testid="stFileUploaderDropzone"] {border:1px dashed #82958b; border-radius:16px; padding:24px}
  [data-testid="stButton"] button, [data-testid="stFormSubmitButton"] button {
    border-radius:12px; min-height:48px; padding:10px 20px; font-weight:600}
  [data-testid="stButton"] button[kind="primary"],
  [data-testid="stFormSubmitButton"] button[kind="primary"] {
    background:#17634e; color:#fff; border:1px solid #17634e}
  [data-testid="stButton"] button[kind="primary"]:hover,
  [data-testid="stFormSubmitButton"] button[kind="primary"]:hover {background:#104b3a}
  [data-baseweb="input"], [data-baseweb="textarea"], [data-baseweb="select"] > div {
    border-radius:10px; min-height:46px}
  [data-testid="stAlert"] {border-radius:14px}
  [data-testid="stRadio"] [role="radiogroup"] {gap:12px}
  [data-testid="stRadio"] label {padding:8px 12px; border:1px solid #82958b55; border-radius:10px}
  .flow-note {border-left:3px solid #38936e; padding:8px 14px;
    margin:0 0 20px; font-size:.9rem; line-height:1.65}
  @media(max-width:640px) {
    .block-container {padding:4.5rem 1rem 2rem}
    .finance-hero {padding:24px 22px; border-radius:20px; margin-bottom:12px}
    .finance-hero h1 {font-size:1.85rem}
    .finance-hero .tag {margin-top:14px}
    [role="tablist"] {display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:6px}
    [role="tab"] {width:100%; padding:10px 6px; min-height:48px}
    [data-testid="stForm"] {padding:18px 14px}
    [data-testid="stButton"] button, [data-testid="stFormSubmitButton"] button {width:100%}
    [data-testid="stFileUploaderDropzone"] {padding:16px; flex-wrap:wrap}
    [data-baseweb="input"] input, textarea {font-size:16px !important}
  }

  /* Secondary navigation: distinguish the finance sections from the main app. */
  [data-testid="stTabs"] [data-testid="stTabs"] [role="tablist"] {
    background:transparent; padding:0; gap:6px; margin-top:4px; margin-bottom:16px}
  [data-testid="stTabs"] [data-testid="stTabs"] [role="tab"] {
    border:1px solid #82958b55; padding:9px 10px; min-height:44px}
  [data-testid="stTabs"] [data-testid="stTabs"] [role="tab"][aria-selected="true"] {
    background:var(--secondary-background-color); color:var(--text-color);
    border:2px solid #258467; box-shadow:none}
  [data-testid="stMetricValue"] {font-size:1.65rem; line-height:1.3; overflow:visible}
  [data-testid="stMetricValue"] > div {white-space:normal; overflow-wrap:anywhere}
  [data-testid="stMetricLabel"] {min-height:2.5rem; align-items:flex-start}
  [data-testid="stMetric"] {height:100%; min-height:136px; padding:18px}
  [data-testid="stExpander"] {border-radius:14px}
  [data-testid="stDataFrame"] {border-radius:12px; overflow:hidden}
  .filter-spacer {height:28px}
  @media(max-width:640px) {
    .filter-spacer {display:none}
    [data-testid="stHorizontalBlock"]:has(> [data-testid="stColumn"]:nth-child(4)):not(:has(> [data-testid="stColumn"]:nth-child(5))) {
      flex-wrap:wrap !important; gap:12px !important}
    [data-testid="stHorizontalBlock"]:has(> [data-testid="stColumn"]:nth-child(4)):not(:has(> [data-testid="stColumn"]:nth-child(5))) > [data-testid="stColumn"] {
      flex:1 1 calc(50% - 12px) !important; width:calc(50% - 12px) !important; min-width:0 !important}
    [data-testid="stMetric"] {min-height:132px; padding:14px 12px}
    [data-testid="stMetricValue"] {font-size:1.35rem}
    [data-testid="stMetricLabel"] p {font-size:.85rem}
    [data-testid="stTabs"] [data-testid="stTabs"] [role="tab"] {padding:8px; min-height:44px}
  }


  .react-aria-SelectionIndicator {display:none}
  [data-testid="stCaptionContainer"] {opacity:1; color:var(--text-color)}
  @media(max-width:640px) {
    .finance-hero {padding:18px 20px}
    .finance-hero .tag {display:none}
    .finance-hero h1 {font-size:1.65rem}
    [data-testid="stHorizontalBlock"]:has(> [data-testid="stColumn"]:nth-child(3)):has([data-testid="stSelectbox"]) {
      flex-wrap:wrap !important; gap:12px !important}
    [data-testid="stHorizontalBlock"]:has(> [data-testid="stColumn"]:nth-child(3)):has([data-testid="stSelectbox"]) > [data-testid="stColumn"] {
      flex:1 1 calc(50% - 12px) !important; width:calc(50% - 12px) !important; min-width:0 !important}
    [data-testid="stHorizontalBlock"]:has(> [data-testid="stColumn"]:nth-child(3)):has([data-testid="stSelectbox"]) > [data-testid="stColumn"]:nth-child(3) {
      flex:1 1 100% !important; width:100% !important}
  }
</style>""", unsafe_allow_html=True)

st.markdown("""
<div class="finance-hero">
  <div class="eyebrow">TU ESPACIO FINANCIERO</div>
  <h1>Mis finanzas, en orden.</h1>
  <p>Registra tus gastos, organiza tus pendientes y consulta tus finanzas en un solo lugar.</p>
  <span class="tag">Registro manual · Documentos · Clasificación con IA</span>
</div>
""", unsafe_allow_html=True)

tab1, tab2, tab3, tab4 = st.tabs(["Registrar", "Documentos", "Pendientes", "Finanzas"])

with tab1:
    st.subheader("Registra un gasto")
    st.caption("Anota los detalles. Después podrás clasificarlo desde Pendientes.")
    with st.form("manual_form"):
        col1, col2 = st.columns(2)
        fecha_input = col1.date_input("Fecha", datetime.today())
        monto_input = col2.number_input("Monto ($)", min_value=0.0, format="%.2f")
        comercio_input = st.text_input("Comercio / Descripción")
        
        # Botón primario verde
        submit_btn = st.form_submit_button("Guardar gasto en pendientes", type="primary")
        
        if submit_btn and comercio_input:
            fecha_formateada = fecha_input.strftime("%d/%m/%y")
            monto_limpio = limpiar_monto(monto_input)
            
            hoja_recepcion.append_row(
                [fecha_formateada, comercio_input, monto_limpio],
                value_input_option="USER_ENTERED"
            )
            st.success(f"Guardado exitosamente: {comercio_input} por ${monto_limpio}")

with tab2:
    st.subheader("Agrega gastos desde un documento")
    st.caption("Sube un ticket o estado de cuenta, o pega una imagen para extraer sus gastos.")

    st.info(
        "Puedes subir un PDF o imagen, "
        "o pegar directamente una imagen desde tu portapapeles."
    )

    modo_entrada = st.radio(
        "¿Cómo quieres agregar el documento?",
        [
            "📎 Subir archivo",
            "📋 Pegar imagen"
        ],
        horizontal=True
    )

    st.caption("Al analizar, los gastos encontrados se guardan automáticamente en Pendientes.")

    instrucciones_usuario = st.text_area(
        "¿Qué debe tomar en cuenta la IA? (Opcional)",
        placeholder="Ej. Solo extrae los gastos del mes de octubre..."
    )

    # =====================================================
    # OPCIÓN 1: SUBIR ARCHIVO
    # =====================================================

    if modo_entrada == "📎 Subir archivo":

        archivo_subido = st.file_uploader(
            "Sube tu archivo",
            type=["pdf", "png", "jpg", "jpeg"]
        )

        if archivo_subido is not None:

            if st.button(
                "Analizar y guardar gastos",
                type="primary",
                key="analizar_archivo"
            ):

                with st.spinner(
                    "La IA está leyendo y filtrando el documento..."
                ):

                    bytes_data = archivo_subido.getvalue()

                    nombre_archivo = archivo_subido.name.lower()

                    if nombre_archivo.endswith(".pdf"):
                        mime = "application/pdf"

                    elif nombre_archivo.endswith(".png"):
                        mime = "image/png"

                    else:
                        mime = "image/jpeg"

                    gastos_extraidos = extraer_gastos_de_documento(
                        bytes_data,
                        mime,
                        instrucciones_usuario
                    )

                    if gastos_extraidos:

                        st.write(
                            f"**Se encontraron "
                            f"{len(gastos_extraidos)} gastos:**"
                        )

                        for g in gastos_extraidos:

                            monto_limpio = limpiar_monto(
                                g["monto"]
                            )

                            st.write(
                                f"- 📅 {g['fecha']} | "
                                f"🏢 {g['comercio']} | "
                                f"💵 ${monto_limpio}"
                            )

                            hoja_recepcion.append_row(
                                [
                                    g["fecha"],
                                    g["comercio"],
                                    monto_limpio
                                ],
                                value_input_option="USER_ENTERED"
                            )

                        st.success(
                            "¡Todos los gastos se agregaron "
                            "a la fila de espera correctamente!"
                        )

                    else:

                        st.warning(
                            "No se encontraron gastos "
                            "o no coincidieron con tus instrucciones."
                        )

    # =====================================================
    # OPCIÓN 2: PEGAR IMAGEN
    # =====================================================

    if modo_entrada == "📋 Pegar imagen":

        st.write(
            "Copia una captura, ticket o imagen y "
            "presiona el botón de abajo."
        )

        imagen_pegada = paste_image_button(
            label="📋 Pegar imagen del portapapeles",
            key="imagen_portapapeles"
        )

        if imagen_pegada.image_data is not None:

            imagen = imagen_pegada.image_data

            st.image(
                imagen,
                caption="Imagen pegada",
                use_container_width=True
            )

            if st.button(
                "Analizar y guardar gastos de la imagen",
                type="primary",
                key="analizar_imagen_pegada"
            ):

                with st.spinner(
                    "La IA está leyendo la imagen..."
                ):

                    buffer = BytesIO()

                    # Convertimos a PNG para Gemini
                    imagen.save(
                        buffer,
                        format="PNG"
                    )

                    bytes_data = buffer.getvalue()

                    gastos_extraidos = extraer_gastos_de_documento(
                        bytes_data,
                        "image/png",
                        instrucciones_usuario
                    )

                    if gastos_extraidos:

                        st.write(
                            f"**Se encontraron "
                            f"{len(gastos_extraidos)} gastos:**"
                        )

                        for g in gastos_extraidos:

                            monto_limpio = limpiar_monto(
                                g["monto"]
                            )

                            st.write(
                                f"- 📅 {g['fecha']} | "
                                f"🏢 {g['comercio']} | "
                                f"💵 ${monto_limpio}"
                            )

                            hoja_recepcion.append_row(
                                [
                                    g["fecha"],
                                    g["comercio"],
                                    monto_limpio
                                ],
                                value_input_option="USER_ENTERED"
                            )

                        st.success(
                            "¡Todos los gastos se agregaron "
                            "a la fila de espera correctamente!"
                        )

                    else:

                        st.warning(
                            "No se encontraron gastos "
                            "en la imagen pegada."
                        )

with tab3:
    st.subheader("Organiza tus pendientes")
    st.caption("El siguiente paso después de registrar o importar tus gastos.")
    st.markdown("<div class=flow-note><strong>De pendientes a organizados.</strong><br>La IA clasifica los gastos en espera y los coloca en el mes correspondiente de tu hoja 2026.</div>", unsafe_allow_html=True)
    
    # Botón primario verde
    if st.button("Clasificar gastos pendientes", type="primary"):
        with st.spinner("Despertando a la IA y acomodando celdas en el panel visual..."):
            total = procesar_pendientes()
            if total > 0:
                st.success(f"¡Listo! Se clasificaron y acomodaron {total} gastos exitosamente.")
                st.balloons()
            else:
                st.info("No hay gastos nuevos por procesar en la fila de espera.")

with tab4:
    render_finanzas(archivo, cliente_ai)
