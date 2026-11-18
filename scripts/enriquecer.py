"""Paso final tras la descarga (lo llama descargar_datos.py; también se puede lanzar solo, sin volver a descargar).

1. Ordena cada clase por capitalización en dólares (Yahoo da cada empresa en su moneda: wones, yenes, peniques…).
2. Recalcula los catalizadores con un emparejado estricto titular-empresa.
3. Redacta, para cada activo, por qué la señal es Comprar, Mantener o Evitar a 1 mes, 12 meses y 5 años, en lenguaje
   natural y solo con lo que dicen los datos: precio, analistas, resultados, calendario y prensa (con titular y fuente).
"""
import re
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parent.parent
CLEAN = BASE / "data" / "clean"
HOY = pd.Timestamp.today().normalize()

SUFIJOS = re.compile(r"\(the\)|,|\b(inc|incorporated|corp|corporation|company|co|plc|ltd|limited|holdings?|group|"
                     r"s\.?a\.?|se|ag|n\.?v\.?|s\.?p\.?a\.?|ab|asa|oyj|the|class [abc])\b\.?", re.I)
COMUNES = {"american", "first", "general", "united", "international", "national", "global", "new", "bank", "fast",
           "royal", "china", "great", "southern", "western", "eastern", "northern", "community", "real", "energy",
           "capital", "health", "pacific", "data", "digital", "home", "west", "east", "north", "south", "advanced",
           "applied", "world", "public", "life", "union", "standard", "alliance", "federal", "central", "main", "texas",
           "atlantic", "best", "smart", "big", "old", "us", "u.s.", "banco", "grupo", "compagnie", "deutsche", "british"}
TICKER_NO = {"ALL", "NOW", "CEO", "IPO", "ETF", "SEC", "FDA", "DOJ", "USA", "GDP", "FED", "CFO", "EPS", "AI"}


def claves_activo(nombre, nombre_largo, ticker):
    """Palabras que deben aparecer en un titular para que hable de verdad de esa empresa."""
    limpio = lambda t: re.sub(r"\s+", " ", SUFIJOS.sub(" ", str(t))).strip(" .-").lower()
    claves = {limpio(nombre)}
    if isinstance(nombre_largo, str) and nombre_largo:
        claves.add(limpio(nombre_largo))
    for c in list(claves):
        p = c.split(" ")[0]
        if len(c.split(" ")) > 1 and len(p) >= 4 and p not in COMUNES:
            claves.add(p)
    claves = {c for c in claves if len(c) >= 3}
    tk = str(ticker).split(".")[0].split("-")[0].upper()
    return claves, (tk if tk.isalpha() and len(tk) >= 3 and tk not in TICKER_NO else None)


def menciona(titular, claves, tk):
    t = str(titular)
    return any(re.search(r"\b" + re.escape(k) + r"\b", t.lower()) for k in claves) or \
        bool(tk and re.search(r"\b" + tk + r"\b", t))


# ---------- 1. Orden por capitalización en dólares ----------
def tipos_cambio(monedas):
    import yfinance as yf
    fx = {"USD": 1.0}
    for m in monedas:
        base = {"GBp": "GBP", "ILA": "ILS", "ZAc": "ZAR"}.get(m, m)
        if base in fx or not isinstance(base, str) or len(base) != 3:
            continue
        try:
            h = yf.Ticker(f"{base}USD=X").history(period="5d")["Close"].dropna()
            fx[base] = float(h.iloc[-1])
        except Exception:
            pass
    for m, base in (("GBp", "GBP"), ("ILA", "ILS"), ("ZAc", "ZAR")):
        if base in fx:
            fx[m] = fx[base] / 100                           # cotizan en peniques / agorot / céntimos
    return fx


def reordenar(act):
    fx = tipos_cambio(act["moneda"].dropna().unique())
    act["cap_usd"] = act["cap_bursatil"] * act["moneda"].map(fx)
    indices = act["clase"] == "Índices"
    clave = np.where(indices, act["orden"], -act["cap_usd"].fillna(0))
    act = act.assign(_k=clave).sort_values(["clase_orden", "_k"]).drop(columns="_k")
    act["orden"] = range(1, len(act) + 1)
    return act


# ---------- 3. Explicaciones ----------
FRASE_TEMA = {"Resultados": "sus resultados", "Analistas": "los cambios de recomendación de los analistas",
              "Acuerdos": "acuerdos u operaciones corporativas", "Directiva": "cambios en la dirección",
              "Macro": "el contexto macroeconómico", "Regulación": "asuntos regulatorios",
              "Litigios": "litigios e investigaciones", "Estrategia": "sus planes estratégicos"}
MESES = ["", "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
         "noviembre", "diciembre"]


def pc(x, dec=0, signo=True):
    if x is None or pd.isna(x):
        return "—"
    s = f"{x * 100:+.{dec}f}" if signo else f"{abs(x) * 100:.{dec}f}"
    return s.replace(".", ",") + " %"


def fecha_es(f):
    f = pd.Timestamp(f)
    return f"{f.day} de {MESES[f.month]}"


def cita(n):
    t = str(n["titular"]).strip()
    t = t if len(t) <= 110 else t[:107].rsplit(" ", 1)[0] + "…"
    return f"«{t}» ({n['fuente']})"


def unir(partes):
    partes = [p for p in partes if p]
    return partes[0] if len(partes) == 1 else ", ".join(partes[:-1]) + " y " + partes[-1] if partes else ""


