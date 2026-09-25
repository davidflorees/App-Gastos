"""Panel financiero de solo lectura sobre las hojas anuales de Gastos.

Los presupuestos viven en una base SQLite separada; este módulo no actualiza
ninguna celda de Google Sheets.
"""

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo
import calendar
import json
import os
import re
import sqlite3
import statistics
import unicodedata


MESES = ("Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio", "Julio",
         "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre")
GRUPOS = ("Necesarios", "Salidas", "OXXO", "Yo")
COLORES = {"Necesarios": "#34D399", "Salidas": "#FBBF24",
           "OXXO": "#38BDF8", "Yo": "#A78BFA"}


def hoy_local():
    return datetime.now(ZoneInfo("America/Mexico_City")).date()


def moneda(x):
    return f"${x:,.2f}"


def normalizar(texto):
    return "".join(c for c in unicodedata.normalize("NFKD", str(texto).strip())
                   if not unicodedata.combining(c)).casefold()


def importe(valor):
    if isinstance(valor, (int, float, Decimal)):
        return Decimal(str(valor))
    s = str(valor).replace("$", "").replace("MXN", "").replace(" ", "").strip()
    if not s:
        raise ValueError("Importe vacío")
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".") if len(s.split(",")[-1]) in (1, 2) else s.replace(",", "")
    try:
        n = Decimal(s)
        if not n.is_finite() or n <= 0:
            raise ValueError("Importe no positivo")
        return n
    except InvalidOperation as exc:
        raise ValueError("Importe no numérico") from exc


def fecha(valor):
    if isinstance(valor, (float, int)) or re.fullmatch(r"\d+(?:\.0+)?", str(valor).strip()):
        return date(1899, 12, 30) + timedelta(days=int(float(valor)))
    texto = str(valor).strip()
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y", "%d-%m-%y", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(texto, fmt).date()
        except ValueError:
            pass
    raise ValueError("Fecha no reconocida")


def categoria_grupo(valor):
    original = str(valor).strip()
    sufijo = re.search(r"\s*\(([ns])\)\s*$", original, flags=re.I)
    base = original[:sufijo.start()].strip() if sufijo else original
    if normalizar(base) == "oxxo":
        return "OXXO", "OXXO", "oxxo"
    grupo = "Necesarios" if sufijo and sufijo.group(1).lower() == "n" else (
        "Salidas" if sufijo else "Yo")
    return grupo, base, normalizar(base)


def leer_matriz(matriz, anio):
    """Extrae filas 3:38 de bloques con encabezados en las dos primeras filas."""
    if len(matriz) < 2:
        return [], []
    cabeceras = matriz[0]
    columnas = matriz[1]
    movimientos, alertas = [], []
    for col in range(0, len(cabeceras), 3):
        nombre = str(cabeceras[col]).strip()
        if normalizar(nombre) not in [normalizar(m) for m in MESES]:
            continue
        mes = next(i for i, m in enumerate(MESES, 1) if normalizar(m) == normalizar(nombre))
        if [normalizar(columnas[j]) if j < len(columnas) else "" for j in range(col, col + 3)] != ["categoria", "fecha", "importe"]:
            alertas.append(f"{anio} {nombre}: encabezados de columnas inesperados.")
            continue
        for fila_idx, fila in enumerate(matriz[2:38], 3):
            vals = [fila[j] if j < len(fila) else "" for j in range(col, col + 3)]
            if not any(str(v).strip() for v in vals):
                continue
            try:
                if not all(str(v).strip() for v in vals):
                    raise ValueError("Movimiento incompleto")
                d, total = fecha(vals[1]), importe(vals[2])
                grupo, base, clave = categoria_grupo(vals[0])
                if not clave:
                    raise ValueError("Categoría vacía")
            except (ValueError, OverflowError) as exc:
                alertas.append(f"{anio} {nombre}, fila {fila_idx}: {exc} ({vals!r}).")
                continue
            if d.year != anio or d.month != mes:
                alertas.append(f"{anio} {nombre}, fila {fila_idx}: fecha {d:%d/%m/%Y} fuera del mes; excluida del cálculo.")
                continue
            movimientos.append({"anio": anio, "mes": mes, "fecha": d, "importe": total,
                                "grupo": grupo, "categoria": base, "clave": clave,
                                "fila": fila_idx})
    return movimientos, alertas


