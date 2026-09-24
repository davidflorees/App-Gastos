import streamlit as st
import gspread
from google import genai
from google.genai import types  # <- AGREGA ESTA LÍNEA
import time
import json
from datetime import datetime

# --- CONFIGURACIÓN INICIAL ---
st.set_page_config(page_title="Gestor de Gastos", page_icon="💸", layout="centered")

# Cargar secretos de Streamlit
GEMINI_API_KEY = st.secrets["GEMINI_API_KEY"]
cliente_ai = genai.Client(api_key=GEMINI_API_KEY)

# Conexión a Google Sheets usando el diccionario de secretos de Streamlit
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

def clasificar_gasto(comercio):
    prompt = f"""
    Actúa como un categorizador financiero automático.
    Comercio recibido: '{comercio}'
    
    Elige estrictamente una de las siguientes categorías para ese comercio:
    {', '.join(CATEGORIAS_PERMITIDAS)}
    
    Instrucciones obligatorias:
    1. Si el texto dice o contiene "farmacia", devuelve SIEMPRE 'Farmacia (n)'.
    2. Si contiene "oxxo", devuelve 'OXXO'.
    3. Si contiene "cine", devuelve 'Cine (s)'.
    4. Si contiene "gasolina" o "gas", devuelve 'Gasolina (n)'.
    5. Si contiene "super", "walmart", "heb", devuelve 'Super (n)'.
    6. Aplica el sentido común para el resto.
    7. SOLO si el texto es totalmente irreconocible, devuelve 'Comida'.
    
    Responde ÚNICAMENTE con el nombre exacto de la categoría. No uses comillas.
    """
    try:
        response = cliente_ai.models.generate_content(
            model='gemini-3.6-pro',
            contents=prompt
        )
        return response.text.strip()
    except Exception as e:
        return "Comida"

def extraer_gastos_de_documento(archivo_bytes, mime_type, instrucciones=""):
    """Usa Gemini para leer imágenes o PDFs con instrucciones personalizadas y reintentos automáticos"""
    prompt = """
    Analiza este estado de cuenta o ticket. Extrae todos los gastos y devuélvelos en formato JSON estricto.
    El JSON debe ser una lista de diccionarios con las llaves: "fecha" (formato DD/MM/YY), "comercio" (nombre limpio), "monto" (solo número sin símbolos).
    Ignora depósitos, pagos de tarjeta o abonos, solo quiero los gastos/compras.
    """
    
    if instrucciones:
        prompt += f"\nINSTRUCCIONES MUY IMPORTANTES DEL USUARIO: {instrucciones}\nDebes cumplir estas instrucciones estrictamente al filtrar o procesar los datos."
        
    prompt += '\nEjemplo de salida esperada: [{"fecha": "23/09/26", "comercio": "Starbucks", "monto": 150}]'
    
    max_reintentos = 3
    
    for intento in range(max_reintentos):
        try:
            response = cliente_ai.models.generate_content(
                model='gemini-3.6-flash',
                contents=[
                    types.Part.from_bytes(data=archivo_bytes, mime_type=mime_type),
                    prompt
                ]
            )
            texto_json = response.text.replace("```json", "").replace("```", "").strip()
            return json.loads(texto_json)
            
        except Exception as e:
            error_msg = str(e)
            # Si es error 503 y aún nos quedan intentos, esperamos y reintentamos
            if "503" in error_msg or "UNAVAILABLE" in error_msg:
                if intento < max_reintentos - 1:
                    st.warning(f"Servidores de Google ocupados. Reintentando automáticamente en 5 segundos... (Intento {intento + 1} de {max_reintentos})")
                    time.sleep(5)
                    continue # Vuelve al inicio del for
            
            # Si es otro tipo de error, o si ya agotamos los intentos
            st.error(f"Error al analizar documento: {error_msg}")
            return []
            
    return []

def procesar_pendientes():
    registros = hoja_recepcion.get_all_values()
    procesados = 0
    
    progress_bar = st.progress(0)
    status_text = st.empty()

    for index, fila in enumerate(registros[1:], start=2):
        if len(fila) < 4 or fila[3] != "Listo":
            if not fila[0] or not fila[1]:
                continue
                
            fecha_str, comercio, monto = fila[0], fila[1], fila[2]
            status_text.text(f"Procesando: {comercio}...")
            
            categoria = clasificar_gasto(comercio)
            
            try:
                mes = int(fecha_str.split('-')[1]) if '-' in fecha_str else int(fecha_str.split('/')[1])
            except:
                continue
                
            # Mapeo alineado a tu diseño visual (Julio=19, Agosto=22, Sept=25...)
            columnas_mes = {1: 1, 2: 4, 3: 7, 4: 10, 5: 13, 6: 16, 7: 19, 8: 22, 9: 25, 10: 28, 11: 31, 12: 34}
            col_inicial = columnas_mes.get(mes)
            
            if col_inicial:
                col_letra = gspread.utils.rowcol_to_a1(1, col_inicial)[0]
                valores_mes = hoja_visual.get(f"{col_letra}3:{col_letra}38")
                fila_destino = 3 + len([v for v in valores_mes if v])
                
                if fila_destino <= 38:
                    hoja_visual.update_cell(fila_destino, col_inicial, categoria)
                    hoja_visual.update_cell(fila_destino, col_inicial + 1, fecha_str)
                    hoja_visual.update_cell(fila_destino, col_inicial + 2, monto)
                    hoja_recepcion.update_cell(index, 4, "Listo")
                    procesados += 1
            time.sleep(1) 
            
        progress_bar.progress(min((index - 1) / (len(registros) - 1), 1.0))
        
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
    st.write("Presiona este botón para que la IA clasifique todos los gastos de la fila de espera y los acomode en el panel visual del 2026.")
    
    if st.button("🚀 Procesar Todo Ahora", type="primary"):
        with st.spinner("Despertando a la IA y acomodando celdas..."):
            total = procesar_pendientes()
            if total > 0:
                st.success(f"¡Listo! Se clasificaron y acomodaron {total} gastos exitosamente.")
                st.balloons()
            else:
                st.info("No hay gastos nuevos por procesar.")