def explicar(a, tec, nots, cats, res, f=None):
    if a.get("datos_dudosos") == 1:
        t = ("La serie de precios de este valor tiene saltos sospechosos en la fuente (por ejemplo, mezcla de libras y "
             "peniques), así que no se calcula ninguna señal hasta que los datos sean fiables.")
        return t, t, t, "Sin señal: los datos de precio de la fuente no son fiables."
    nombre = a["nombre"]
    r1m, d50, d200, r12 = a["var_1m"], tec.get("d50"), tec.get("d200"), tec.get("r12_1")
    dias = a.get("dias_a_resultados")
    hay_res = pd.notna(dias) and 0 <= dias <= 30
    pend = cats[cats["tema"] != "Resultados"] if len(cats) else cats

    # --- prensa de las últimas semanas: temas con más peso y su titular más fiable ---
    temas, tono_txt = [], ""
    if len(nots):
        con_tema = nots[nots["tema"].isin(FRASE_TEMA)]
        peso = con_tema.groupby("tema")["impacto_pts"].sum().sort_values(ascending=False)
        for t in peso.index[:2]:
            mejor = con_tema[con_tema["tema"] == t].sort_values(["impacto_pts", "nivel_fuente"], ascending=[False, True]).iloc[0]
            temas.append((t, mejor))
        tono = nots["tono"].mean()
        tono_txt = " con un tono mayoritariamente negativo" if tono < -0.15 else \
            " con un tono mayoritariamente positivo" if tono > 0.15 else ""
    if temas:
        prensa = (f"En la prensa de las últimas semanas pesan " +
                  unir([f"{FRASE_TEMA[t]} ({cita(n)})" for t, n in temas]) + tono_txt + ".")
    elif len(nots):
        prensa = f"Las noticias recientes no apuntan a ningún asunto concreto; la más destacada: {cita(nots.iloc[0])}."
    else:
        prensa = "Apenas hay noticias propias recientes en las fuentes consultadas."
    pendiente = ""
    if len(pend):
        c = pend.sort_values("puntuacion", ascending=False).iloc[0]
        pendiente = (f"Sigue abierto un asunto que puede mover el precio ({c['tipo'].lower()}: {cita(c)}): "
                     f"{c['si_sale_bien']}, en casos parecidos se movió de media {pc(c['mov_favorable'])}; "
                     f"{c['si_sale_mal']}, {pc(c['mov_desfavorable'])}.")
    cuando = ""
    if hay_res:
        cuando = "hoy" if dias == 0 else "mañana" if dias == 1 else f"el {fecha_es(a['prox_resultados'])}"
        mov = a.get("mov_medio_resultados")
        resultados_txt = (f"Publica resultados {cuando}" +
                          (f"; en sus últimas publicaciones se movió de media un {pc(mov, signo=False)} en la sesión." if pd.notna(mov) else "."))
    else:
        resultados_txt = ""
    riesgo = a["riesgo"]
    riesgo_txt = (f"Es un valor muy volátil (riesgo {riesgo} de 7)." if riesgo >= 6 else
                  f"Su riesgo es moderado ({riesgo} de 7)." if riesgo >= 4 else f"Su riesgo es bajo ({riesgo} de 7).")

    # --- 1 mes ---
    if pd.notna(r1m) and r1m <= -0.08:
        mom = f"Ha caído un {pc(r1m, signo=False)} en el último mes"
    elif pd.notna(r1m) and r1m >= 0.08:
        mom = f"Ha subido un {pc(r1m, signo=False)} en el último mes"
    else:
        mom = f"En el último mes apenas se ha movido ({pc(r1m, 1)})"
    if d50 is not None and abs(d50) >= 0.03:
        mom += f" y cotiza un {pc(d50, signo=False)} por {'debajo' if d50 < 0 else 'encima'} de su media de 50 sesiones"
    if pd.notna(r1m) and r1m <= -0.08 and (d50 or 0) < 0:
        mom += ": la tendencia inmediata es bajista."
    elif pd.notna(r1m) and r1m >= 0.15:
        mom += ": el impulso es fuerte, aunque tras subidas tan rápidas son habituales las recogidas de beneficios."
    else:
        mom += "."
    rc = a["rec_corto"]
    if rc == "Evitar":
        esperar = unir([x for x in ("a que publique resultados" if hay_res and dias <= 10 else "",
                                    "a que se aclare lo pendiente" if pendiente else "",
                                    "a que la caída se estabilice" if (r1m or 0) < -0.08 else "") if x])
        concl_c = "Con estas señales, a un mes es mejor no entrar" + (f": conviene esperar {esperar}." if esperar else ".")
    elif rc == "Comprar":
        concl_c = "Las señales a un mes son favorables."
    else:
        concl_c = "Las señales a un mes están equilibradas: no hay un motivo claro ni para entrar ni para salir."
    est_c = f"Escenario central a un mes: {pc(a['corto_p50'], 1)} (rango probable de {pc(a['corto_p10'])} a {pc(a['corto_p90'])})."
    exp_c = " ".join(x for x in (mom, prensa, pendiente, resultados_txt, riesgo_txt, est_c, concl_c) if x)

    # --- 12 meses ---
    tend = ""
    if d200 is not None:
        tend = f"Cotiza un {pc(d200, signo=False)} por {'debajo' if d200 < 0 else 'encima'} de su media de 200 sesiones"
        r1a = a.get("var_1a")
        if pd.notna(r1a):
            tend += f" y en el último año {'ha subido' if r1a >= 0 else 'ha caído'} un {pc(r1a, signo=False)}"
        tend += "."
    ana = ""
    if a.get("n_analistas", 0) >= 3 and pd.notna(a.get("potencial")):
        ana = (f"Los {int(a['n_analistas'])} analistas que la siguen le ven de media un recorrido de {pc(a['potencial'])} "
               f"(precio objetivo {a['obj_medio']:,.2f} {a['moneda']}".replace(",", "X").replace(".", ",").replace("X", ".") +
               (f"; consenso: {a['consenso']}" if isinstance(a.get("consenso"), str) and a["consenso"] else "") + ").")
        if a.get("notas_bancos_90d", 0) >= 3:
            ana += f" En los últimos 90 días hubo {int(a['mejoras_90d'])} {'mejora' if a['mejoras_90d'] == 1 else 'mejoras'} y {int(a['rebajas_90d'])} {'rebaja' if a['rebajas_90d'] == 1 else 'rebajas'} de recomendación."
    elif a["clase"] == "Acciones":
        ana = "Apenas tiene cobertura de analistas, así que la previsión se apoya sobre todo en su propio historial."
    sorp = ""
    if len(res):
        k = int((res["sorpresa_pct"] > 0).sum())
        sorp = (f"Ha superado las previsiones de beneficio en {k} de sus últimos {len(res)} trimestres"
                f" ({pc(res['sorpresa_pct'].mean() / 100, 1)} de media).")
    if a["medio_p90"] > 3 or a["medio_p10"] < -0.9:
        rango_m = "; el rango probable es demasiado amplio para ser útil porque el activo es muy volátil"
    else:
        rango_m = f" (rango probable de {pc(a['medio_p10'])} a {pc(a['medio_p90'])})"
    perd = ("una probabilidad de pérdidas baja" if a["medio_perdida"] < 0.05 else
            f"un {pc(a['medio_perdida'], signo=False)} de probabilidad de acabar en pérdidas")
    est_m = f"El modelo estadístico estima un escenario central a 12 meses de {pc(a['medio_p50'], 1)}{rango_m}, con {perd}."
    rm = a["rec_medio"]
    concl_m = {"Comprar": "En conjunto, a 12 meses las señales apuntan al alza.",
               "Evitar": "En conjunto, a 12 meses pesan más las señales negativas.",
               "Mantener": "En conjunto, a 12 meses las señales positivas y negativas se compensan."}[rm]
    fund_m = ""
    if f is not None and pd.notna(f.get("nota_fund")):
        fund_m = (f"Los fundamentales del negocio ({f['nota_fund']:.0f}/100 frente a su sector) "
                  + ("suman a la señal." if f["nota_fund"] >= 60 else "restan a la señal." if f["nota_fund"] <= 40 else "son neutros."))
    anclada = ""
    if a["clase"] == "Acciones" and pd.notna(a.get("vol_anual")) and a["vol_anual"] < 0.12:
        anclada = (f"Ojo: su cotización apenas se mueve (volatilidad del {a['vol_anual'] * 100:.0f} %), algo propio de una "
                   "empresa con una oferta de compra o una fusión pendiente; si es así, el recorrido queda limitado al precio "
                   "ofrecido y el objetivo de los analistas puede estar desfasado.")
    exp_m = " ".join(x for x in (tend, anclada, ana, sorp, fund_m, est_m, concl_m) if x)

    # --- 5 años ---
    dd = a.get("max_caida_5a")
    rango_l = ("; con tanta volatilidad el rango probable es demasiado amplio para ser útil" if a["largo_p90"] > 3 or a["largo_p10"] < -0.9
               else f" (rango probable de {pc(a['largo_p10'])} a {pc(a['largo_p90'])})")
    largo = (f"A cinco años, el modelo estima un escenario central de {pc(a['largo_p50'])} acumulado{rango_l}. Su volatilidad es del {a['vol_anual'] * 100:.0f} %"
             + (f" y en los últimos cinco años llegó a caer un {pc(dd, signo=False)} desde máximos." if pd.notna(dd) else "."))
    rl = a["rec_largo"]
    concl_l = {"Comprar": "Según el modelo, la rentabilidad esperada compensa el riesgo que se asume.",
               "Evitar": "Según el modelo, la rentabilidad esperada no compensa el riesgo que se asume.",
               "Mantener": "Según el modelo, la rentabilidad esperada está en línea con el riesgo, sin ventaja clara."}[rl]
    exp_l = " ".join(x for x in (texto_fund(f, a), largo, concl_l) if x)

    # --- resumen en una frase ---
    if rc == "Evitar":
        rz_c = (" a la espera de sus resultados" if hay_res and dias <= 10 else
                f" hasta que se aclare {FRASE_TEMA.get(pend.iloc[0]['tema'], 'lo pendiente')}" if len(pend) else
                " por la caída reciente" if (r1m or 0) < -0.08 else "")
    elif rc == "Comprar":
        rz_c = " por su buen impulso reciente" if (r1m or 0) > 0.05 else ""
    else:
        rz_c = f" tras subir un {pc(r1m, signo=False)} en un mes" if (r1m or 0) >= 0.15 else \
            " a la espera de sus resultados" if hay_res and dias <= 10 else ""
    pot = a.get("potencial")
    if a.get("n_analistas", 0) >= 3 and pd.notna(pot):
        rz_m = (f", porque los analistas le ven recorrido ({pc(pot)})" if pot >= 0.15 else
                ", con los analistas viendo poco recorrido" if pot < 0.05 else "")
    else:
        rz_m = ", con su tendencia por encima de la media" if (d200 or 0) > 0.05 else \
            ", mientras siga por debajo de su tendencia" if (d200 or 0) < -0.05 else ""
    if anclada:
        rz_m = ", porque su cotización está casi inmóvil (posible OPA o fusión) y el recorrido queda limitado"
    elif f is not None and ((f.get("margen_oper") or 0) < 0 or f.get("per_fwd") == 9999) and rm != "Comprar":
        rz_m = (f": aunque los analistas le ven recorrido ({pc(pot)}), la empresa está en pérdidas"
                if pd.notna(pot) and pot >= 0.15 else ", porque la empresa está en pérdidas")
    bien, mal = rasgos(f)
    if rl == "Comprar" and bien:
        rz_l = " por " + unir(bien[:2])
    elif rl == "Evitar" and mal:
        rz_l = " por " + unir(mal[:2])
    elif rl == "Mantener" and bien and (mal or riesgo >= 6):
        rz_l = " porque, pese a " + unir(bien[:1]) + ", " + ("pesan " + unir(mal[:1]) if mal else "su riesgo es alto")
    else:
        rz_l = " por su alto riesgo" if rl != "Comprar" and riesgo >= 6 else ""
    resumen = (f"{ETQ[rc]} a corto plazo{rz_c}; {ETQ[rm].lower()} a 12 meses{rz_m}; {ETQ[rl].lower()} a 5 años{rz_l}.")
    return exp_c, exp_m, exp_l, resumen


