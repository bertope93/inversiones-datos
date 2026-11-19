"""Descarga cotizaciones, datos de analistas y noticias públicas, y calcula forecast, riesgo y recomendación.

Fuentes: Yahoo Finance (precios ajustados, consenso y objetivos de analistas, próximos resultados) y
Google News (titulares de prensa económica, recientes y de los días de grandes movimientos).
Los activos se definen en config/activos.csv (añadir una fila = añadir un activo).

Uso:  python scripts/descargar_datos.py      (luego, en Power BI: Actualizar)
"""
import csv
import json
import math
import os
import re
import sys
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import feedparser
import numpy as np
import pandas as pd
import yfinance as yf

BASE = Path(__file__).resolve().parent.parent
RAW, CLEAN = BASE / "data" / "raw", BASE / "data" / "clean"
HOY = pd.Timestamp.today().normalize()

# Rentabilidad anual de referencia a largo plazo por clase (prior de la previsión; ver docs/metodologia.md)
PRIOR = {"Acciones": 0.08, "ETFs": 0.07, "Índices": 0.07, "Cripto": 0.05, "Divisas": 0.0, "Renta fija": 0.04}
HORIZONTES = {"corto": 21, "medio": 252, "largo": 1260}          # sesiones: 1 mes, 12 meses, 5 años
CONSENSO = {"strong_buy": ("Compra fuerte", 8), "buy": ("Comprar", 4), "hold": ("Mantener", 0),
            "underperform": ("Infraponderar", -4), "sell": ("Vender", -8)}

# ---------- Léxico de temas y tono de titulares (inglés y español) ----------
# Cada tema suma puntos por las expresiones que aparecen (las inequívocas pesan más); gana el de más puntos.
# Antes ganaba la primera coincidencia y fallaba con palabras sueltas: «Outlook» (el correo) contaba como resultados,
# «disappoint» como nombramiento y «downgrades … on CEO worries» como cambio en la dirección.
TEMAS = [
    ("Analistas", [(r"\b(upgrade[sd]?|downgrade[sd]?)\b|price target|target price|initiat\w* (coverage|at)|"
                    r"\b(outperform|underperform|overweight|underweight|equal[- ]weight)\b|\b(buy|sell|hold) rating|"
                    r"(raises?|cuts?|trims?|lifts?|lowers?|boosts?|hikes?) (its |price |pt )?targets?\b|\b(raises|cuts|trims|lifts|lowers|boosts|hikes) (its |price |pt )?targets?|rebaja la recomendaci|eleva la recomendaci|precio objetivo|recomendaci[oó]n de", 4),
                   (r"\banalysts?\b|\banalistas?\b|\brating\b|wall street's top", 1)]),
    ("Resultados", [(r"\bearnings\b|\bq[1-4]\b|quarterly|first[- ]quarter|second[- ]quarter|third[- ]quarter|fourth[- ]quarter|"
                     r"(beats?|miss(es)?|tops?) (estimates|expectations|forecasts)|\beps\b|half[- ]year results|"
                     r"(raises|cuts|lowers|boosts|lifts|reaffirms|reiterates|maintains|trims|hikes|narrows|withdraws) "
                     r"(its |full[- ]year |annual |fy\s?\d* |\d{4} )*(outlook|guidance|forecast)|"
                     r"\bguidance\b|net (loss|income)|(posts|logs|reports|swings to) .{0,25}(loss|profit)|resultados|trimestr|beneficio neto|cifra de negocio", 4),
                    (r"\brevenue\b|\bprofit\b|\bsales (rose|fell|grew|growth|jump|beat|miss|decline|surge)|"
                     r"\bingresos\b|\bfacturaci|\bventas\b", 2)]),
    ("Acuerdos", [(r"\bsettle(ment|s|d)?\b|\bagrees? to (pay|buy|sell|settle)", 7),
                  (r"\bacquir(e|es|ed|ing)\b|\bacquisition\b|\bmerger\b|\btakeover\b|\bbuyout\b|"
                   r"\btender offer\b|joint venture|(licen[cs]ing|supply|collaboration|distribution) (deal|agreement|pact)|"
                   r"\bto buy\b|\bbuys\b|\bsells? (its |a )?(stake|unit|business|division)|"
                   r"\bacuerdo\b|adquisici|adquiere|fusi[oó]n|\bopa\b|alianza", 4),
                  (r"\bdeal\b|\bagreement\b|partnership|partners with|\bstake\b|\bcontract\b|contrato", 2)]),
    ("Litigios", [(r"\blawsuit|\bsues?\b|\bsued\b|\bcourt\b|litigation|\bjudge\b|\bruling\b|injunction|\bjury\b|"
                   r"class action|patent (case|suit|infringement|dispute|win|loss)|\bindict|\bguilty\b|\bverdict\b|"
                   r"\bdemanda\b|\bjuicio\b|tribunal|sentencia|litigio|querella", 4),
                  (r"(?<!clinical )(?<!phase \d )\btrial\b(?! (data|results|readout|met|showed))|\bappeal\b|\bpatent\b|"
                   r"espionage|espionaje|smuggl|fraud|fraude|\barrest|detenid|\bauditor", 2)]),
    ("Regulación", [(r"\bfda\b|\bema\b|\bfcc\b|\bftc\b|\bdoj\b|\bcnmv\b|antitrust|antimonopol|"
                     r"approv(al|es|ed)\b|aprobaci|aprueba|export (control|ban|curb|licen|restriction)|"
                     r"\btariffs?\b|aranceles?|sanction|sanci[oó]n|\bprobe[sd]?\b|investigat|investigaci|regulator|regulador", 4),
                    (r"regulat|\bban(s|ned)?\b|\bsec\b(?! filing)", 2)]),
    ("Directiva", [(r"\b(resign(s|ed|ation)?|steps? down|stepping down|ousted|fired|retire[sd]?|departure|depart(s|ing))\b|"
                    r"\bappoint(s|ed|ment)?\b|\bnames? (new )?(ceo|chief|president|chair)|new (ceo|chief executive|cfo|chair)|"
                    r"\bsuccess(ion|or)\b|leadership (change|overhaul|shake|transition|shuffle)|management shake|"
                    r"\bdimit|dimisi|\bnombra\b|nuevo (consejero delegado|presidente)|relevo", 4),
                   (r"\bceo\b|chief executive|\bcfo\b|\bchair(man|woman)?\b|board of directors|consejero delegado", 1)]),
    ("I+D y producto", [(r"\bphase (1|2|3|i|ii|iii)\b|clinical|trial (data|results|readout)|topline|pivotal|"
                         r"breakthrough (therapy|designation)|\bunveil|\blaunch(es|ed)?\b|rolls? out|new (product|model|chip|drug)|"
                         r"ensayo|lanza|presenta (su|un|una) nuev", 3)]),
    ("Incidencias", [(r"\boutage\b|\bdisruption\b|\bhack(ed|ers)?\b|data breach|cyber ?attack|\brecalls?\b|\bstrike\b|"
                      r"\bexplosion\b|\bfire at\b|halts? production|grounded|ca[ií]da del servicio|ciberataque|huelga|apag[oó]n", 4)]),
    ("Capital", [(r"\bdividend|\bbuyback|share repurchase|stock split|share (offering|sale)|\bipo\b|insider (buy|sell|trad)|"
                  r"\bform 4\b|dividendo|recompra|ampliaci[oó]n de capital|salida a bolsa", 3),
                 (r"own shares|acciones propias|shares (newly )?(bought|sold|acquired|purchased) by|\bstake (in|of)\b|(increases|decreases|raises|cuts|trims|boosts|lifts|lowers) (its |their )?(holdings|stake|position)|position in\b|"
                  r"hedge fund|shareholders'? meeting|annual general meeting|\bagm\b|junta (general )?de accionistas|"
                  r"\bdelist|\bspac\b", 2)]),
    ("Macro", [(r"\bfed\b|federal reserve|inflation|interest rates?|rate[- ](cut|hike)s?|\becb\b|recession|jobs report|payrolls|"
                r"treasury yields?|\bgdp\b|\bcpi\b|tipos de inter|inflaci|\bbce\b|recesi|\bpib\b|\bipc\b", 3),
               (r"\byields?\b|\brates\b|\btreasur|\bdollar\b|oil demand|\bopec\b|d[oó]lar", 1)]),
    # temas amplios (al final: en empate gana el más específico)
    ("Estrategia", [(r"strateg|estrategia|cost[- ]cut|job cuts?|layoffs?|(axe|cut|slash)\w* [\d,]+ jobs|restructur|reestructur|"
                     r"reorgani[sz]|spin[- ]?off|investor day|capital markets day|growth (plan|pillar|strategy|story)|"
                     r"new (plant|factory|facility|data cent(er|re))|\bexpan(d|ds|ding|sion)\b|expansi[oó]n|despidos|"
                     r"\bpivot|nueva planta|plan estrat[eé]gico|invierte|\binvest(s|ing)? .{0,20}(billion|million|bn\b)", 3),
                    (r"\bplans? to\b|\bplanea\b|\bconsiders?\b|\bpush (into|for)\b|\bmove into\b", 1)]),
    ("Cotización", [(r"\b(stock|shares?|acciones|títulos)\b.{0,30}\b(falls?|fell|rises?|rose|jump(s|ed)?|soar(s|ed)?|surg(e|es|ed)|"
                     r"slid(e|es)?|sinks?|sank|drop(s|ped)?|tumbl(e|es|ed)|plung(e|es|ed)|climb(s|ed)?|gain(s|ed|ing)?|"
                     r"rall(y|ies|ied|ying)|rebound(s|ed)?|crash(es|ed)?|d(o|i)ve(s|d)?|going (up|down)|moves? -?\d|(fall|ris|soar|surg|plung|sink|slid|climb|jump)ing|slump(s|ed)?|slip(s|ped)?|slide|moved (up|down)|trading (lower|higher)|"
                     r"in focus|hits?|extends? (losses|gains)|sube|baja|cae|se dispara|se desploma|repunta)\b|"
                     r"52-week (high|low)|all[- ]time high|record high|\bstraight (green|red) days|premarket|pre-market|after[- ]hours|"
                     r"\bsell-?off\b|\brout\b|\bup \d+(\.\d+)?%|\bdown \d+(\.\d+)?%|\bytd\b|price, news|stock price|share price|"
                     r"m[aá]ximos? (hist[oó]ricos?|anuales?)|m[ií]nimos? (hist[oó]ricos?|anuales?)|cotizaci[oó]n|"
                     r"\b(sube|cae|baja|se dispara|se desploma|repunta|retrocede|avanza) (un|el|en bolsa|\d)", 3)]),
    ("Opinión", [(r"should you buy|buy the dip|time to buy|worth buying|(under|over)valued|fairly valued|stocks? to (buy|watch|own|hold)|"
                  r"\bbest .{0,25}(stocks?|etfs?|reits?|buys?|picks?)\b|top (stocks?|picks?)|how to invest|could (double|triple|soar)|"
                  r"millionaire|\bbuy (now|right now|before|today)\b|\b(vs\.?|versus)\s|stands? out|what's going on|"
                  r"\bwhy (is |are )?.{0,40}\b(stock|shares)\b|\bis .{0,30} a buy\b|\bbull(ish)? case|\bbear(ish)? case|"
                  r"\bright now\b|can you justify|bargain|still cheap|fair value|what could (lift|sink)|\bforget\b|\bforecasts?\b|could be (a|the)|merece la pena|\bcomprar\b|infravalorad|sobrevalorad", 3)]),
]
AMPLIOS = {"Estrategia", "Cotización", "Opinión"}
TEMAS_RE = [(n, [(re.compile(p), w) for p, w in pats]) for n, pats in TEMAS]
POSITIVO = (r"surge|soar|jump|rall|beat|record|gain|rise|rises|rising|upgrade|approv|\bwins?\b|strong|growth|"
            r"bullish|outperform|boost|climb|higher|tops|settle|sube|dispara|r[eé]cord|gana|mejora|rebota|crece")