def resumen(registros):
    total = sum((r["importe"] for r in registros), Decimal(0))
    grupos = {g: sum((r["importe"] for r in registros if r["grupo"] == g), Decimal(0)) for g in GRUPOS}
    categorias = defaultdict(list)
    dias = defaultdict(lambda: Decimal(0))
    semanas = defaultdict(lambda: Decimal(0))
    for r in registros:
        categorias[r["clave"]].append(r)
        dias[r["fecha"]] += r["importe"]
        semanas[r["fecha"] - timedelta(days=r["fecha"].weekday())] += r["importe"]
    detalle = []
    for clave, items in categorias.items():
        montos = [r["importe"] for r in items]
        subtotal = sum(montos, Decimal(0))
        detalle.append({"clave": clave, "categoria": items[0]["categoria"], "total": subtotal,
                        "movimientos": len(items), "promedio": subtotal / len(items),
                        "maximo": max(montos), "minimo": min(montos),
                        "porcentaje": float(subtotal / total * 100) if total else 0})
    detalle.sort(key=lambda x: x["total"], reverse=True)
    return {"total": total, "conteo": len(registros), "grupos": grupos,
            "categorias": detalle, "dias": dict(sorted(dias.items())),
            "semanas": dict(sorted(semanas.items()))}


def comparacion(actual, anterior):
    if anterior is None:
        return "Sin datos previos"
    if anterior == 0:
        return "Nueva" if actual else "—"
    diferencia = actual - anterior
    return f"{'+' if diferencia >= 0 else '−'}{moneda(abs(diferencia))} ({'+' if diferencia >= 0 else '−'}{abs(float(diferencia / anterior * 100)):.1f}%)"


def hallazgos(actual, anterior, registros, limite=None):
    if not actual["conteo"]:
        return ["Todavía no hay movimientos válidos en este mes."]
    ideas = []
    principal = actual["categorias"][0]
    ideas.append(f"{principal['categoria']} concentra {principal['porcentaje']:.1f}% del gasto ({moneda(principal['total'])}).")
    oxxo = [r for r in registros if r["grupo"] == "OXXO"]
    if oxxo:
        ideas.append(f"OXXO: {len(oxxo)} compras, {moneda(actual['grupos']['OXXO'])} en total y {moneda(actual['grupos']['OXXO'] / len(oxxo))} por visita.")
    if anterior and anterior["conteo"]:
        ideas.append(f"Frente al mes anterior, el gasto total cambió {comparacion(actual['total'], anterior['total'])}.")
    if limite is not None:
        diferencia = limite - actual["total"]
        ideas.append(f"Presupuesto: {'quedan ' + moneda(diferencia) if diferencia >= 0 else 'excedido por ' + moneda(-diferencia)}.")
    return ideas


def detectar_anomalias(actual, anterior, registros):
    alertas = []
    por_categoria = defaultdict(list)
    for r in registros:
        por_categoria[r["clave"]].append(r)
    for clave, items in por_categoria.items():
        montos = [r["importe"] for r in items]
        if len(montos) >= 4:
            mediana = statistics.median(montos)
            for r in items:
                if mediana and r["importe"] >= mediana * Decimal("2.5") and r["importe"] - mediana >= 150:
                    alertas.append(f"{r['categoria']}: {moneda(r['importe'])} el {r['fecha']:%d/%m} supera 2.5 veces la mediana de esa categoría.")
    if anterior and anterior["conteo"]:
        prev = {x["clave"]: x["total"] for x in anterior["categorias"]}
        for x in actual["categorias"]:
            pasado = prev.get(x["clave"])
            if pasado is None:
                alertas.append(f"Categoría nueva respecto al mes anterior: {x['categoria']} ({moneda(x['total'])}).")
            elif pasado >= 100 and x["total"] >= pasado * Decimal("1.5") and x["total"] - pasado >= 200:
                alertas.append(f"{x['categoria']} subió {comparacion(x['total'], pasado)} frente al mes anterior.")
    return alertas[:12]


def ruta_db():
    return Path(os.environ.get("GASTOS_BUDGET_DB_PATH", str(Path(__file__).parent / ".streamlit" / "presupuestos.sqlite3"))).expanduser()


def conexion_db():
    ruta = ruta_db()
    ruta.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(ruta), timeout=15)
    conn.execute("CREATE TABLE IF NOT EXISTS presupuestos (anio INTEGER NOT NULL, mes INTEGER NOT NULL, total REAL NOT NULL DEFAULT 0, necesarios REAL NOT NULL DEFAULT 0, salidas REAL NOT NULL DEFAULT 0, oxxo REAL NOT NULL DEFAULT 0, yo REAL NOT NULL DEFAULT 0, PRIMARY KEY (anio, mes))")
    return conn