# ---------- 4. Fundamentales: el negocio frente a su sector ----------
# (columna, campo de Yahoo, nombre, formato, sentido (+1 = más es mejor), bloque de la nota)
FUND = [("crec_ventas", "revenueGrowth", "Crecimiento de las ventas (interanual)", "pct", 1, "Crecimiento"),
        ("crec_bpa", "earningsGrowth", "Crecimiento del beneficio (interanual)", "pct", 1, "Crecimiento"),
        ("margen_bruto", "grossMargins", "Margen bruto", "pct", 1, None),
        ("margen_oper", "operatingMargins", "Margen operativo", "pct", 1, "Rentabilidad"),
        ("roe", "returnOnEquity", "Rentabilidad sobre fondos propios (ROE)", "pct", 1, "Rentabilidad"),
        ("deuda_fp", "debtToEquity", "Deuda frente a fondos propios", "veces", -1, "Solidez"),
        ("fcf_rend", None, "Flujo de caja libre sobre capitalización", "pct", 1, "Solidez"),
        ("per_fwd", "forwardPE", "PER esperado (precio / beneficio de 12 meses)", "x", -1, "Valoración"),
        ("ev_ebitda", "enterpriseToEbitda", "Valor de empresa / EBITDA", "x", -1, "Valoración"),
        ("div_rent", None, "Rentabilidad por dividendo", "pct", 1, None)]
BLOQUES = ["Crecimiento", "Rentabilidad", "Solidez", "Valoración"]


