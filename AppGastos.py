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

CATEGORIAS_PERMITIDAS = [
    "OXXO", "Comida", "Cafetería (s)", "Farmacia (n)", 
    "Gasolina (n)", "Super (n)", "Cine (s)", "Flores",
    "Comida (s)", "Comida (n)", "Corte (n)", "Helado (s)", "Regalo"
]

# --- FUNCIONES NÚCLEO ---

def extraer_gastos_de_documento(archivo_bytes, mime_type, instrucciones=""):
    """Usa Gemini mediante la File API con espera de procesamiento activa"""
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
    """Envía todos los comercios en una sola petición para evitar límites de velocidad."""
    prompt = f"""
    Actúa como un categorizador financiero automático.
    Clasifica esta lista de comercios asignando a cada uno estrictamente una de las siguientes categorías:
    {', '.join(CATEGORIAS_PERMITIDAS)}
    
    Instrucciones obligatorias:
    1. Si el texto dice o contiene "farmacia", devuelve 'Farmacia (n)'.
    2. Si contiene "oxxo", devuelve 'OXXO'.
    3. Si contiene "cine", devuelve 'Cine (s)'.
    4. Si contiene "gasolina" o "gas", devuelve 'Gasolina (n)'.
    5. Si contiene "super", "walmart", "heb", devuelve 'Super (n)'.
    6. Aplica el sentido común para el resto.
    7. SOLO si el texto es totalmente irreconocible, devuelve 'Comida'.
    
    Comercios a clasificar:
    {json.dumps(lista_comercios)}
    
    Responde ÚNICAMENTE con un arreglo JSON válido, donde cada objeto tenga las llaves "comercio" y "categoria".
    Ejemplo de salida:
    [
        {{"comercio": "Oxxo Naranjos", "categoria": "OXXO"}},
        {{"comercio": "Uber Eats", "categoria": "Comida"}}
    ]
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
    
    # 1. Agrupar todos los gastos pendientes
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
    
    # 2. Enviar a la IA en una sola llamada
    nombres_comercios = [item["comercio"] for item in filas_a_procesar]
    resultados_ia = clasificar_gastos_en_lote(nombres_comercios)
    
    # Convertir respuesta de la IA en un diccionario rápido { "Oxxo": "OXXO", "Uber": "Comida" }
    mapa_categorias = {r.get("comercio", ""): r.get("categoria", "Comida") for r in resultados_ia}
    
    procesados = 0
    progress_bar = st.progress(0)
    
    # 3. Acomodar los resultados en Google Sheets
    for i, item in enumerate(filas_a_procesar):
        comercio = item["comercio"]
        categoria = mapa_categorias.get(comercio, "Comida")
        
        # Validar que la categoría de la IA exista en tu lista
        if categoria not in CATEGORIAS_PERMITIDAS:
            categoria = "Comida"
            
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
            
            if fila_destino <= 38:
                hoja_visual.update(
                    values=[[categoria, item["fecha"], item["monto"]]],
                    range_name=f"{col_letra}{fila_destino}"
                )
                hoja_recepcion.update_cell(item["index"], 4, "Listo")
                procesados += 1
        
        # Freno minúsculo de 1 segundo exclusivamente para no saturar Google Sheets
        time.sleep(1)
        progress_bar.progress(min((i + 1) / len(filas_a_procesar), 1.0))
        
    status_text.text("¡Procesamiento finalizado!")
    return procesados

# --- INTERFAZ VISUAL ---

st.title("💸 Mi Panel Financiero")

tab1, tab2, tab3 = st.tabs(["✍️ Ingreso Manual", "📄 Subir Documento", "🚀 Ejecutar Ahora"])

# Pestaña 1: Ingreso Manual
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
            hoja_recepcion.append_row([fecha_formateada, comercio_input, str(monto_input)])
            st.success(f"Guardado: {comercio_input} por ${monto_input}")

# Pestaña 2: Subir PDF o Imagen
with tab2:
    st.subheader("Extraer desde Ticket o Estado de Cuenta")
    st.info("Sube una foto de un ticket o un PDF de tu banco.")
    
    archivo_subido = st.file_uploader("Sube tu archivo", type=["pdf", "png", "jpg", "jpeg"])
    
    instrucciones_usuario = st.text_area(
        "Instrucciones especiales para la IA (Opcional)", 
        placeholder="Ej. Solo extrae los gastos del mes de septiembre, e ignora los retiros en efectivo..."
    )
    
    if archivo_subido is not None:
        if st.button("Analizar Documento"):
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
                    st.write(f"Se encontraron y filtraron {len(gastos_extraidos)} gastos:")
                    for g in gastos_extraidos:
                        st.write(f"- {g['fecha']} | {g['comercio']} | ${g['monto']}")
                        hoja_recepcion.append_row([g['fecha'], g['comercio'], str(g['monto'])])
                    st.success("¡Todos los gastos se agregaron a la fila de espera!")
                else:
                    st.warning("No se encontraron gastos o no coincidieron con tus instrucciones.")

# Pestaña 3: Ejecutar y Acomodar
with tab3:
    st.subheader("Acomodar gastos pendientes")
    st.write("Presiona este botón para que la IA clasifique todos los gastos de la fila de espera en un solo bloque.")
    
    if st.button("🚀 Procesar Todo Ahora", type="primary"):
        with st.spinner("Despertando a la IA y acomodando celdas..."):
            total = procesar_pendientes()
            if total > 0:
                st.success(f"¡Listo! Se clasificaron y acomodaron {total} gastos exitosamente.")
                st.balloons()
            else:
                st.info("No hay gastos nuevos por procesar.")