NEGATIVO = (r"plunge|\bfall|falls|drop|slump|tumble|miss|loss|lawsuit|\bsues?\b|downgrade|\bcut|weak|probe|"
            r"investigat|fraud|recall|bearish|decline|crash|sink|slide|lower|warn|fear|resign|"
            r"\bcae|desploma|pierde|demanda|hunde|recorta|baja|retrocede")


# Nivel de la fuente: 1 = agencia o prensa financiera de referencia, 2 = prensa económica/sectorial
# especializada, 3 = blogs y webs de opinión de inversores (útiles, pero no verificadas).
FUENTES = {
    1: ["Reuters", "Bloomberg", "The Wall Street Journal", "WSJ", "Financial Times", "CNBC", "Barron's",
        "MarketWatch", "Associated Press", "AP News", "The Economist", "Nikkei Asia", "Expansión",
        "Cinco Días", "El Economista", "eleconomista.es", "Europa Press", "EFE", "Dow Jones", "Morningstar",
        "Fortune", "S&P Global", "Investor's Business Daily", "The New York Times", "Handelsblatt", "Les Echos"],
    2: ["Yahoo Finance", "Business Insider", "Forbes", "Fierce Pharma", "FiercePharma", "BioPharma Dive",
        "Endpoints News", "STAT", "Investing.com", "CoinDesk", "The Block", "Kiplinger", "Axios", "TechCrunch",
        "The Verge", "Quartz", "Fox Business", "Bolsamanía", "El Confidencial", "El País", "Investopedia",
        "Cointelegraph", "FXStreet", "Evaluate", "Pharmaceutical Technology", "Tom's Hardware", "CNN"],
}
PREMIUM = ("(site:reuters.com OR site:bloomberg.com OR site:wsj.com OR site:ft.com OR site:cnbc.com OR "
           "site:barrons.com OR site:marketwatch.com OR site:apnews.com)")
PREMIUM_ES = ("(site:expansion.com OR site:cincodias.elpais.com OR site:eleconomista.es OR site:europapress.es OR "
              "site:reuters.com OR site:bloomberg.com)")


def nivel_fuente(f):
    f = (f or "").lower()
    for nivel, lista in FUENTES.items():
        if any(x.lower() in f for x in lista):
            return nivel
    return 3


SEC_UA = {"User-Agent": "PBI-Inversiones/1.0 investigacion-personal (contacto: pbi-inversiones@example.org)"}
ITEMS_8K = {"1.01": "Acuerdo relevante", "1.02": "Fin de un acuerdo relevante", "1.03": "Quiebra",
            "2.01": "Compra o venta de activos", "2.02": "Resultados", "2.03": "Nueva deuda",
            "2.05": "Reestructuración", "2.06": "Deterioro de activos", "3.01": "Aviso de exclusión de cotización",
            "4.01": "Cambio de auditor", "5.02": "Entrada o salida de directivos o consejeros",
            "5.03": "Cambio de estatutos", "5.07": "Votación de accionistas", "7.01": "Comunicación al mercado",
            "8.01": "Otros hechos relevantes"}
FORMULARIOS = {"10-K": "Informe anual auditado", "10-Q": "Informe trimestral", "20-F": "Informe anual (emisor extranjero)",
               "6-K": "Comunicación de emisor extranjero", "4": "Compraventa de acciones de un directivo (insider)",
               "SC 13D": "Participación significativa (>5 %)", "SC 13G": "Participación significativa (>5 %)",
               "DEF 14A": "Convocatoria de junta", "144": "Aviso de venta de un insider"}


def sec_documentos(tickers_activos):
    import requests
    try:
        mapa = requests.get("https://www.sec.gov/files/company_tickers.json", headers=SEC_UA, timeout=30).json()
    except Exception:
        return []
    cik = {v["ticker"].upper(): int(v["cik_str"]) for v in mapa.values()}
    filas = []
    for orden, t in tickers_activos:
        if t.upper() not in cik:
            continue
        c = cik[t.upper()]
        try:
            r = requests.get(f"https://data.sec.gov/submissions/CIK{c:010d}.json", headers=SEC_UA,
                             timeout=30).json()["filings"]["recent"]
        except Exception:
            continue
        time.sleep(0.15)                                    # la SEC pide no pasar de 10 peticiones por segundo
        for k in range(len(r["form"])):
            fecha = r["filingDate"][k]
            if fecha < (HOY - pd.Timedelta(days=365)).date().isoformat():
                break
            f = r["form"][k]
            if f not in FORMULARIOS and f != "8-K":
                continue
            items = [x for x in (r["items"][k] or "").split(",") if x and x != "9.01"]
            desc = "; ".join(ITEMS_8K.get(x, x) for x in items) if f == "8-K" else FORMULARIOS[f]
            acc = r["accessionNumber"][k].replace("-", "")
            filas.append({"id_activo": orden, "fecha": fecha, "formulario": f,
                          "descripcion": desc or "Hecho relevante",
                          "relevante": int(f in ("8-K", "10-K", "10-Q", "20-F", "6-K", "SC 13D")),
                          "enlace": f"https://www.sec.gov/Archives/edgar/data/{c}/{acc}/{r['primaryDocument'][k]}"})
    return filas