def fundamentales(act):
    """Datos del negocio de cada acción (Yahoo Finance) y su posición frente a las demás de su sector."""
    import json
    cache_f = BASE / "data" / "raw" / "info_cache.json"
    info = json.loads(cache_f.read_text(encoding="utf-8")) if cache_f.exists() else {}
    filas = []
    for _, a in act.iterrows():
        i = info.get(a["ticker"], {}) if a["clase"] == "Acciones" else {}
        f = {"id": a["id"], "descripcion": (i.get("longBusinessSummary") or "").strip(),
             "empleados": i.get("fullTimeEmployees"), "ciudad": i.get("city") or "", "ventas": i.get("totalRevenue"),
             "moneda_fin": i.get("financialCurrency") or a["moneda"]}
        for col, campo, *_ in FUND:
            v = i.get(campo) if campo else None
            f[col] = float(v) if isinstance(v, (int, float)) and np.isfinite(v) else np.nan
        if pd.notna(f["deuda_fp"]):
            f["deuda_fp"] /= 100                                     # Yahoo lo da en %
        fcf, cap = i.get("freeCashflow"), i.get("marketCap")
        if fcf and cap and (i.get("financialCurrency") in (None, i.get("currency"))):
            f["fcf_rend"] = fcf / cap
        dy = i.get("dividendYield")
        f["div_rent"] = dy / 100 if isinstance(dy, (int, float)) else (0.0 if i else np.nan)
        for col in ("per_fwd", "ev_ebitda"):                         # múltiplo negativo = pierde dinero: lo peor
            if pd.notna(f[col]) and f[col] <= 0:
                f[col] = 9999.0
        filas.append(f)
    fu = pd.DataFrame(filas).set_index("id")
    ai = act.set_index("id")
    # en finanzas se compara por industria (un banco no es una gestora) si hay bastantes
    fu["sector"] = np.where(ai["sector"].eq("Financial Services"), ai["industria"].fillna(""), ai["sector"])
    n_ind = fu.groupby("sector")["sector"].transform("count")
    fu["sector"] = np.where(ai["sector"].eq("Financial Services") & (n_ind < 8), ai["sector"], fu["sector"])
    fu["es_accion"] = ai["clase"].eq("Acciones")
    banco = ai["industria"].fillna("").str.contains("Bank|Insurance|Credit", regex=True)
    fu.loc[banco, ["deuda_fp", "fcf_rend"]] = np.nan          # su deuda y su caja no se leen como en una industria
    # percentil dentro del sector (si el sector tiene menos de 8 datos, frente a todas las acciones)
    for col, _, _, _, sentido, bloque in FUND:
        if not bloque:
            fu[f"{col}_med"] = fu[fu["es_accion"]].groupby("sector")[col].transform("median")
            continue
        base = fu[fu["es_accion"] & fu[col].notna()]
        r_sector = base.groupby("sector")[col].rank(pct=True)
        n_sector = base.groupby("sector")[col].transform("count")
        r_total = base[col].rank(pct=True)
        r = np.where(n_sector >= 8, r_sector, r_total)
        fu.loc[base.index, f"{col}_pct"] = r if sentido > 0 else 1 - r + 1 / len(base)
        fu[f"{col}_med"] = fu[fu["es_accion"]].groupby("sector")[col].transform(
            lambda s: s[s < 9999].median())
    for b in BLOQUES:
        cols = [f"{c}_pct" for c, *_, bl in FUND if bl == b]
        fu[f"b_{b}"] = fu[cols].mean(axis=1)
    nb = fu[[f"b_{b}" for b in BLOQUES]].notna().sum(axis=1)
    fu["nota_fund"] = np.where(nb >= 2, fu[[f"b_{b}" for b in BLOQUES]].mean(axis=1) * 100, np.nan)
    fu["n_sector"] = fu[fu["es_accion"]].groupby("sector")["sector"].transform("count")
    return fu


def fmt_fund(v, tipo):
    if v is None or pd.isna(v):
        return "—"
    if tipo == "pct":
        return pc(v, 0 if abs(v) >= 0.1 else 1, signo=False) if v >= 0 else pc(v, 0)
    if v >= 9999:
        return "pérdidas"
    return f"{v:.1f}".replace(".", ",") + (" veces" if tipo == "veces" else "")


def rasgos(f):
    """Lo mejor y lo peor del negocio en frases cortas (solo lo que se aleja claramente del sector)."""
    bien, mal = [], []
    if f is None:
        return bien, mal
    def p(c):
        return f.get(f"{c}_pct")
    if pd.notna(f.get("crec_ventas")) and (p("crec_ventas") or 0) >= 0.7:
        bien.append(f"ventas creciendo un {pc(f['crec_ventas'], signo=False)}")
    elif pd.notna(f.get("crec_ventas")) and f["crec_ventas"] < 0:
        mal.append(f"ventas cayendo ({pc(f['crec_ventas'])})")
    if pd.notna(f.get("margen_oper")) and (p("margen_oper") or 0) >= 0.7:
        bien.append(f"un margen operativo del {pc(f['margen_oper'], signo=False)}")
    elif pd.notna(f.get("margen_oper")) and f["margen_oper"] < 0:
        mal.append("pérdidas operativas")
    elif pd.notna(f.get("margen_oper")) and (p("margen_oper") or 1) <= 0.25:
        mal.append(f"márgenes bajos ({pc(f['margen_oper'], 1, signo=False)})")
    if pd.notna(f.get("deuda_fp")) and (p("deuda_fp") or 1) <= 0.2 and f["deuda_fp"] > 1:
        mal.append(f"mucha deuda ({fmt_fund(f['deuda_fp'], 'veces')} sus fondos propios)")
    if pd.notna(f.get("per_fwd")) and f["per_fwd"] < 9999:
        med = f.get("per_fwd_med")
        if (p("per_fwd") or 0) >= 0.7 and pd.notna(med):
            bien.append(f"un PER esperado de {fmt_fund(f['per_fwd'], 'x')} (su sector, {fmt_fund(med, 'x')})")
        elif (p("per_fwd") or 1) <= 0.25 and pd.notna(med):
            mal.append(f"una valoración exigente (PER esperado de {fmt_fund(f['per_fwd'], 'x')} frente a {fmt_fund(med, 'x')} en su sector)")
    elif f.get("per_fwd") == 9999:
        mal.append("beneficios esperados negativos")
    return bien, mal


def texto_fund(f, a):
    if f is None or pd.isna(f.get("nota_fund")):
        return ""
    bien, mal = rasgos(f)
    sector = f"las {int(f['n_sector'])} empresas de su sector ({f['sector']})" if f.get("n_sector", 0) >= 8 else "el resto de acciones"
    t = (f"Frente a {sector}, su negocio saca {f['nota_fund']:.0f} sobre 100 (50 es la empresa típica de su sector; "
         "la nota compara con sus rivales, no dice si el sector es bueno)")
    det = []
    if bien:
        det.append("a favor, " + unir(bien))
    if mal:
        det.append("en contra, " + unir(mal))
    return t + (": " + "; ".join(det) if det else "") + "."


# ---------- 5. Desglose de la puntuación ----------
CONS_PTS = {"Compra fuerte": 8, "Comprar": 4, "Mantener": 0, "Infraponderar": -4, "Vender": -8}


def clipf(x, a, b):
    return max(a, min(b, x))