def cargar_presupuesto(anio, mes):
    with conexion_db() as conn:
        row = conn.execute("SELECT total, necesarios, salidas, oxxo, yo FROM presupuestos WHERE anio=? AND mes=?", (anio, mes)).fetchone()
    return dict(zip(("total", "Necesarios", "Salidas", "OXXO", "Yo"), row or (0, 0, 0, 0, 0)))


def guardar_presupuesto(anio, mes, valores):
    assert 1 <= mes <= 12 and all(0 <= float(v) <= 1_000_000_000 for v in valores.values())
    with conexion_db() as conn:
        conn.execute("INSERT INTO presupuestos VALUES (?,?,?,?,?,?,?) ON CONFLICT(anio,mes) DO UPDATE SET total=excluded.total, necesarios=excluded.necesarios, salidas=excluded.salidas, oxxo=excluded.oxxo, yo=excluded.yo",
                     (anio, mes, *(float(valores[k]) for k in ("total", "Necesarios", "Salidas", "OXXO", "Yo"))))


def exportar_presupuestos():
    with conexion_db() as conn:
        filas = conn.execute("SELECT anio,mes,total,necesarios,salidas,oxxo,yo FROM presupuestos ORDER BY anio,mes").fetchall()
    return json.dumps([dict(zip(("anio", "mes", "total", "Necesarios", "Salidas", "OXXO", "Yo"), fila)) for fila in filas], ensure_ascii=False, indent=2).encode("utf-8")


def importar_presupuestos(datos):
    filas = json.loads(datos)
    if not isinstance(filas, list) or len(filas) > 600:
        raise ValueError("Archivo inválido o demasiado grande")
    limpios = []
    for fila in filas:
        anio, mes = int(fila["anio"]), int(fila["mes"])
        valores = {k: float(fila[k]) for k in ("total", "Necesarios", "Salidas", "OXXO", "Yo")}
        if not 2000 <= anio <= 2100 or not 1 <= mes <= 12 or not all(0 <= v <= 1_000_000_000 for v in valores.values()):
            raise ValueError("Hay periodos o importes inválidos")
        limpios.append((anio, mes, *(valores[k] for k in ("total", "Necesarios", "Salidas", "OXXO", "Yo"))))
    with conexion_db() as conn:
        conn.executemany("INSERT INTO presupuestos VALUES (?,?,?,?,?,?,?) ON CONFLICT(anio,mes) DO UPDATE SET total=excluded.total, necesarios=excluded.necesarios, salidas=excluded.salidas, oxxo=excluded.oxxo, yo=excluded.yo", limpios)
    return len(limpios)