def profesionales(par):
    """Cambios de recomendación de bancos de inversión y BPA estimado frente a real (Yahoo Finance)."""
    orden, t = par
    tk, ana, res = yf.Ticker(t), [], []
    try:
        u = tk.upgrades_downgrades
        u = u[u.index >= HOY - pd.Timedelta(days=365)]
        for f, x in u.iterrows():
            ana.append({"id_activo": orden, "fecha": f.date().isoformat(), "firma": x.get("Firm"),
                        "accion": {"up": "Mejora", "down": "Rebaja", "main": "Mantiene", "init": "Inicia cobertura",
                                   "reit": "Reitera"}.get(x.get("Action"), x.get("Action")),
                        "recomendacion": x.get("ToGrade"), "anterior": x.get("FromGrade"),
                        "precio_objetivo": x.get("currentPriceTarget"), "objetivo_anterior": x.get("priorPriceTarget")})
    except Exception:
        pass
    try:
        e = tk.get_earnings_dates(limit=12)
        for f, x in e.iterrows():
            res.append({"id_activo": orden, "fecha": f.date().isoformat(), "bpa_estimado": x.get("EPS Estimate"),
                        "bpa_real": x.get("Reported EPS"), "sorpresa_pct": x.get("Surprise(%)")})
    except Exception:
        pass
    return ana, res

VACIAS = set("the a an and or of to in on for with from at by as is are was be its it this that after over into "
             "stock stocks shares share says said new will could may el la los las de del y en por para con que "
             "un una se su sus al lo".split())
EXCLUSIVA = (r"exclusive|exclusiva|sources said|sources say|people familiar|according to (?:people|sources)|"
             r"\breport(?:s|edly)?\b|según fuentes|fuentes cercanas|filtra")
TEMA_PESO = {"Litigios": 2, "Directiva": 2, "Regulación": 2, "Acuerdos": 2, "Resultados": 2, "Incidencias": 2,
             "I+D y producto": 1, "Analistas": 1, "Capital": 1, "Estrategia": 1}


def palabras(t):
    return {w for w in re.findall(r"[a-záéíóúñü0-9]+", t.lower()) if len(w) > 3 and w not in VACIAS}


def verificar(no):
    """Corroboración: cuántas fuentes distintas cuentan lo mismo (±2 días, ≥4 palabras clave en común).
    Confirmada = medio de referencia o ≥2 fuentes independientes. Impacto = fuente + tema + exclusiva + eco."""
    no = no.copy()
    no["_f"] = pd.to_datetime(no["fecha"])
    no["_p"] = no["titular"].map(palabras)
    coinciden = []
    for _, g in no.groupby("id_activo"):
        filas = list(g[["_f", "_p", "fuente"]].itertuples(index=True))
        for i, f, pw, fu in filas:
            fuentes = {fu}
            for _, f2, pw2, fu2 in filas:
                if fu2 not in fuentes and abs((f2 - f).days) <= 2 and len(pw & pw2) >= 4:
                    fuentes.add(fu2)
            coinciden.append((i, len(fuentes)))
    no["fuentes_coinciden"] = pd.Series(dict(coinciden))
    no["exclusiva"] = no["titular"].str.lower().str.contains(EXCLUSIVA, regex=True).astype(int)
    no["verificacion"] = np.where((no["nivel_fuente"] == 1) | (no["fuentes_coinciden"] >= 2), "Confirmada",
                                  "Por confirmar")
    pts = ((4 - no["nivel_fuente"]) + no["tema"].map(TEMA_PESO).fillna(0) + no["exclusiva"] * np.where(no["nivel_fuente"] <= 2, 2, 1)
           + no["fuentes_coinciden"].clip(upper=4) - 1)
    no["impacto_pts"] = pts
    no["impacto"] = np.select([pts >= 6, pts >= 4], ["Alto", "Medio"], "Bajo")
    return no.drop(columns=["_f", "_p"])


def peso_noticia(n):
    """Peso en el tono: prensa de referencia 1; especializada 0,7; blogs 0,3 (0,6 si otras fuentes lo confirman)."""
    nv = nivel_fuente(n["fuente"])
    return {1: 1.0, 2: 0.7}.get(nv, 0.6 if n.get("eco", 1) >= 2 else 0.3)

# ---------- Catalizadores: asuntos pendientes que pueden mover el precio ----------
PENDIENTE = (r"talks|negotiat|could|may |might|possible|potential|pending|await|expected|considering|weigh|"
             r"seeks?|plans? to|in discussions|nears?|close to|bid|offer|proposal|appeal|ruling due|decision|"
             r"deadline|vote|hearing|pdufa|review|probe|investigation|rumou?r|report|"
             r"conversaciones|negociaci|podr[ií]a|posible|pendiente|a la espera|estudia|sopesa|ofert|recurso|decisi[oó]n")
ESCENARIOS = {
    "Acuerdos": ("Negociación / operación corporativa", "si se cierra el acuerdo u operación", "si se rompen las conversaciones"),
    "Litigios": ("Litigio pendiente", "si el fallo o el recurso le es favorable", "si pierde en los tribunales"),
    "Regulación": ("Decisión regulatoria", "si obtiene la aprobación o se levanta la restricción", "si se deniega o se endurece"),
    "Directiva": ("Cambio en la dirección", "si la sucesión tranquiliza al mercado", "si se prolonga la incertidumbre"),
    "Resultados": ("Resultados", "si supera lo esperado", "si decepciona"),
    "I+D y producto": ("Ensayo o lanzamiento", "si los datos o el lanzamiento convencen", "si decepcionan"),
}


def catalizadores(no, ev, act, resultados):
    no = no.copy()
    no["_f"] = pd.to_datetime(no["fecha"])
    reciente = no[(no["evento"] == 0) & (no["_f"] >= HOY - pd.Timedelta(days=30)) & no["tema"].isin(ESCENARIOS)
                  & no["titular"].str.lower().str.contains(PENDIENTE, regex=True) & (no["nivel_fuente"] <= 2)]
    # tema dominante de cada gran movimiento pasado (según sus titulares) para buscar analogías
    hist = no[no["evento"] == 1].groupby(["id_activo", "fecha_evento"])["tema"].agg(
        lambda x: x[x != "General"].mode().iloc[0] if (x != "General").any() else "General").reset_index()
    if len(ev):
        ev = ev.merge(hist, left_on=["id_activo", "fecha"], right_on=["id_activo", "fecha_evento"], how="left")
    filas = []
    for _, a in act[act["clase"].isin(["Acciones", "Cripto"])].iterrows():
        i = a["id"]
        from enriquecer import claves_activo, menciona          # el titular debe nombrar a la empresa
        claves, tk = claves_activo(a["nombre"], a.get("nombre_largo"), a["ticker"])
        cand = reciente[(reciente["id_activo"] == i) & reciente["titular"].apply(lambda t: menciona(t, claves, tk))]
        cand = cand.sort_values(["impacto_pts", "fecha"], ascending=False)
        elegidos = []
        for tipo in cand["tema"].unique():
            elegidos.append(cand[cand["tema"] == tipo].iloc[0])
        # resultados en los próximos 30 días: catalizador de calendario
        if pd.notna(a.get("dias_a_resultados")) and 0 <= a["dias_a_resultados"] <= 30 and "Resultados" not in [e["tema"] for e in elegidos]:
            elegidos.append(pd.Series({"tema": "Resultados", "titular": f"Publica resultados el {a['prox_resultados']}",
                                       "fuente": "Calendario de resultados", "enlace": a["web"], "fecha": HOY.date().isoformat(),
                                       "nivel_fuente": 1, "verificacion": "Confirmada", "impacto_pts": 5}))
        ev_a = ev[ev["id_activo"] == i] if len(ev) else ev
        sube = ev_a[ev_a["retorno"] > 0]["retorno"] if len(ev_a) else pd.Series(dtype=float)
        baja = ev_a[ev_a["retorno"] < 0]["retorno"] if len(ev_a) else pd.Series(dtype=float)
        mov_tipico = 2.5 * a["sigma"] / math.sqrt(252)
        for c in elegidos:
            tipo = c["tema"]
            nombre, txt_up, txt_down = ESCENARIOS[tipo]
            mismo = ev_a[ev_a["tema"] == tipo] if len(ev_a) and "tema" in ev_a else pd.DataFrame()
            if tipo == "Resultados" and pd.notna(a.get("mov_medio_resultados")):
                up = down = float(a["mov_medio_resultados"])
                base = f"movimiento medio de {a['nombre']} en sus últimas publicaciones de resultados"
            elif len(mismo):
                ult = mismo.sort_values("fecha").iloc[-1]
                up = float(mismo[mismo["retorno"] > 0]["retorno"].median()) if (mismo["retorno"] > 0).any() else (float(sube.median()) if len(sube) else mov_tipico)
                down = float(-mismo[mismo["retorno"] < 0]["retorno"].median()) if (mismo["retorno"] < 0).any() else (float(-baja.median()) if len(baja) else mov_tipico)
                base = (f"la última noticia de este tipo ({ult['fecha']}) movió {a['nombre']} un "
                        f"{'+' if ult['retorno'] > 0 else ''}{ult['retorno'] * 100:.0f} % en un día".replace(".", ","))
            elif len(sube) or len(baja):
                up = float(sube.median()) if len(sube) else mov_tipico
                down = float(-baja.median()) if len(baja) else mov_tipico
                base = f"mediana de sus grandes movimientos de los dos últimos años"
            else:
                up = down = mov_tipico
                base = "2,5 veces su movimiento diario típico (sin antecedentes propios)"
            up, down = min(up, 0.3), min(down, 0.3)        # un solo precedente extremo no es una previsión
            filas.append({"id_activo": i, "fecha": str(c["fecha"]), "tipo": nombre, "tema": tipo, "titular": c["titular"],
                          "fuente": c["fuente"], "enlace": c["enlace"], "nivel_fuente": int(c["nivel_fuente"]),
                          "verificacion": c["verificacion"], "si_sale_bien": txt_up, "si_sale_mal": txt_down,
                          "mov_favorable": up, "mov_desfavorable": -down, "base": base,
                          "asimetria": up - down, "puntuacion": up * (1.0 if c["verificacion"] == "Confirmada" else 0.6)
                          * (1.2 if c["nivel_fuente"] == 1 else 1.0)})
    return pd.DataFrame(filas)