def desglose(a, tec, ana, res4, fpts_m, fpts_l):
    """Puntos que suma o resta cada factor. Si la descarga ya los guardó (comp_*), se usan tal cual; si no,
    se reconstruyen con los mismos pesos y lo que no se puede recalcular (tono de prensa) queda como resto."""
    sig = a["sigma"] if pd.notna(a.get("sigma")) and a["sigma"] > 0 else 0.3
    if a["clase"] == "Acciones":
        sig = max(sig, 0.15)
    riesgo = -(a["riesgo"] - 4) * 2
    out = {}
    for h in ("corto", "medio", "largo"):
        guardado = a.get(f"comp_{h}")
        if isinstance(guardado, str) and guardado:
            comp = [(float(x.split("|", 1)[0]), x.split("|", 1)[1]) for x in guardado.split(" ;; ")]
        else:
            comp = []
            if h == "corto":
                if tec.get("d50") is not None:
                    comp.append((clipf(tec["d50"] / (sig * np.sqrt(50 / 252)), -2, 2) * 10,
                                 f"precio {pc(tec['d50'])} frente a su media de 50 sesiones"))
                if pd.notna(a.get("var_1m")):
                    comp.append((clipf(a["var_1m"] / (sig * np.sqrt(21 / 252)), -2, 2) * 8, f"último mes {pc(a['var_1m'])}"))
                comp.append((riesgo, f"riesgo {a['riesgo']}/7"))
                resto = "tono de la prensa de las dos últimas semanas"
            elif h == "medio":
                if tec.get("d200") is not None:
                    comp.append((clipf(tec["d200"] / (sig * np.sqrt(200 / 252)), -2, 2) * 10,
                                 f"precio {pc(tec['d200'])} frente a su media de 200 sesiones"))
                if tec.get("r12_1") is not None:
                    comp.append((clipf(tec["r12_1"] / sig, -2, 2) * 8, f"tendencia de 12 meses {pc(tec['r12_1'])}"))
                if pd.notna(a.get("potencial")):
                    comp.append((clipf((a["potencial"] - 0.10) / sig, -2, 2) * 10,
                                 f"objetivo medio de {int(a['n_analistas'])} analistas: {pc(a['potencial'])}"))
                if CONS_PTS.get(a.get("consenso")):
                    comp.append((CONS_PTS[a["consenso"]], f"consenso de los analistas: {a['consenso']}"))
                if len(ana) >= 3:
                    mej, reb = (ana["accion"] == "Mejora").sum(), (ana["accion"] == "Rebaja").sum()
                    sub = ana.dropna(subset=["precio_objetivo", "objetivo_anterior"])
                    neto = np.sign(sub["precio_objetivo"] - sub["objetivo_anterior"]).sum()
                    comp.append((clipf((mej - reb + 0.5 * neto) / len(ana) * 12, -8, 8),
                                 f"{len(ana)} notas de bancos en 90 días ({mej} mejoras, {reb} rebajas)"))
                if len(res4):
                    m = res4["sorpresa_pct"].mean()
                    comp.append((clipf(m / 5, -1, 1) * 5, f"beneficio frente a lo esperado: {pc(m / 100, 1)} de media en {len(res4)} trimestres"))
                comp.append((riesgo, f"riesgo {a['riesgo']}/7"))
                resto = "tono de la prensa de los últimos 60 días"
            else:
                dd = a.get("max_caida_5a")
                if pd.notna(dd) and dd < -0.4:
                    comp.append((-8 if dd < -0.6 else -4, f"peor caída en 5 años {pc(dd)}"))
                if CONS_PTS.get(a.get("consenso")):
                    comp.append((CONS_PTS[a["consenso"]] * 0.5, f"consenso de los analistas: {a['consenso']}"))
                comp.append((-(a["riesgo"] - 6) * 2, f"riesgo {a['riesgo']}/7 (frente al 6/7 típico de una acción)"))
                resto = f"rentabilidad esperada ({pc(a['mu'], 1)} al año) frente a su volatilidad ({pc(sig, signo=False)})"
            base = a[f"score_{h}_base"]
            r = base - 50 - sum(c for c, _ in comp)
            if abs(r) >= 1 and 0 < base < 100:
                comp.append((r, resto))
        f = {"medio": fpts_m, "largo": fpts_l}.get(h)
        if f:
            comp.append(f)
        out[h] = [(c, t) for c, t in comp if abs(c) >= 0.5]
    return out


def html_desglose(comp, score, rec):
    filas = ""
    for c, t in sorted(comp, key=lambda x: -abs(x[0])):
        w = min(abs(c) / 25 * 50, 50)
        col = "#047857" if c > 0 else "#B91C1C"
        barra = (f"<span style='display:inline-block;width:100px;height:8px;position:relative;background:#F1F5F9;border-radius:4px;vertical-align:0'>"
                 f"<span style='position:absolute;top:0;height:8px;border-radius:4px;background:{col};"
                 f"{'left:50px' if c > 0 else f'left:{50 - w:.0f}px'};width:{w:.0f}px'></span>"
                 f"<span style='position:absolute;left:49px;top:-2px;width:1px;height:12px;background:#94A3B8'></span></span>")
        filas += (f"<tr><td style='padding:2px 6px 2px 0;color:#475569'>{t}</td><td style='width:104px'>{barra}</td>"
                  f"<td style='text-align:right;width:34px;color:{col};font-family:Segoe UI Semibold'>{c:+.0f}</td></tr>")
    return (f"<table style='width:100%;border-collapse:collapse;font-size:12px'>"
            f"<tr><td style='padding:2px 0;color:#64748B'>Punto de partida (neutral)</td><td></td>"
            f"<td style='text-align:right;color:#64748B'>50</td></tr>{filas}"
            f"<tr style='border-top:1px solid #E2E8F0'><td style='padding:3px 0'><b>Puntuación</b> "
            f"<span style='color:#64748B'>(62 o más: Favorable · 38 o menos: Desfavorable)</span></td><td></td>"
            f"<td style='text-align:right'><b>{score:.0f}</b></td></tr></table>")