def render_finanzas(archivo, cliente_ai):
    import pandas as pd
    import streamlit as st

    @st.cache_data(ttl=120, show_spinner=False)
    def cargar_anio(anio, _archivo):
        return leer_matriz(_archivo.worksheet(str(anio)).get_all_values(), anio)

    st.markdown("### 📊 Inteligencia financiera")
    st.caption("Movimientos de las hojas anuales · cifras calculadas desde transacciones · importes en MXN")
    try:
        anios = sorted(int(ws.title) for ws in archivo.worksheets() if re.fullmatch(r"20\d{2}", ws.title))
        if not anios:
            st.info("No se encontraron hojas anuales para analizar.")
            return
        datos = []
        alertas_por_anio = {}
        with st.spinner("Leyendo movimientos de las hojas anuales..."):
            for anio in anios:
                registros, errores = cargar_anio(anio, archivo)
                datos.extend(registros)
                alertas_por_anio[anio] = errores
    except Exception as exc:
        st.error(f"No se pudieron leer las hojas anuales: {exc}")
        return

    ctrl1, ctrl2, ctrl3 = st.columns([1, 1, 1])
    anio = ctrl1.selectbox("Año", anios, index=len(anios) - 1, key="fin_anio")
    meses_con_datos = sorted({r["mes"] for r in datos if r["anio"] == anio})
    mes_default = meses_con_datos[-1] if meses_con_datos else hoy_local().month
    mes = ctrl2.selectbox("Mes", range(1, 13), index=mes_default - 1,
                          format_func=lambda m: MESES[m - 1], key="fin_mes")
    if ctrl3.button("↻ Actualizar datos", use_container_width=True):
        cargar_anio.clear()
        st.rerun()

    del_mes = [r for r in datos if (r["anio"], r["mes"]) == (anio, mes)]
    prev_anio, prev_mes = (anio - 1, 12) if mes == 1 else (anio, mes - 1)
    previo = [r for r in datos if (r["anio"], r["mes"]) == (prev_anio, prev_mes)]
    actual = resumen(del_mes)
    anterior = resumen(previo) if previo else None

    try:
        presupuesto = cargar_presupuesto(anio, mes)
    except (OSError, sqlite3.Error) as exc:
        st.error(f"No se pudo acceder al archivo separado de presupuestos: {exc}")
        return
    limite = Decimal(str(presupuesto["total"])) if presupuesto["total"] > 0 else None

    tab_mes, tab_anual, tab_categoria, tab_calidad = st.tabs(
        ["📍 Mes y presupuesto", "📈 Evolución", "🔎 Categorías", "🧾 Calidad de datos"])
    with tab_mes:
        st.markdown(f"#### {MESES[mes - 1]} {anio}")
        if not del_mes:
            st.info("No hay movimientos válidos para el mes elegido. Puedes configurar su presupuesto de todos modos.")
        a, b, c, d = st.columns(4)
        a.metric("Gastado", moneda(actual["total"]), delta=comparacion(actual["total"], anterior["total"]) if anterior else None,
                 delta_color="inverse")
        b.metric("Movimientos", actual["conteo"])
        c.metric("Ticket promedio", moneda(actual["total"] / actual["conteo"]) if actual["conteo"] else "—")
        d.metric("Mayor categoría", actual["categorias"][0]["categoria"] if actual["categorias"] else "—")

        st.markdown("##### Tus cuatro bolsas")
        cols = st.columns(4)
        for col, grupo in zip(cols, GRUPOS):
            valor = actual["grupos"][grupo]
            col.metric(grupo, moneda(valor), f"{float(valor / actual['total'] * 100):.1f}% del gasto" if actual["total"] else "0% del gasto", delta_color="off")
        if actual["conteo"]:
            df_grupos = pd.DataFrame([{"Grupo": g, "MXN": float(actual["grupos"][g])} for g in GRUPOS])
            st.bar_chart(df_grupos.set_index("Grupo"), color="#34D399")
            dia_pico = max(actual["dias"], key=actual["dias"].get)
            st.caption(f"Día de mayor gasto: {dia_pico:%d/%m/%Y} · {moneda(actual['dias'][dia_pico])}")
            st.markdown("##### Ritmo diario y semanal")
            st.line_chart(pd.DataFrame([{"Fecha": dia, "MXN": float(valor)}
                                         for dia, valor in actual["dias"].items()]).set_index("Fecha"), color="#38BDF8")
            st.bar_chart(pd.DataFrame([{"Semana desde": dia, "MXN": float(valor)}
                                        for dia, valor in actual["semanas"].items()]).set_index("Semana desde"), color="#FBBF24")
            fin_de_semana = sum((r["importe"] for r in del_mes if r["fecha"].weekday() >= 5), Decimal(0))
            st.caption(f"Fines de semana: {moneda(fin_de_semana)} · {float(fin_de_semana / actual['total'] * 100):.1f}% del mes.")

        st.markdown("##### Presupuesto mensual")
        with st.form(f"presupuesto_{anio}_{mes}"):
            nuevo = {}
            nuevo["total"] = st.number_input("Límite total mensual (MXN)", 0.0, 1_000_000_000.0,
                                             float(presupuesto["total"]), step=100.0, format="%.2f")
            st.caption("Los límites por bolsa son opcionales. Escribe 0 para desactivarlos.")
            limites_cols = st.columns(4)
            for col, grupo in zip(limites_cols, GRUPOS):
                nuevo[grupo] = col.number_input(grupo, 0.0, 1_000_000_000.0,
                                                 float(presupuesto[grupo]), step=100.0, format="%.2f")
            if st.form_submit_button("Guardar presupuesto", type="primary"):
                try:
                    guardar_presupuesto(anio, mes, nuevo)
                    st.success("Presupuesto guardado fuera de Google Sheets.")
                    st.rerun()
                except (OSError, sqlite3.Error, ValueError) as exc:
                    st.error(f"No se pudo guardar el presupuesto: {exc}")
        if limite is not None:
            restante = limite - actual["total"]
            p1, p2, p3 = st.columns(3)
            p1.metric("Disponible", moneda(restante))
            p2.metric("Consumido", f"{float(actual['total'] / limite * 100):.1f}%")
            p3.metric("Desviación", moneda(actual["total"] - limite) if restante < 0 else "Dentro del límite")
            st.progress(min(float(actual["total"] / limite), 1.0))
            if (anio, mes) == (hoy_local().year, hoy_local().month):
                hoy = hoy_local()
                dias_mes = calendar.monthrange(anio, mes)[1]
                restantes = dias_mes - hoy.day
                diario = actual["total"] / hoy.day
                proyectado = diario * dias_mes
                esperado = limite * Decimal(hoy.day) / dias_mes
                k1, k2, k3 = st.columns(3)
                k1.metric("Ritmo vs plan", moneda(actual["total"] - esperado))
                k2.metric("Proyección al cierre", moneda(proyectado))
                k3.metric("Disponible por día restante", moneda(max(restante, 0) / restantes) if restantes else "Mes concluido")
                st.caption("Proyección lineal según el gasto acumulado y días transcurridos; es una referencia, no una predicción.")
        for grupo in GRUPOS:
            techo = Decimal(str(presupuesto[grupo]))
            if techo > 0:
                st.caption(f"{grupo}: {moneda(actual['grupos'][grupo])} de {moneda(techo)} · {float(actual['grupos'][grupo] / techo * 100):.1f}%")
                st.progress(min(float(actual["grupos"][grupo] / techo), 1.0))

        st.markdown("##### Hallazgos y acciones")
        ideas = hallazgos(actual, anterior, del_mes, limite)
        for idea in ideas:
            st.write("• " + idea)
        if actual["grupos"]["OXXO"] and mes:
            oxxo = [r for r in del_mes if r["grupo"] == "OXXO"]
            st.caption(f"Si se repitiera durante 12 meses el patrón de OXXO de este mes: {moneda(actual['grupos']['OXXO'] * 12)} al año. Es un escenario ilustrativo.")
            if len(oxxo) >= 4:
                st.write("• Puedes revisar la frecuencia de OXXO y establecer un límite específico para observarla mes a mes.")
        if st.button("✨ Interpretar cifras con Gemini", disabled=not bool(del_mes)):
            payload = {"periodo": f"{MESES[mes-1]} {anio}", "total": str(actual["total"]),
                       "movimientos": actual["conteo"], "bolsas": {k: str(v) for k, v in actual["grupos"].items()},
                       "categorias": [{"nombre": c["categoria"], "total": str(c["total"]), "cantidad": c["movimientos"]} for c in actual["categorias"][:12]],
                       "presupuesto": str(limite) if limite else None, "hallazgos": ideas}
            try:
                with st.spinner("Interpretando resultados..."):
                    respuesta = cliente_ai.models.generate_content(
                        model="gemini-3.5-flash-lite",
                        contents="Interpreta exclusivamente estas cifras de gasto personal en español. No recalcules ni inventes valores, ingresos, causas o diagnósticos. Explica 3 patrones y 2 acciones concretas, sin juzgar. Aclara que no es asesoría financiera. Datos calculados: " + json.dumps(payload, ensure_ascii=False))
                st.markdown(respuesta.text or "No hubo respuesta de Gemini.")
            except Exception as exc:
                st.warning(f"No se pudo generar la interpretación: {exc}")

    with tab_anual:
        registros_anio = [r for r in datos if r["anio"] == anio]
        mensual = {m: resumen([r for r in registros_anio if r["mes"] == m]) for m in range(1, 13)}
        meses_activos = [m for m in range(1, 13) if mensual[m]["conteo"]]
        total_anual = sum((mensual[m]["total"] for m in meses_activos), Decimal(0))
        cols = st.columns(3)
        cols[0].metric("Acumulado", moneda(total_anual))
        cols[1].metric("Promedio / mes con datos", moneda(total_anual / len(meses_activos)) if meses_activos else "—")
        cols[2].metric("Meses con movimientos", len(meses_activos))
        if meses_activos:
            serie = pd.DataFrame([{"Mes": MESES[m-1], "Total": float(mensual[m]["total"]),
                                   **{g: float(mensual[m]["grupos"][g]) for g in GRUPOS}}
                                  for m in meses_activos]).set_index("Mes")
            st.markdown("##### Gasto mensual")
            st.line_chart(serie[["Total"]], color="#34D399")
            st.markdown("##### Evolución por bolsa")
            st.line_chart(serie[list(GRUPOS)])
            st.dataframe(serie.style.format("${:,.2f}"), use_container_width=True)
            st.caption("Se muestran solo meses con movimientos válidos. Los meses sin datos no se tratan como gasto cero.")

    with tab_categoria:
        st.markdown(f"##### Categorías · {MESES[mes - 1]} {anio}")
        prev_categorias = {r["clave"]: r["total"] for r in anterior["categorias"]} if anterior else {}
        if actual["categorias"]:
            tabla = pd.DataFrame([{"Categoría": r["categoria"], "Total": moneda(r["total"]),
                                   "% del mes": f"{r['porcentaje']:.1f}%", "Movs.": r["movimientos"],
                                   "Ticket": moneda(r["promedio"]), "Máximo": moneda(r["maximo"]),
                                   "Mínimo": moneda(r["minimo"]),
                                   "Vs anterior": comparacion(r["total"], prev_categorias.get(r["clave"], Decimal(0))) if anterior else "Sin datos previos"}
                                  for r in actual["categorias"]])
            st.dataframe(tabla, hide_index=True, use_container_width=True)
            st.bar_chart(pd.DataFrame([{"Categoría": x["categoria"], "MXN": float(x["total"])}
                                       for x in actual["categorias"][:12]]).set_index("Categoría"), color="#34D399")
        categorias_anio = {}
        for r in datos:
            if r["anio"] == anio:
                categorias_anio.setdefault(r["clave"], r["categoria"])
        if categorias_anio:
            clave = st.selectbox("Explorar categoría", list(categorias_anio),
                                 format_func=lambda c: categorias_anio[c])
            detalle = [r for r in datos if r["anio"] == anio and r["clave"] == clave]
            por_mes = {m: resumen([r for r in detalle if r["mes"] == m]) for m in range(1, 13)}
            trayectoria = pd.DataFrame([{"Mes": MESES[m - 1], "Total": float(por_mes[m]["total"]),
                                         "Movimientos": por_mes[m]["conteo"],
                                         "Ticket promedio": float(por_mes[m]["total"] / por_mes[m]["conteo"]) if por_mes[m]["conteo"] else 0}
                                        for m in range(1, 13) if por_mes[m]["conteo"]]).set_index("Mes")
            st.line_chart(trayectoria[["Total"]], color="#A78BFA")
            st.dataframe(trayectoria.style.format({"Total": "${:,.2f}", "Ticket promedio": "${:,.2f}"}), use_container_width=True)

    with tab_calidad:
        st.markdown("##### Registros que requieren revisión")
        avisos = [a for a in alertas_por_anio.get(anio, []) if f"{anio} {MESES[mes-1]}" in a]
        for aviso in avisos:
            st.warning(aviso)
        if not avisos:
            st.success("No se detectaron fechas fuera del mes ni movimientos incompletos en este bloque.")
        st.markdown("##### Variaciones y valores atípicos")
        anomalas = detectar_anomalias(actual, anterior, del_mes)
        for aviso in anomalas:
            st.write("• " + aviso)
        if not anomalas:
            st.caption("Sin alertas según los umbrales actuales (2.5× mediana o aumentos de al menos 50%).")
        st.caption("Se excluyen las filas con fecha fuera del mes, importes inválidos y movimientos incompletos; el archivo no se corrige automáticamente.")
        st.markdown("##### Respaldo de presupuestos")
        st.download_button("Descargar presupuestos (.json)", exportar_presupuestos(),
                           file_name="presupuestos_gastos.json", mime="application/json")
        respaldo = st.file_uploader("Restaurar o migrar presupuestos (.json)", type="json", key="fin_import")
        if respaldo and st.button("Importar presupuestos del respaldo"):
            try:
                cantidad = importar_presupuestos(respaldo.getvalue().decode("utf-8"))
                st.success(f"Se importaron {cantidad} periodos.")
                st.rerun()
            except (ValueError, KeyError, TypeError, UnicodeDecodeError, sqlite3.Error) as exc:
                st.error(f"Respaldo inválido: {exc}")
        st.caption("Los presupuestos se guardan en un archivo SQLite separado. Si tu alojamiento reinicia el disco de la app, conserva y restaura el respaldo JSON.")