def tema(t):
    t = str(t).lower()
    mejor, pts = "General", 0
    for nombre, pats in TEMAS_RE:                  # empate: gana el que va antes en la lista (más específico)
        if nombre in AMPLIOS and pts >= 2:
            break                                  # los temas amplios solo cuentan si no hay uno concreto
        p = sum(w for rx, w in pats if rx.search(t))
        if p > pts:
            mejor, pts = nombre, p
    return mejor if pts >= 2 else "General"


def tono(t):
    t = t.lower()
    p, n = len(re.findall(POSITIVO, t)), len(re.findall(NEGATIVO, t))
    return 0 if p == n else (1 if p > n else -1)


# ---------- Noticias (Google News RSS) ----------
def yahoo_news(ticker):
    """Feed de noticias de Yahoo Finance del valor (agrega Reuters, Bloomberg, Barron's, etc.)."""
    out = []
    try:
        for x in yf.Ticker(ticker).news or []:
            c = x.get("content", x)
            fecha = pd.Timestamp(c.get("pubDate") or c.get("displayTime")).date()
            out.append({"fecha": fecha, "titular": c.get("title", "").strip(),
                        "fuente": (c.get("provider") or {}).get("displayName", "Yahoo Finance"),
                        "enlace": ((c.get("canonicalUrl") or {}).get("url") or (c.get("clickThroughUrl") or {}).get("url") or "")})
    except Exception:
        pass
    return [o for o in out if o["titular"]]


def google_news(consulta, idioma, desde=None, hasta=None, maximo=60):
    q = consulta + (f" after:{desde:%Y-%m-%d} before:{hasta:%Y-%m-%d}" if desde is not None else "")
    reg = "hl=es&gl=ES&ceid=ES:es" if idioma == "es" else "hl=en-US&gl=US&ceid=US:en"
    url = f"https://news.google.com/rss/search?q={urllib.parse.quote(q)}&{reg}"
    for intento in range(3):
        try:
            feed = feedparser.parse(url, agent="Mozilla/5.0 (informe-inversiones)")
            break
        except Exception:
            time.sleep(2 * (intento + 1))
    else:
        return []
    out = []
    for e in feed.entries[:maximo]:
        fuente = getattr(e, "source", {}).get("title", "") if hasattr(e, "source") else ""
        titulo = e.title
        if fuente and titulo.endswith(" - " + fuente):
            titulo = titulo[: -len(fuente) - 3]
        try:
            fecha = parsedate_to_datetime(e.published).astimezone(timezone.utc).date()
        except Exception:
            continue
        out.append({"fecha": fecha, "titular": titulo.strip(), "fuente": fuente, "enlace": e.link})
    return out


# ---------- Estadística ----------
def norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


Z = {"p025": -1.95996, "p10": -1.28155, "p50": 0.0, "p90": 1.28155, "p975": 1.95996}


def cuantiles(s0, mu, sigma, sesiones):
    sigma = min(sigma, SIGMA_MAX)       # con más del 80 % de volatilidad la lognormal da rangos absurdos (+8.000 %)
    h = sesiones / 252
    m = math.log(1 + mu) * h          # la mediana crece a la rentabilidad esperada; la volatilidad abre las bandas
    return {k: s0 * math.exp(m + z * sigma * math.sqrt(h)) for k, z in Z.items()}, m, sigma * math.sqrt(h)


SIGMA_MAX = 0.8
TAU_DERIVA = 0.25 / math.log(2)        # semivida de 3 meses: el impulso reciente se desvanece hacia la deriva de largo plazo
TAU_VOL = (2 / 12) / math.log(2)       # semivida de 2 meses: la volatilidad actual vuelve a la de 5 años (como un GARCH)


def trayectoria(p, h, extra_var=0.0, piso=0.0):
    """Media y desviación del logaritmo del precio a h años (revisión de Cibran, 19/11/2026).
    - Deriva: la de corto plazo (con el momentum 12-1) se desvanece hacia la de largo con semivida de 3 meses.
    - Objetivo de analistas: con su sesgo optimista descontado (10 %), a la mitad y solo hasta 12 meses.
    - Volatilidad: la actual (EWMA) converge a la de 5 años con semivida de 2 meses.
    extra_var: salto de los días de resultados; piso: volatilidad mínima."""
    g_lp, g_cp = p["g_lp"], p["g_cp"]
    m = g_lp * h + (g_cp - g_lp) * TAU_DERIVA * (1 - math.exp(-h / TAU_DERIVA)) + p["obj"] * min(h, 1)
    s0, slp = max(min(p["sig0"], SIGMA_MAX), piso), max(min(p["sig_lp"], SIGMA_MAX), piso)
    v = slp ** 2 * h + (s0 ** 2 - slp ** 2) * TAU_VOL * (1 - math.exp(-h / TAU_VOL)) + extra_var
    return m, math.sqrt(max(v, 1e-12))


def cuantiles_tray(s0, p, h, extra_var=0.0, piso=0.0):
    m, sd = trayectoria(p, h, extra_var, piso)
    return {k: s0 * math.exp(m + z * sd) for k, z in Z.items()}, m, sd


def corregir_peniques(s):
    """Yahoo mezcla a veces libras y peniques (saltos de x100): se pasa todo a la unidad del último dato."""
    sal = np.log(s / s.shift()).abs() > math.log(50)
    if not sal.any():
        return s
    s = s.copy()
    for f in s.index[sal][::-1]:
        k = s.index.get_loc(f)
        fac = s.iloc[k] / s.iloc[k - 1]
        s.iloc[:k] = s.iloc[:k] * (100 if fac > 1 else 0.01)
    return s


def srri(vol):
    """Indicador sintético de riesgo y remuneración de la UE (1-7) según volatilidad anual."""
    for nivel, tope in enumerate([0.005, 0.02, 0.05, 0.10, 0.15, 0.25], start=1):
        if vol < tope:
            return nivel
    return 7


def clip(x, a, b):
    return max(a, min(b, x))


def etiqueta(score):
    return "Comprar" if score >= 62 else ("Evitar" if score <= 38 else "Mantener")