def html_empresa(a, f, desg):
    css_h = "font-family:Segoe UI Semibold;font-size:13.5px;color:#0F172A;margin:12px 0 4px"
    partes = []
    if f is not None and f.get("descripcion"):
        datos = [x for x in (a["sector"], a["industria"], ", ".join(y for y in (f.get("ciudad"), a["pais"]) if isinstance(y, str) and y),
                             f"{int(f['empleados']):,} empleados".replace(",", ".") if pd.notna(f.get("empleados")) else "",
                             (f"ventas de {f['ventas'] / 1e9:,.1f} mil millones {f['moneda_fin']}".replace(".", ",") if f['ventas'] >= 1e9
                              else f"ventas de {f['ventas'] / 1e6:,.0f} millones {f['moneda_fin']}".replace(",", ".")) if pd.notna(f.get("ventas")) and f["ventas"] else "")
                 if isinstance(x, str) and x]
        partes.append(f"<div style='{css_h};margin-top:0'>A qué se dedica</div>"
                      f"<div style='color:#475569;font-size:11.5px;margin-bottom:4px'>{' · '.join(datos)}</div>"
                      f"<div style='color:#334155'>{f['descripcion']}</div>"
                      f"<div style='color:#94A3B8;font-size:11px'>Descripción de Yahoo Finance (en inglés).</div>")
    elif a["clase"] != "Acciones":
        partes.append(f"<div style='{css_h};margin-top:0'>Qué es</div><div style='color:#334155'>{a['nombre_largo']} · {a['sector']}"
                      f"{' · ' + a['mercado'] if isinstance(a.get('mercado'), str) and a['mercado'] else ''}.</div>")
    if f is not None and pd.notna(f.get("nota_fund")):
        sector = f["sector"] if f.get("n_sector", 0) >= 8 else "todas las acciones"
        filas = ""
        for col, _, nombre, tipo, sentido, bloque in FUND:
            v = f.get(col)
            if pd.isna(v):
                continue
            pct = f.get(f"{col}_pct")
            if bloque and pd.notna(pct):
                juicio, colr = (("Mejor que la mayoría", "#047857") if pct >= 0.67 else ("Peor que la mayoría", "#B91C1C") if pct <= 0.33
                                else ("En la media", "#64748B"))
                if bloque == "Valoración":
                    juicio = {"#047857": "Más barata", "#B91C1C": "Más cara", "#64748B": "En la media"}[colr]
            else:
                juicio, colr = "", "#64748B"
            filas += (f"<tr style='border-bottom:1px solid #F1F5F9'><td style='padding:3px 0'>{nombre}</td>"
                      f"<td style='text-align:right'><b>{fmt_fund(v, tipo)}</b></td>"
                      f"<td style='text-align:right;color:#64748B'>{fmt_fund(f.get(col + '_med'), tipo)}</td>"
                      f"<td style='text-align:right;color:{colr};font-size:11.5px'>{juicio}</td></tr>")
        bl = " · ".join(f"{b} {f['b_' + b] * 100:.0f}" for b in BLOQUES if pd.notna(f.get("b_" + b)))
        partes.append(f"<div style='{css_h}'>Cómo es el negocio frente a su sector · nota {f['nota_fund']:.0f}/100</div>"
                      f"<div style='color:#64748B;font-size:11.5px;margin-bottom:3px'>Comparado con {sector} ({bl}). "
                      f"50 = la empresa típica de su sector. Datos de los últimos 12 meses publicados por la empresa, vía Yahoo Finance.</div>"
                      f"<table style='width:100%;border-collapse:collapse;font-size:12px'><tr style='color:#64748B;font-size:11px'>"
                      f"<td></td><td style='text-align:right'>Empresa</td><td style='text-align:right'>Mediana sector</td><td></td></tr>{filas}</table>")
    nombres = {"corto": "A 1 mes", "medio": "A 12 meses", "largo": "A 5 años"}
    partes.append(f"<div style='{css_h}'>De dónde sale cada puntuación</div>"
                  "<div style='color:#64748B;font-size:11.5px'>Cada señal parte de 50 puntos; cada factor suma (verde) o resta (rojo).</div>")
    for h in ("corto", "medio", "largo"):
        partes.append(f"<div style='margin-top:6px;font-family:Segoe UI Semibold;font-size:12px'>{nombres[h]}: {ETQ.get(a[f'rec_{h}'], a[f'rec_{h}'])}</div>"
                      + html_desglose(desg[h], a[f"score_{h}"], a[f"rec_{h}"]))
    return "<div style='font-family:Segoe UI;font-size:12.5px;line-height:1.45;color:#0F172A'>" + "".join(partes) + "</div>"


# ---------- 6. Correcciones de la revisión financiera (Cibran) ----------
ETQ = {"Comprar": "Favorable", "Mantener": "Neutral", "Evitar": "Desfavorable"}
ETQ_INV = {v: k for k, v in ETQ.items()}


def aviso_objetivo(a):
    """Aviso cuando el precio objetivo de los analistas está muy lejos del precio (p. ej. Samsung, +146 %).
    Suele indicar objetivos desfasados tras un movimiento fuerte, o una media poco representativa."""
    p = a.get("potencial")
    if a["clase"] != "Acciones" or pd.isna(p) or a.get("datos_dudosos") == 1:
        return ""
    px, ob, oa, n = a["precio"], a.get("obj_bajo"), a.get("obj_alto"), int(a.get("n_analistas") or 0)
    todos_arriba = pd.notna(ob) and ob > px * 1.4
    todos_abajo = pd.notna(oa) and oa < px * 0.8
    if not (p >= 0.8 or p <= -0.4 or todos_arriba or todos_abajo):
        return ""
    t = (f"Ojo con el precio objetivo: la media de {n} analistas queda un {pc(p)} "
         f"{'por encima' if p > 0 else 'por debajo'} del precio, una distancia poco habitual")
    if todos_arriba:
        t += f", e incluso el objetivo más bajo está un {pc(ob / px - 1)} por encima"
    elif todos_abajo:
        t += f", e incluso el más alto está un {pc(oa / px - 1)} por debajo"
    m1, m12 = a.get("var_1m"), a.get("var_1a")
    if p > 0 and pd.notna(m1) and m1 <= -0.15:
        t += f". El precio ha caído un {pc(m1, signo=False)} en el último mes y los analistas suelen tardar en revisar sus objetivos"
    elif p < 0 and pd.notna(m1) and m1 >= 0.2:
        t += f". El precio ha subido un {pc(m1)} en el último mes y los objetivos pueden no haberse puesto al día"
    elif p < 0 and pd.notna(m12) and m12 >= 1:
        t += f". El precio se ha multiplicado en un año ({pc(m12)}) y los objetivos van por detrás"
    elif n < 5:
        t += ". Con tan pocos analistas la media es poco representativa"
    else:
        t += ". Puede deberse a objetivos sin actualizar o a una diferencia de moneda o de clase de acción en la fuente"
    return t + ". Por prudencia, en la señal a 12 meses este factor cuenta solo la mitad."


def ajustes_revision(act, dd):
    """Aplica a los datos ya descargados lo que la descarga nueva hace de serie (para no esperar a mañana):
    tope y suelo de volatilidad en las previsiones, riesgo no contado dos veces a 5 años y sesgo alcista de los
    objetivos de analistas. Una sola vez (columna ajuste_v2)."""
    if "ajuste_v2" in act:
        return act, set()
    nuevos = act["comp_medio"].apply(lambda x: isinstance(x, str) and bool(x)) if "comp_medio" in act else pd.Series(False, index=act.index)
    tocados = set()
    for c in ("score_medio_base", "score_largo_base"):
        act[c] = act[c].astype(float)
    for k, a in act.iterrows():
        if nuevos[k]:
            continue
        acc = a["clase"] == "Acciones"
        sz = max(a["sigma"], 0.15) if acc else a["sigma"]
        if pd.notna(a.get("riesgo")):
            act.loc[k, "score_largo_base"] = float(np.clip(a["score_largo_base"] + 4, 0, 100))
        pot = a.get("potencial")
        if pd.notna(pot) and sz > 0:
            d = 10 * (clipf((pot - 0.10) / sz, -2, 2) - clipf(pot / sz, -2, 2))
            act.loc[k, "score_medio_base"] = float(np.clip(a["score_medio_base"] + d, 0, 100))
        # previsiones: volatilidad entre 15 % (acciones) y 80 %
        s_new = min(max(a["sigma"], 0.15 if acc else 0), dd.SIGMA_MAX)
        if pd.notna(a["sigma"]) and abs(s_new - a["sigma"]) > 1e-9:
            tocados.add(a["id"])
            for hz, h in (("corto", 21 / 252), ("medio", 1.0), ("largo", 5.0)):
                p50 = a[f"{hz}_p50"]
                if pd.isna(p50):
                    continue
                m = np.log1p(p50)
                sd = s_new * np.sqrt(h)
                act.loc[k, f"{hz}_p10"] = np.expm1(m + dd.Z["p10"] * sd)
                act.loc[k, f"{hz}_p90"] = np.expm1(m + dd.Z["p90"] * sd)
                act.loc[k, f"{hz}_perdida"] = dd.norm_cdf(-m / sd)
            act.loc[k, "sigma_prev"] = s_new
    act["ajuste_v2"] = 1
    return act, tocados


