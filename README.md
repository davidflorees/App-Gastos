# Gestor de Gastos + Finanzas

Coloca `AppGastos.py`, `finanzas.py` y la carpeta `.streamlit` en el mismo directorio de tu aplicación. Arranca con `streamlit run AppGastos.py`. Conserva tus `st.secrets` existentes para Gemini y la cuenta de servicio de Google.

La pestaña **Finanzas** solo lee las hojas con nombres de año (`2025`, `2026`, etc.) del libro `Gastos`. Considera únicamente filas 3 a 38 de cada bloque mensual `Categoría / Fecha / Importe`; calcula las cifras a partir de esos movimientos, excluye fechas fuera del mes y marca las discrepancias en **Calidad de datos**. `Apple Pay` sigue siendo la bandeja de entrada de las funciones existentes.

Los presupuestos se almacenan en `.streamlit/presupuestos.sqlite3`, separados de Google Sheets. Si el servidor pierde su disco entre reinicios, descarga el respaldo JSON en **Calidad de datos** y restáuralo allí. Para guardar en un volumen persistente, configura `GASTOS_BUDGET_DB_PATH` con una ruta de SQLite escribible y persistente. Los presupuestos no se añaden ni actualizan en el libro `Gastos`.

La proyección lineal y el disponible diario se muestran solo para el mes actual. La interpretación con Gemini se solicita únicamente al pulsar el botón y recibe cifras agregadas, sin datos individuales de movimientos.

Dependencias: `streamlit`, `gspread`, `google-genai` y `pandas` (esta última normalmente ya está instalada con Streamlit).