def pct(x, dec=0):
    return ("+" if x > 0 else "") + f"{x * 100:.{dec}f}".replace(".", ",") + " %"


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    RAW.mkdir(parents=True, exist_ok=True)
    CLEAN.mkdir(parents=True, exist_ok=True)
    activos = list(csv.DictReader(open(BASE / "config" / "activos.csv", encoding="utf-8")))
    tickers = [a["ticker"] for a in activos]
    # modo rápido (minutos): precios del último mes sobre el histórico guardado, fichas y noticias solo de los
    # valores principales; el resto (noticias antiguas, grandes movimientos) se reutiliza de la última descarga
    rapido = "rapido" in sys.argv and (RAW / "precios_yahoo.csv").exists() and (CLEAN / "noticias.csv").exists()
    previas = {}
    prev_an = prev_res = None
    if (CLEAN / "activos.csv").exists():        # si Yahoo falla para un valor, se conservan sus datos anteriores
        id_tk0 = dict(pd.read_csv(CLEAN / "activos.csv", usecols=["id", "ticker"]).values)
        def _prev(nombre):
            try:
                d = pd.read_csv(CLEAN / nombre)
                d["ticker"] = d["id_activo"].map(id_tk0)
                return d.dropna(subset=["ticker"])
            except Exception:
                return None
        prev_an, prev_res = _prev("analistas.csv"), _prev("resultados.csv")
    if rapido:
        print("MODO RÁPIDO")
        pa = pd.read_csv(CLEAN / "activos.csv", usecols=["id", "ticker"])
        id_tk = dict(zip(pa["id"], pa["ticker"]))
        pn = pd.read_csv(CLEAN / "noticias.csv", low_memory=False)
        pn["ticker"] = pn["id_activo"].map(id_tk)
        pn["fecha"] = pd.to_datetime(pn["fecha"], errors="coerce").dt.date
        pn = pn.dropna(subset=["ticker", "fecha"])
        for c in ("fuente", "enlace"):
            pn[c] = pn[c].fillna("").astype(str)
        base_cols = ["fecha", "titular", "fuente", "enlace"]
        for tk, g in pn[pn["evento"] == 0].groupby("ticker"):
            previas[tk] = g[base_cols].to_dict("records")
        ev_prev = pd.read_csv(CLEAN / "eventos.csv")
        ev_prev["ticker"] = ev_prev["id_activo"].map(id_tk)
        nev_prev = pn[pn["evento"] == 1]

    print("· precios (Yahoo Finance)…")
    partes = []
    for k in range(0, len(tickers), 250):     # por bloques: Yahoo bloquea las peticiones masivas (sobre todo en la nube)
        bloque = tickers[k:k + 250]
        try:
            px = yf.download(bloque, period="1mo" if rapido else "max", interval="1d", auto_adjust=True, progress=False,
                             group_by="ticker", threads=True)
            partes.append(pd.DataFrame({t: px[t]["Close"] for t in bloque if t in px.columns.get_level_values(0)}))
        except Exception:
            pass
        time.sleep(3)
    cierres = pd.concat(partes, axis=1) if partes else pd.DataFrame()
    cierres.index = pd.to_datetime(cierres.index).tz_localize(None).normalize()
    cierres = cierres.dropna(how="all", axis=1)
    viejo_f = RAW / "precios_yahoo.csv"
    if viejo_f.exists():                      # lo que no se pudo bajar conserva su histórico anterior
        viejo = pd.read_csv(viejo_f, index_col=0, parse_dates=True)
        cierres = cierres.combine_first(viejo) if rapido else cierres.combine_first(viejo[[c for c in viejo if c not in cierres]])

    def faltan():
        return [t for t in tickers if t not in cierres or cierres[t].dropna().size < 60
                or cierres[t].dropna().index[-1] < HOY - pd.Timedelta(days=10)]
    for ronda in range(2 if rapido else 4):     # Yahoo corta las descargas grandes: reintentar por bloques
        pend = faltan()
        if not pend:
            break
        print(f"  reintento {ronda + 1}: {len(pend)} valores sin precio")
        for k in range(0, len(pend), 40):
            bloque = pend[k:k + 40]
            try:
                extra = yf.download(bloque, period="max", interval="1d", auto_adjust=True, progress=False,
                                    group_by="ticker", threads=False)
                for t in bloque:
                    if t in extra.columns.get_level_values(0):
                        serie = extra[t]["Close"].copy()
                        serie.index = pd.to_datetime(serie.index).tz_localize(None).normalize()
                        if serie.dropna().size:
                            cierres = cierres.reindex(cierres.index.union(serie.index))
                            cierres[t] = serie
            except Exception:
                pass
            time.sleep(4)
        time.sleep(20)
    print(f"  sin precio tras reintentos: {len(faltan())}")
    cierres.to_csv(RAW / "precios_yahoo.csv")

    print("· fichas de Yahoo (capitalización, sector, analistas), con caché…")
    cache_f = RAW / "info_cache.json"
    cache = json.loads(cache_f.read_text(encoding="utf-8")) if cache_f.exists() else {}
    hoy_txt = HOY.date().isoformat()

    principales = {t for _, t in sorted(((cache.get(a["ticker"], {}).get("marketCap") or 0, a["ticker"]) for a in activos
                                          if a["clase"] == "Acciones"), reverse=True)[:150]}

    def caducada(t, a):
        c = cache.get(t)
        if not c or not (c.get("marketCap") or c.get("longName") or c.get("shortName")):
            return not rapido
        if rapido and not (t in principales or str(a.get("nucleo", "0")) == "1" or a["clase"] != "Acciones"):
            return False
        dias = (HOY - pd.Timestamp(c.get("_fecha", "2000-01-01"))).days
        return dias >= (1 if str(a.get("nucleo", "0")) == "1" or a["clase"] != "Acciones" else 7)

    def info(t):
        tk = yf.Ticker(t)
        for intento in range(4):
            try:
                i = tk.info or {}
                if i.get("marketCap") or i.get("longName") or i.get("shortName") or i.get("quoteType"):
                    break
            except Exception:
                pass
            time.sleep(8 * (intento + 1))                 # Yahoo limita las peticiones: esperar y reintentar
        else:
            return t, None
        if i.get("quoteType") == "EQUITY":
            try:
                cal = tk.calendar or {}
                fechas = [f for f in (cal.get("Earnings Date") or []) if pd.Timestamp(f) >= HOY]
                i["_prox_resultados"] = str(fechas[0]) if fechas else ""
            except Exception:
                i["_prox_resultados"] = ""
        i["_fecha"] = hoy_txt
        return t, i
    pendientes = [a["ticker"] for a in activos if caducada(a["ticker"], a)]
    print(f"  {len(pendientes)} fichas que actualizar ({len(activos) - len(pendientes)} en caché)")
    hechas = 0
    with ThreadPoolExecutor(4) as ex:
        for t, i in ex.map(info, pendientes):
            if i:
                cache[t] = i
                hechas += 1
                if hechas % 200 == 0:
                    cache_f.write_text(json.dumps(cache, default=str, ensure_ascii=False), encoding="utf-8")
                    print(f"  {hechas} fichas…")
    cache_f.write_text(json.dumps(cache, default=str, ensure_ascii=False), encoding="utf-8")
    infos = {t: cache.get(t, {}) for t in tickers}
    print(f"  fichas con datos: {sum(1 for v in infos.values() if v)} de {len(tickers)}")
    (RAW / "info_yahoo.json").write_text(json.dumps(infos, default=str, ensure_ascii=False), encoding="utf-8")

    caps = sorted([(infos.get(a["ticker"], {}).get("marketCap") or 0, a["ticker"]) for a in activos
                   if a["clase"] == "Acciones"], reverse=True)
    top = {t for _, t in caps[:150]}
    for a in activos:
        a["nucleo"] = 1 if (str(a.get("nucleo", "0")) == "1" or a["ticker"] in top) else 0
        a["_cap"] = infos.get(a["ticker"], {}).get("marketCap") or 0
    # orden de importancia: por clase; dentro de cada clase, índices en su orden y el resto por capitalización
    clases = list(PRIOR)
    activos = [a for _, a in sorted(enumerate(activos), key=lambda x: (
        clases.index(x[1]["clase"]), x[0] if x[1]["clase"] == "Índices" else -x[1]["_cap"]))]
    tickers = [a["ticker"] for a in activos]
    print(f"· noticias recientes (Google News): {sum(a['nucleo'] for a in activos)} activos con cobertura completa…")

    def tres_busquedas(a):
        if rapido:                                   # solo lo nuevo de los principales; se suma a lo ya guardado
            nuevas = (google_news(a["consulta"] + " when:2d", a["idioma"], maximo=40) + yahoo_news(a["ticker"]))                 if a["nucleo"] else []
            return nuevas + previas.get(a["ticker"], [])
        if not a["nucleo"]:
            grande = a["_cap"] >= 10e9
            return (google_news(a["consulta"], a["idioma"], maximo=40) if grande else []) + yahoo_news(a["ticker"])
        prem = PREMIUM_ES if a["idioma"] == "es" else PREMIUM
        base = a["consulta"].replace(" stock", "").replace(" bolsa", "")
        return (google_news(a["consulta"] + " when:3d", a["idioma"], maximo=40)      # lo de hoy y ayer
                + google_news(f"{base} {prem}", a["idioma"], maximo=60)             # solo prensa de referencia
                + google_news(a["consulta"], a["idioma"], maximo=60)                # cobertura general
                + google_news(f"{a['nombre']} " + ("stock" if a["idioma"] == "es" else "bolsa acciones"),
                              "en" if a["idioma"] == "es" else "es", maximo=40)        # el otro idioma
                + yahoo_news(a["ticker"]))
    with ThreadPoolExecutor(6) as ex:
        recientes = dict(zip(tickers, ex.map(tres_busquedas, activos)))

    print("· analistas de bancos de inversión y resultados trimestrales…")
    acciones = [(k, a["ticker"]) for k, a in enumerate(activos, start=1) if a["clase"] == "Acciones" and a["nucleo"]]
    analistas, resultados = [], []
    with ThreadPoolExecutor(6) as ex:
        for ana, res in ex.map(profesionales, acciones):
            analistas += ana
            resultados += res
    for prev, lista in ((prev_an, analistas), (prev_res, resultados)):
        if prev is None or not len(prev):
            continue
        con = {x["id_activo"] for x in lista}
        por_tk = dict(tuple(prev.groupby("ticker")))
        for orden, t in acciones:
            if orden not in con and t in por_tk:
                lista += por_tk[t].drop(columns="ticker").assign(id_activo=orden).to_dict("records")
    print(f"  {len(analistas)} notas de bancos, {len(resultados)} fechas de resultados")
    print("· documentos oficiales presentados a la SEC…")
    documentos = sec_documentos(acciones)

    filas_activo, filas_serie, filas_noticia, filas_evento = [], [], [], []
    serie_tmp = CLEAN / "serie.tmp.csv"
    serie_tmp.unlink(missing_ok=True)
    peticiones_evento = []
    for orden, a in enumerate(activos, start=1):
        t = a["ticker"]
        if t not in cierres or cierres[t].dropna().size < 60 or cierres[t].dropna().index[-1] < HOY - pd.Timedelta(days=10):
            print("  sin datos:", t)
            continue
        s = cierres[t].dropna()
        s = s[s > 0]                             # precios a cero (cripto de micro-céntimos) rompen los cálculos
        if a["clase"] in ("Acciones", "ETFs"):
            s = corregir_peniques(s)
        r = np.log(s).diff().dropna()
        s0, fecha0 = float(s.iloc[-1]), s.index[-1]
        ses_anio = 365 if a["clase"] == "Cripto" else 252          # la cripto cotiza todos los días
        anual = math.sqrt(ses_anio)
        r1a = r[r.index > fecha0 - pd.Timedelta(days=365)]
        vol_1a = float(r1a.std() * anual)
        ewma = float(np.sqrt((r ** 2).ewm(alpha=0.06).mean().iloc[-1]) * anual)
        sigma = 0.5 * vol_1a + 0.5 * ewma
        r5 = s[s.index > fecha0 - pd.Timedelta(days=5 * 365)]
        anios = max((r5.index[-1] - r5.index[0]).days / 365.25, 0.5)
        mu_hist = clip((r5.iloc[-1] / r5.iloc[0]) ** (1 / anios) - 1, -0.5, 0.6)
        i = infos.get(t, {})
        if a["clase"] == "Acciones" and i.get("quoteType") in ("ETF", "MUTUALFUND", "INDEX", "CLOSEDENDFUND"):
            continue                                         # en Acciones solo empresas
        objetivo = i.get("targetMeanPrice") if a["clase"] == "Acciones" else None
        n_anal = i.get("numberOfAnalystOpinions") or 0
        potencial = (objetivo / s0 - 1) if objetivo and n_anal >= 3 else None
        prior = PRIOR[a["clase"]]
        if potencial is not None:
            mu = 0.4 * prior + 0.3 * mu_hist + 0.3 * clip(potencial, -0.5, 0.6)
        else:
            mu = 0.6 * prior + 0.4 * mu_hist

        def ret(dias):
            p = s[s.index <= fecha0 - pd.Timedelta(days=dias)]
            return float(s0 / p.iloc[-1] - 1) if len(p) else None
        ytd = s[s.index < pd.Timestamp(fecha0.year, 1, 1)]
        maxdd = float((s[s.index > fecha0 - pd.Timedelta(days=5 * 365)] /
                       s[s.index > fecha0 - pd.Timedelta(days=5 * 365)].cummax() - 1).min())
        sma50, sma200 = float(s.iloc[-50:].mean()), float(s.iloc[-200:].mean()) if len(s) >= 200 else float(s.mean())

        # Noticias recientes y su tono
        vistos = set()
        nots = [n for n in recientes.get(t, []) if not (n["titular"] in vistos or vistos.add(n["titular"]))]
        nots = [n for n in nots if n["fecha"] >= (HOY - pd.Timedelta(days=120)).date()]

        def tono_medio(dias):
            v = [(tono(n["titular"]), peso_noticia(n)) for n in nots
                 if n["fecha"] >= (HOY - pd.Timedelta(days=dias)).date()]
            peso = sum(w for _, w in v)
            return (sum(t * w for t, w in v) / peso, len(v)) if peso else (0.0, 0)
        tono14, n14 = tono_medio(14)
        tono60, n60 = tono_medio(60)

        # ---------- Señales profesionales ----------
        ana = [x for x in analistas if x["id_activo"] == orden and x["fecha"] >= (HOY - pd.Timedelta(days=90)).date().isoformat()]
        mej = sum(x["accion"] == "Mejora" for x in ana)
        reb = sum(x["accion"] == "Rebaja" for x in ana)
        subidas = [x for x in ana if x["precio_objetivo"] and x["objetivo_anterior"]]
        neto_obj = sum(np.sign(x["precio_objetivo"] - x["objetivo_anterior"]) for x in subidas)
        rev = (mej - reb + 0.5 * neto_obj) / max(len(ana), 1)
        rev_pts = clip(rev * 12, -8, 8) if len(ana) >= 3 else 0
        rev_txt = (f"{len(ana)} notas de bancos en 90 días ({mej} mejoras, {reb} rebajas, "
                   f"{sum(1 for x in subidas if x['precio_objetivo'] > x['objetivo_anterior'])} suben objetivo)") if ana else ""
        res = sorted([x for x in resultados if x["id_activo"] == orden], key=lambda x: x["fecha"])
        pasados = [x for x in res if x["bpa_real"] == x["bpa_real"] and x["bpa_real"] is not None][-4:]
        sorp = [x["sorpresa_pct"] for x in pasados if x["sorpresa_pct"] == x["sorpresa_pct"]]
        sorpresa_pts = clip(np.mean(sorp) / 5, -1, 1) * 5 if sorp else 0
        sorpresa_txt = (f"BPA frente a lo esperado, últimos {len(sorp)} trimestres: "
                        f"{'+' if np.mean(sorp) > 0 else ''}{np.mean(sorp):.1f} % de media".replace(".", ",")) if sorp else ""
        futuros = [x["fecha"] for x in res if x["fecha"] >= HOY.date().isoformat()]
        prox_res = futuros[0] if futuros else (i.get("_prox_resultados", "")[:10] if a["clase"] == "Acciones" else "")
        # movimiento típico en días de resultados (máximo del día y el siguiente, por las publicaciones tras el cierre)
        movs = []
        for x in pasados + [y for y in res if y not in pasados][:0]:
            d = pd.Timestamp(x["fecha"])
            v = r[(r.index >= d) & (r.index <= d + pd.Timedelta(days=4))].head(2).abs()
            if len(v):
                movs.append(float(v.max()))
        mov_res = float(np.mean(movs)) if movs else 0.0
        dias_res = (pd.Timestamp(prox_res) - HOY).days if prox_res else 999

        # ---------- Parámetros de la trayectoria prevista ----------
        g_lp = math.log(1 + 0.7 * prior + 0.3 * clip(mu_hist, -0.2, 0.25))
        r12_1 = None
        p_1m, p_12m = s[s.index <= fecha0 - pd.Timedelta(days=30)], s[s.index <= fecha0 - pd.Timedelta(days=365)]
        if len(p_1m) and len(p_12m) and vol_1a > 0:
            r12_1 = float(p_1m.iloc[-1] / p_12m.iloc[-1] - 1)
        g_cp = g_lp + (0.10 * clip(r12_1 / vol_1a, -2, 2) * vol_1a if r12_1 is not None else 0.0)
        if g_lp > 0:                                       # el momentum no cambia el signo de la línea central
            g_cp = max(g_cp, 0.0)
        elif g_lp < 0:
            g_cp = min(g_cp, 0.0)
        r5l = np.log(r5).diff().dropna()
        tray = {"g_lp": g_lp, "g_cp": g_cp, "sig0": ewma,
                "sig_lp": float(r5l.std() * anual) if len(r5l) > 60 else sigma,
                "obj": 0.5 * math.log(1 + clip(potencial - 0.10, -0.3, 0.3)) if potencial is not None else 0.0}
        mu = math.exp(trayectoria(tray, 1.0)[0]) - 1         # rentabilidad central a 12 meses

        # ---------- Previsión por horizonte ----------
        prev = {}
        # a 5 años no se usan objetivos de analistas (son a 12 meses) y el histórico se acota: domina la referencia de la clase
        mu_largo = 0.6 * prior + 0.4 * clip(mu_hist, -0.2, 0.25)
        for hz, ses in HORIZONTES.items():
            ses_h = round(ses * ses_anio / 252)
            extra = mov_res if 0 <= dias_res <= ses_h * 1.45 else 0.0
            q, m, sd = cuantiles_tray(s0, tray, ses_h / ses_anio, extra ** 2, 0.15 if a["clase"] == "Acciones" else 0.0)
            prev[hz] = {"p10": q["p10"] / s0 - 1, "p50": q["p50"] / s0 - 1, "p90": q["p90"] / s0 - 1,
                        "perdida": norm_cdf(-m / sd) if sd > 0 else 0.5}

        # ---------- Puntuaciones ----------
        nivel = srri(vol_1a)
        # una acción casi inmóvil (p. ej. con una OPA encima) daría puntuaciones extremas: volatilidad mínima del 15 %
        sig_z = max(sigma, 0.15) if a["clase"] == "Acciones" else sigma
        cons = CONSENSO.get(i.get("recommendationKey") or "", (None, 0)) if a["clase"] == "Acciones" and n_anal >= 3 else (None, 0)
        z_t50 = clip((s0 / sma50 - 1) / (sig_z * math.sqrt(50 / 252)), -2, 2)
        r1m = ret(30) or 0.0
        z_1m = clip(r1m / (sig_z * math.sqrt(21 / 252)), -2, 2)
        corto = [(z_t50 * 10, f"precio {pct(s0 / sma50 - 1)} frente a su media de 50 sesiones"),
                 (z_1m * 8, f"último mes {pct(r1m)}"),
                 ((tono14 * 15) if n14 >= 3 else 0, f"tono de {n14} titulares ponderados por fiabilidad (14 días): " +
                  ("positivo" if tono14 > 0.15 else "negativo" if tono14 < -0.15 else "neutro")),
                 (-(nivel - 4) * 2, f"riesgo {nivel}/7")]
        r12_1 = (s.iloc[-21] / s[s.index <= fecha0 - pd.Timedelta(days=365)].iloc[-1] - 1) if ret(365) is not None else 0
        z_t200 = clip((s0 / sma200 - 1) / (sig_z * math.sqrt(200 / 252)), -2, 2)
        medio = [(z_t200 * 10, f"precio {pct(s0 / sma200 - 1)} frente a su media de 200 sesiones"),
                 (clip(r12_1 / sig_z, -2, 2) * 8, f"tendencia de 12 meses {pct(r12_1)}"),
                 (clip(((potencial or 0) - 0.10) / sig_z, -2, 2) * 10 if potencial is not None else 0,   # los objetivos suelen ir un 10 % por encima
                  f"objetivo medio de {n_anal} analistas: {pct(potencial)}" if potencial is not None else "sin cobertura de analistas"),
                 (cons[1], f"consenso: {cons[0]}" if cons[0] else ""),
                 (rev_pts, rev_txt), (sorpresa_pts, sorpresa_txt),
                 ((tono60 * 10) if n60 >= 5 else 0, f"tono de {n60} titulares ponderados por fiabilidad (60 días)"),
                 (-(nivel - 4) * 2, f"riesgo {nivel}/7")]
        sharpe = (mu_largo - (0 if a["clase"] == "Divisas" else 0.03)) / sig_z if sig_z > 0 else 0
        largo = [(clip(sharpe, -1.2, 1.2) * 20, f"rentabilidad esperada {pct(mu_largo, 1)} al año con volatilidad del {sigma * 100:.0f} %"),
                 (-8 if maxdd < -0.6 else (-4 if maxdd < -0.4 else 0), f"peor caída en 5 años {pct(maxdd)}"),
                 (cons[1] * 0.5, f"consenso: {cons[0]}" if cons[0] else ""),
                 (-(nivel - 6) * 2, f"riesgo {nivel}/7")]

        def puntua(comp):
            sc = clip(50 + sum(c for c, _ in comp), 0, 100)
            mot = [txt for c, txt in sorted(comp, key=lambda x: -abs(x[0])) if txt and abs(c) >= 1][:3]
            return round(sc), " · ".join(mot) or "señales neutras"
        sc_c, mot_c = puntua(corto)
        sc_m, mot_m = puntua(medio)
        sc_l, mot_l = puntua(largo)

        web = i.get("website") or ""
        dominio = a["dominio"] or re.sub(r"^https?://(www\.)?", "", web).split("/")[0]
        nombre_largo = i.get("longName") or i.get("shortName") or a["nombre"]
        fila = {
            "id": orden, "ticker": t, "nombre": a["nombre"], "nombre_largo": nombre_largo, "clase": a["clase"],
            "mercado": a.get("mercado", ""), "nucleo": a["nucleo"], "elegir": a["nombre"],
            "clase_orden": list(PRIOR).index(a["clase"]) + 1,
            "sector": i.get("sector") or {"ETFs": "Fondo cotizado", "Índices": "Índice bursátil", "Cripto": "Criptomoneda",
                                          "Divisas": "Tipo de cambio", "Renta fija": "Bonos"}.get(a["clase"], ""),
            "industria": i.get("industry") or "", "pais": i.get("country") or "",
            "moneda": i.get("currency") or ("%" if t == "^TNX" else ""),
            "logo": f"https://www.google.com/s2/favicons?domain={dominio}&sz=128",
            "web": web or f"https://{dominio}", "orden": orden,
            "fecha_dato": fecha0.date().isoformat(), "precio": s0,
            "var_1d": float(s0 / s.iloc[-2] - 1), "var_1m": r1m, "var_ytd": float(s0 / ytd.iloc[-1] - 1) if len(ytd) else None,
            "var_1a": ret(365), "var_5a": ret(5 * 365),
            "vol_anual": vol_1a, "max_caida_5a": maxdd, "riesgo": nivel,
            "beta": i.get("beta"), "per": i.get("trailingPE"), "cap_bursatil": i.get("marketCap"),
            "consenso": cons[0] or "", "n_analistas": n_anal if a["clase"] == "Acciones" else 0,
            "obj_medio": objetivo, "obj_alto": i.get("targetHighPrice") if objetivo else None,
            "obj_bajo": i.get("targetLowPrice") if objetivo else None, "potencial": potencial,
            "prox_resultados": prox_res, "dias_a_resultados": dias_res if dias_res < 999 else None,
            "mov_medio_resultados": mov_res or None, "notas_bancos_90d": len(ana), "mejoras_90d": mej, "rebajas_90d": reb,
            "mu": mu, "sigma": sigma, "tono_60d": tono60, "n_noticias_60d": n60,
            "score_corto": sc_c, "rec_corto": etiqueta(sc_c), "motivo_corto": mot_c,
            "score_medio": sc_m, "rec_medio": etiqueta(sc_m), "motivo_medio": mot_m,
            "score_largo": sc_l, "rec_largo": etiqueta(sc_l), "motivo_largo": mot_l,
            # desglose de la puntuación (puntos que suma o resta cada factor) para explicarla en el informe
            **{f"comp_{k}": " ;; ".join(f"{c:+.0f}|{txt}" for c, txt in comp if txt and abs(c) >= 0.5)
               for k, comp in (("corto", corto), ("medio", medio), ("largo", largo))},
        }
        for hz in HORIZONTES:
            for k, v in prev[hz].items():
                fila[f"{hz}_{k}"] = v
        filas_activo.append(fila)

        # ---------- Serie: histórico + previsión a 12 meses ----------
        corte = fecha0 - pd.Timedelta(days=(3 if a["nucleo"] else 2) * 365)
        antigua = s[s.index < corte].resample("W-FRI").last().dropna()
        # se escribe al disco activo a activo (con 2.500 activos, guardarlo todo en memoria agota la RAM)
        hist = pd.concat([antigua, s[s.index >= corte]])
        bloque = pd.DataFrame({"id": orden, "fecha": hist.index.strftime("%Y-%m-%d"), "cierre": hist.values})
        for c in ("mediana", "b80_inf", "b80_sup", "b95_inf", "b95_sup"):
            bloque[c] = np.nan
        bloque.loc[bloque.index[-1], ["mediana", "b80_inf", "b80_sup", "b95_inf", "b95_sup"]] = s0   # arranque de la previsión
        dias = pd.bdate_range(fecha0 + pd.Timedelta(days=1), periods=252) if ses_anio == 252 else \
            pd.date_range(fecha0 + pd.Timedelta(days=1), periods=365)
        prev_filas = []
        for k, f in enumerate(dias, start=1):
            extra = mov_res ** 2 if (prox_res and f >= pd.Timestamp(prox_res)) else 0.0
            q, _, _ = cuantiles_tray(s0, tray, k / ses_anio, extra)
            prev_filas.append((orden, f.strftime("%Y-%m-%d"), np.nan, q["p50"], q["p10"], q["p90"], q["p025"], q["p975"]))
        bloque = pd.concat([bloque, pd.DataFrame(prev_filas, columns=bloque.columns)])
        bloque.to_csv(serie_tmp, mode="a", header=not serie_tmp.exists(), index=False, encoding="utf-8")
        filas_serie.append(len(bloque))

        for n in nots:
            filas_noticia.append({"id_activo": orden, **n, "evento": 0})

        # ---------- Grandes movimientos (2 años) para buscar su porqué ----------
        if a["clase"] != "Divisas" or a["nucleo"]:
            s2 = s[s.index > fecha0 - pd.Timedelta(days=730)]
            cand = []
            r1 = s2.pct_change().dropna()
            for f, v in r1[r1.abs() > 3 * r1.std()].items():
                cand.append((abs(v) / r1.std(), f, f, float(v), "1 día"))
            r5 = (s2 / s2.shift(5) - 1).dropna()
            sd5 = r5.std()
            for f, v in r5[r5.abs() > 2.2 * sd5].items():
                cand.append((abs(v) / sd5, s2.index[s2.index.get_loc(f) - 5], f, float(v), "1 semana"))
            elegidos = []
            if not a["nucleo"] and a["_cap"] < 10e9:
                cand = []                                   # cobertura básica: sin búsqueda de eventos
            for z, ini, fin, v, tipo in sorted(cand, reverse=True):
                if all(abs((fin - e[2]).days) > 10 for e in elegidos):
                    elegidos.append((z, ini, fin, v, tipo))
                if len(elegidos) >= (8 if a["nucleo"] else (1 if a["_cap"] >= 10e9 else 0)):
                    break
            for z, ini, fin, v, tipo in sorted(elegidos, key=lambda x: x[2]):
                if not rapido:
                    peticiones_evento.append((orden, a, ini, fin, v, tipo))
        if rapido:                                   # grandes movimientos: los de la última descarga completa
            for _, e in ev_prev[ev_prev["ticker"] == t].iterrows():
                filas_evento.append({**{k: e[k] for k in ev_prev.columns if k != "ticker"}, "id_activo": orden})
            for _, n in nev_prev[nev_prev["ticker"] == t].iterrows():
                filas_noticia.append({"id_activo": orden, "fecha": n["fecha"], "titular": n["titular"], "fuente": n["fuente"],
                                      "enlace": n["enlace"], "evento": 1, "fecha_evento": n["fecha_evento"],
                                      "retorno_evento": n["retorno_evento"]})

    print(f"· noticias de {len(peticiones_evento)} grandes movimientos…")

    def buscar_evento(p):
        orden, a, ini, fin, rr, tipo = p
        q = a["consulta"].replace(" stock", "").replace(" price", "")
        nots = google_news(q, a["idioma"], ini - pd.Timedelta(days=2), fin + pd.Timedelta(days=1), maximo=12)
        nots.sort(key=lambda n: (nivel_fuente(n["fuente"]), -abs(tono(n["titular"]))))   # fiables y con tono claro primero
        return p, nots[:6]
    with ThreadPoolExecutor(6) as ex:
        for (orden, a, ini, f, rr, tipo), nots in ex.map(buscar_evento, peticiones_evento):
            fila_ev = {"id_activo": orden, "fecha": f.date().isoformat(), "fecha_ini": ini.date().isoformat(),
                       "periodo": tipo, "retorno": rr, "n_titulares": len(nots),
                       "titular": nots[0]["titular"] if nots else "Sin titulares encontrados en prensa para esas fechas"}
            for k in range(3):
                fila_ev[f"contexto{k + 1}"] = (nots[k]["titular"] + " (" + nots[k]["fuente"] + ")") if len(nots) > k else ""
            filas_evento.append(fila_ev)
            for n in nots:
                filas_noticia.append({"id_activo": orden, **n, "evento": 1, "fecha_evento": f.date().isoformat(),
                                      "retorno_evento": rr})

    # ---------- Escribir ----------
    act = pd.DataFrame(filas_activo)
    act = act.replace([np.inf, -np.inf], np.nan)
    act.to_csv(CLEAN / "activos.csv", index=False, encoding="utf-8")
    serie_tmp.replace(CLEAN / "serie.csv")
    ser = range(sum(filas_serie))
    ev = pd.DataFrame(filas_evento)
    ev.to_csv(CLEAN / "eventos.csv", index=False, encoding="utf-8")
    no = pd.DataFrame(filas_noticia)
    no["fecha"] = no["fecha"].astype(str)
    no = no.drop_duplicates(subset=["id_activo", "titular"]).copy()
    no["tema"] = no["titular"].map(tema)
    no["tono"] = no["titular"].map(tono)
    no["nivel_fuente"] = no["fuente"].map(nivel_fuente)
    no["tipo_fuente"] = no["nivel_fuente"].map({1: "Prensa de referencia", 2: "Prensa especializada",
                                                3: "Opinión y blogs"})
    no = verificar(no)
    no["tono_txt"] = no["tono"].map({1: "Positivo", 0: "Neutro", -1: "Negativo"})
    no.insert(0, "id_noticia", range(1, len(no) + 1))
    for c in ("fecha_evento", "retorno_evento"):
        if c not in no:
            no[c] = None
    no.to_csv(CLEAN / "noticias.csv", index=False, encoding="utf-8")
    cat = catalizadores(no, ev, act, resultados)
    cat.to_csv(CLEAN / "catalizadores.csv", index=False, encoding="utf-8")
    print(f"  {len(cat)} catalizadores detectados")
    pd.DataFrame(analistas).to_csv(CLEAN / "analistas.csv", index=False, encoding="utf-8")
    pd.DataFrame(resultados).to_csv(CLEAN / "resultados.csv", index=False, encoding="utf-8")
    pd.DataFrame(documentos).to_csv(CLEAN / "documentos.csv", index=False, encoding="utf-8")
    pd.DataFrame([{"fuente": "Yahoo Finance, Google News, SEC EDGAR", "fecha": datetime.now().strftime("%Y-%m-%d"), "hora": datetime.now().strftime("%H:%M")}]) \
        .to_csv(CLEAN / "actualizacion.csv", index=False, encoding="utf-8")
    print(f"ok: {len(act)} activos, {len(ser)} filas de serie, {len(no)} noticias, {len(ev)} grandes movimientos")
    import enriquecer                                  # orden en dólares, catalizadores y explicaciones
    enriquecer.main()


if __name__ == "__main__":
    candado = RAW / "descarga_completa.lock"           # la rápida no corre mientras la completa está en marcha
    if "rapido" in sys.argv:
        if candado.exists() and time.time() - candado.stat().st_mtime < 14 * 3600:
            print("Hay una descarga completa en marcha: la rápida no se lanza.")
            sys.exit(0)
        main()
    else:
        RAW.mkdir(parents=True, exist_ok=True)
        candado.write_text(str(os.getpid()))
        try:
            main()
        finally:
            candado.unlink(missing_ok=True)