def bandas_serie(act, tocados):
    """Rehace las bandas del gráfico de los activos cuya volatilidad se ha acotado."""
    if not tocados:
        return
    import descargar_datos as dd
    ruta = CLEAN / "serie.csv"
    s = pd.read_csv(ruta)
    sel = s["id"].isin(tocados) & s["mediana"].notna()
    sig = act.set_index("id")["sigma_prev"]
    anio = act.set_index("id")["clase"].map(lambda c: 365 if c == "Cripto" else 252)
    sub = s[sel].copy()
    sub["k"] = sub.groupby("id").cumcount()                      # 0 = arranque (último cierre), 1.. = días de previsión
    sd = sub["id"].map(sig) * np.sqrt(sub["k"] / sub["id"].map(anio))
    for col, z in (("b80_inf", "p10"), ("b80_sup", "p90"), ("b95_inf", "p025"), ("b95_sup", "p975")):
        sub[col] = sub["mediana"] * np.exp(dd.Z[z] * sd)
    s.loc[sel, ["b80_inf", "b80_sup", "b95_inf", "b95_sup"]] = sub[["b80_inf", "b80_sup", "b95_inf", "b95_sup"]]
    tmp = ruta.with_suffix(".tmp")
    s.to_csv(tmp, index=False, encoding="utf-8")
    tmp.replace(ruta)


def tecnicos():
    s = pd.read_csv(CLEAN / "serie.csv", usecols=["id", "fecha", "cierre"]).dropna()
    s["fecha"] = pd.to_datetime(s["fecha"])
    out = {}
    for i, g in s.groupby("id", sort=False):
        c = g.sort_values("fecha").set_index("fecha")["cierre"]
        d = c[c.index > c.index[-1] - pd.Timedelta(days=400)]
        p0 = float(c.iloc[-1])
        hace = c[c.index <= c.index[-1] - pd.Timedelta(days=365)]
        out[i] = {"d50": p0 / d.iloc[-50:].mean() - 1 if len(d) >= 50 else None,
                  "d200": p0 / d.iloc[-200:].mean() - 1 if len(d) >= 120 else None,
                  "r12_1": float(d.iloc[-21] / hace.iloc[-1] - 1) if len(hace) and len(d) > 21 else None}
    return out


def main():
    act = pd.read_csv(CLEAN / "activos.csv")
    for h in ("corto", "medio", "largo"):                # el CSV guarda Favorable/Neutral/Desfavorable
        act[f"rec_{h}"] = act[f"rec_{h}"].replace(ETQ_INV)
    no = pd.read_csv(CLEAN / "noticias.csv", low_memory=False)
    ev = pd.read_csv(CLEAN / "eventos.csv")
    res = pd.read_csv(CLEAN / "resultados.csv")
    print("· orden por capitalización en dólares…")
    act = reordenar(act)
    import descargar_datos as dd
    print("· temas de las noticias…")
    no["tema"] = no["titular"].map(dd.tema)
    no["impacto_pts"] = ((4 - no["nivel_fuente"]) + no["tema"].map(dd.TEMA_PESO).fillna(0)
                         + no["exclusiva"] * np.where(no["nivel_fuente"] <= 2, 2, 1) + no["fuentes_coinciden"].clip(upper=4) - 1)
    no["impacto"] = np.select([no["impacto_pts"] >= 6, no["impacto_pts"] >= 4], ["Alto", "Medio"], "Bajo")
    no.to_csv(CLEAN / "noticias.csv", index=False, encoding="utf-8")
    print("· fundamentales frente al sector…")
    fu = fundamentales(act)
    for h in ("corto", "medio", "largo"):              # la puntuación de la descarga se guarda aparte (se puede relanzar)
        if f"score_{h}_base" not in act:
            act[f"score_{h}_base"] = act[f"score_{h}"]
    if "sig_ajustado" not in act:                      # datos descargados antes del suelo de volatilidad del 15 %
        tec0 = tecnicos()
        for c in ("score_corto_base", "score_medio_base", "score_corto"):
            act[c] = act[c].astype(float)
        for k, a in act.iterrows():
            so = a["sigma"]
            if a["clase"] != "Acciones" or not (pd.notna(so) and 0 < so < 0.15) or (isinstance(a.get("comp_medio"), str) and a["comp_medio"]):
                continue
            t, sf = tec0.get(a["id"], {}), 0.15
            z = lambda x, k_, s: clipf(x / (s * k_), -2, 2) if x is not None and pd.notna(x) else 0
            dc = (10 * (z(t.get("d50"), np.sqrt(50 / 252), sf) - z(t.get("d50"), np.sqrt(50 / 252), so))
                  + 8 * (z(a["var_1m"], np.sqrt(21 / 252), sf) - z(a["var_1m"], np.sqrt(21 / 252), so)))
            dm = (10 * (z(t.get("d200"), np.sqrt(200 / 252), sf) - z(t.get("d200"), np.sqrt(200 / 252), so))
                  + 8 * (z(t.get("r12_1"), 1, sf) - z(t.get("r12_1"), 1, so))
                  + 10 * (z(a.get("potencial"), 1, sf) - z(a.get("potencial"), 1, so)))
            act.loc[k, "score_corto_base"] = float(np.clip(a["score_corto_base"] + dc, 0, 100))
            act.loc[k, "score_medio_base"] = float(np.clip(a["score_medio_base"] + dm, 0, 100))
            act.loc[k, "score_corto"] = round(act.loc[k, "score_corto_base"])
            act.loc[k, "rec_corto"] = dd.etiqueta(act.loc[k, "score_corto"])
        act["sig_ajustado"] = 1
    act, tocados = ajustes_revision(act, dd)
    nota = act["id"].map(fu["nota_fund"])
    act["nota_fund"] = nota.round()
    fpts = {"medio": (nota - 50) * 0.16, "largo": (nota - 50) * 0.4}
    for h, pts in fpts.items():
        act[f"score_{h}"] = (act[f"score_{h}_base"] + pts.fillna(0)).clip(0, 100).round()
    # objetivo de analistas muy alejado del precio: aviso y el factor cuenta la mitad a 12 meses
    act["datos_dudosos"] = (act["clase"].isin(["Acciones", "ETFs"]) & (act["vol_anual"] > 2)).astype(int)
    avisos = act.apply(aviso_objetivo, axis=1)
    sz = np.where(act["clase"].eq("Acciones"), act["sigma"].clip(lower=0.15), act["sigma"])
    medio_obj = 10 * np.clip((act["potencial"] - 0.10) / sz, -2, 2)
    act["score_medio"] = np.where(avisos != "", (act["score_medio"] - 0.5 * medio_obj).clip(0, 100).round(),
                                  act["score_medio"])
    print(f"  {int((avisos != '').sum())} avisos de precio objetivo alejado")
    # empresas en pérdidas: el precio objetivo de los analistas no basta para dar «Favorable» a 12 meses
    perd = (act["id"].map(fu["margen_oper"]) < 0) | (act["id"].map(fu["per_fwd"]) >= 9999)
    act.loc[perd, "score_medio"] = act.loc[perd, "score_medio"].clip(upper=61)
    # cotización anclada (OPA o fusión): el recorrido está limitado al precio ofrecido
    opa = act["clase"].eq("Acciones") & (act["vol_anual"] < 0.12)
    for h in ("medio", "largo"):
        act.loc[opa, f"score_{h}"] = act.loc[opa, f"score_{h}"].clip(39, 61)
    act["datos_dudosos"] = (act["clase"].isin(["Acciones", "ETFs"]) & (act["vol_anual"] > 2)).astype(int)
    for h in ("corto", "medio", "largo"):
        act.loc[act["datos_dudosos"] == 1, f"score_{h}"] = 50
        act[f"rec_{h}"] = act[f"score_{h}"].map(dd.etiqueta)
    print("· catalizadores…")
    cat = dd.catalizadores(no, ev, act, res)
    cat.to_csv(CLEAN / "catalizadores.csv", index=False, encoding="utf-8")
    print("· explicaciones en lenguaje natural…")
    tec = tecnicos()
    no["_f"] = pd.to_datetime(no["fecha"], errors="coerce")
    rec = no[(no["evento"] == 0) & (no["_f"] >= HOY - pd.Timedelta(days=30)) & (no["nivel_fuente"] <= 2)]
    rec_por = dict(tuple(rec.groupby("id_activo")))
    cat_por = dict(tuple(cat.groupby("id_activo"))) if len(cat) else {}
    res_ok = res[res["bpa_real"].notna() & res["sorpresa_pct"].notna()].sort_values("fecha")
    res_por = {i: g.tail(4) for i, g in res_ok.groupby("id_activo")}
    try:
        an = pd.read_csv(CLEAN / "analistas.csv")
    except pd.errors.EmptyDataError:
        an = pd.DataFrame(columns=["id_activo", "fecha", "firma", "accion", "recomendacion", "anterior",
                                   "precio_objetivo", "objetivo_anterior"])
    an = an[pd.to_datetime(an["fecha"], errors="coerce") >= pd.Timestamp(act["fecha_dato"].max()) - pd.Timedelta(days=90)]
    an_por = dict(tuple(an.groupby("id_activo")))
    fu_d = fu.to_dict("index")
    htmls = []
    vacio_n, vacio_c = rec.iloc[:0], (cat.iloc[:0] if len(cat) else pd.DataFrame(columns=["tema", "puntuacion"]))
    textos = []
    act = act.reset_index(drop=True)
    avisos = avisos.reset_index(drop=True)
    fpts = {h: v.reset_index(drop=True) for h, v in fpts.items()}
    for _k, a in act.iterrows():
        claves, tk = claves_activo(a["nombre"], a.get("nombre_largo"), a["ticker"])
        n = rec_por.get(a["id"], vacio_n)
        if a["clase"] in ("Acciones", "Cripto") and len(n):
            n = n[n["titular"].map(lambda t: menciona(t, claves, tk)).astype(bool)]
        n = n.sort_values(["impacto_pts", "fecha"], ascending=False)
        _ = n.sort_values(["impacto_pts", "fecha"], ascending=False) \
            if a["clase"] in ("Acciones", "Cripto") else n.sort_values(["impacto_pts", "fecha"], ascending=False)
        f = fu_d.get(a["id"])
        r4 = res_por.get(a["id"], res_ok.iloc[:0])
        textos.append(explicar(a, tec.get(a["id"], {}), n, cat_por.get(a["id"], vacio_c), r4, f))
        nf = f.get("nota_fund") if f else np.nan
        fm = (fpts["medio"].iloc[_k], f"negocio frente a su sector: {nf:.0f}/100") if pd.notna(nf) else None
        fl = (fpts["largo"].iloc[_k], f"negocio frente a su sector: {nf:.0f}/100") if pd.notna(nf) else None
        desg = desglose(a, tec.get(a["id"], {}), an_por.get(a["id"], an.iloc[:0]), r4, fm, fl)
        for h in ("corto", "medio", "largo"):             # reglas de prudencia que recortan la puntuación
            dif = a[f"score_{h}"] - (50 + sum(c for c, _ in desg[h]))
            if abs(dif) >= 1:
                motivo = ("datos de precio no fiables: sin señal" if a.get("datos_dudosos") == 1 else
                          "objetivo de analistas muy alejado del precio: cuenta la mitad" if h == "medio" and avisos.iloc[_k] and dif < 0 else
                          "cotización casi inmóvil (posible OPA o fusión): señal acotada" if a["vol_anual"] < 0.12 else
                          "empresa en pérdidas: el objetivo de los analistas no basta" if h == "medio" and dif < 0 else
                          "redondeo y límites de 0 a 100")
                desg[h].append((dif, motivo))
        htmls.append(html_empresa(a, f, desg))
    act["exp_corto"], act["exp_medio"], act["exp_largo"], act["exp_resumen"] = zip(*textos)
    act["exp_medio"] = np.where(avisos != "", avisos + " " + act["exp_medio"], act["exp_medio"])
    caja = ("<div style='background:#FFF7ED;border-left:3px solid #EA580C;padding:6px 8px;margin:0 0 8px;"
            "font-family:Segoe UI;font-size:12px;color:#7C2D12'>")
    act["html_empresa"] = [caja + av + "</div>" + h if av else h for av, h in zip(avisos, htmls)]
    for h in ("corto", "medio", "largo"):
        act[f"rec_{h}"] = act[f"rec_{h}"].replace(ETQ)
    for h in ("corto", "medio", "largo"):              # columnas fijas aunque la descarga aún no las guarde
        if f"comp_{h}" not in act:
            act[f"comp_{h}"] = ""
    for h in ("medio", "largo"):                       # el motivo corto de las listas también cita el negocio
        base_mot = act[f"motivo_{h}"].astype(str).str.replace(r"^negocio \d+/100 frente a su sector · ", "", regex=True)
        con = act["nota_fund"].notna() & (fpts[h].abs() >= 3)
        act[f"motivo_{h}"] = np.where(con, "negocio " + act["nota_fund"].fillna(0).astype(int).astype(str)
                                      + "/100 frente a su sector · " + base_mot, base_mot)
    # volatilidad usada en la previsión (el modelo la espera siempre, aunque ningún valor se haya acotado)
    lim = np.minimum(np.maximum(act["sigma"], np.where(act["clase"].eq("Acciones"), 0.15, 0)), dd.SIGMA_MAX)
    act["sigma_prev"] = act["sigma_prev"].fillna(lim) if "sigma_prev" in act else lim
    act.to_csv(CLEAN / "activos.csv", index=False, encoding="utf-8")
    print("· bandas del gráfico de los activos con volatilidad acotada…")
    bandas_serie(act, tocados)
    print(f"ok: {len(act)} activos explicados, {len(cat)} catalizadores")


if __name__ == "__main__":
    main()
