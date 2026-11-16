#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
amazon_category_scraper.py  -  Investigación de nicho para Kindle

Recorre una lista de libros de Amazon (bestsellers, resultados de búsqueda de una
categoría o tu propia lista de ASIN), entra en la ficha de CADA libro y guarda
todo en un CSV. Después analiza el CSV para ver si hay hueco y demanda.

INSTALACIÓN (una vez):
    pip install playwright
    playwright install chromium

1) SACAR DATOS
    python amazon_category_scraper.py scrape ^
        --list-url "https://www.amazon.com/gp/bestsellers/digital-text/NODEID" ^
        --pages 2 --domain amazon.com --out cozy_1920s.csv --headed

    (en Mac/Linux usa \\ en vez de ^ para partir la línea)

    - --list-url se puede repetir (varias categorías / varias listas).
    - --asin-file asins.txt  -> un ASIN por línea, si ya tienes la lista.
    - Si se corta, vuelve a lanzar el mismo comando: salta lo que ya está en el CSV.
    - --headed abre la ventana del navegador. Si sale captcha, lo resuelves a mano
      y pulsas Enter en la terminal.

2) ANALIZAR
    python amazon_category_scraper.py analyze cozy_1920s.csv
"""
import argparse
import csv
import os
import random
import re
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

ASIN_RE = re.compile(r"/(?:dp|gp/product)/([A-Z0-9]{10})")
BIDI = re.compile(r"[\u200e\u200f\u202a-\u202e]")

FIELDS = [
    "asin", "url", "list_pos", "title", "author", "price", "currency",
    "rating", "n_ratings", "pub_date", "age_days", "pages", "language",
    "publisher", "series_name", "series_number", "series_total",
    "bsr_overall", "category_ranks", "ku", "desc_chars", "scraped_at",
]

MONTHS = {
    "jan": 1, "ene": 1, "feb": 2, "mar": 3, "apr": 4, "abr": 4, "may": 5,
    "jun": 6, "jul": 7, "aug": 8, "ago": 8, "sep": 9, "oct": 10, "nov": 11,
    "dec": 12, "dic": 12,
}


# --------------------------------------------------------------------------
#  PARSEO (puro Python, sin navegador: se puede probar offline)
# --------------------------------------------------------------------------
def to_int(s):
    d = re.sub(r"\D", "", s or "")
    return int(d) if d else None


def to_float(s):
    m = re.search(r"\d+(?:[.,]\d+)?", s or "")
    return float(m.group(0).replace(",", ".")) if m else None


def parse_date(s):
    if not s:
        return None
    s = s.strip().lower().replace(".", " ")
    m = re.search(r"(\d{1,2})\s+(?:de\s+)?([a-záéíóú]+)\s+(?:de\s+)?(\d{4})", s)
    if m:
        mon = MONTHS.get(m.group(2)[:3])
        if mon:
            try:
                return date(int(m.group(3)), mon, int(m.group(1)))
            except ValueError:
                return None
    m = re.search(r"([a-z]+)\s+(\d{1,2}),?\s+(\d{4})", s)
    if m:
        mon = MONTHS.get(m.group(1)[:3])
        if mon:
            try:
                return date(int(m.group(3)), mon, int(m.group(2)))
            except ValueError:
                return None
    return None


def parse_price(s):
    if not s:
        return None, ""
    cur = next((c for c in "€$£" if c in s), "")
    m = re.search(r"(\d+[.,]\d{2})", s)
    if m:
        return float(m.group(1).replace(",", ".")), cur
    if re.search(r"free|gratis", s, re.I):
        return 0.0, cur
    return None, cur


def grab(text, labels):
    for lab in labels:
        m = re.search(lab + r"\s*:\s*([^\n]+)", text, re.I)
        if m:
            return m.group(1).strip()
    return None


def parse_bsr(text):
    """Devuelve (ranking_general, 'Categoría #rank | Categoría #rank ...')."""
    m = re.search(
        r"(?:Best Sellers Rank|Clasificación en los más vendidos(?: de Amazon)?)\s*:?\s*(.*)",
        text, re.S | re.I)
    if not m:
        return None, ""
    block = m.group(1)
    end = re.search(r"Customer reviews?|Opini[oó]n media|Rese[ñn]as de clientes|"
                    r"About the author|Sobre el autor", block, re.I)
    if end:
        block = block[:end.start()]
    block = re.sub(r"\([^)]*\)", "", block[:800])  # quita "(See Top 100 in ...)"
    items = re.findall(r"(?:N[ºo]\s*)?(\d[\d.,]*)\s+(?:in|en)\s+([^\n]+)", block)
    if not items:
        return None, ""
    overall = to_int(items[0][0])
    cats = " | ".join(f"{name.strip()} #{to_int(rank)}" for rank, name in items[1:])
    return overall, cats


def parse_page(text, extra=None):
    """text = texto visible de la ficha; extra = datos sacados por selectores."""
    extra = extra or {}
    t = BIDI.sub("", text or "")
    r = {k: None for k in FIELDS}
    r["title"] = (extra.get("title") or "").strip()
    r["author"] = (extra.get("author") or "").strip()
    r["price"], r["currency"] = parse_price(extra.get("price_text"))

    # valoraciones
    rt = (extra.get("rating_text") or "") + "\n" + t
    m = re.search(r"(\d[.,]\d)\s*(?:out of 5|de 5)", rt)
    if m:
        r["rating"] = to_float(m.group(1))
    m = re.search(r"Customer reviews?\s*:\s*(\d[.,]\d)[^\n(]*\((\d[\d.,]*)\)", t, re.I)
    if m:
        if r["rating"] is None:
            r["rating"] = to_float(m.group(1))
        r["n_ratings"] = to_int(m.group(2))
    if r["n_ratings"] is None:
        m = re.search(r"([\d.,]+)\s+(?:global\s+)?(?:ratings?|valoraciones|calificaciones)",
                      (extra.get("ratings_text") or "") + "\n" + t, re.I)
        if m:
            r["n_ratings"] = to_int(m.group(1))
        elif re.fullmatch(r"\(?\s*[\d.,]+\s*\)?", extra.get("ratings_text") or ""):
            r["n_ratings"] = to_int(extra["ratings_text"])

    # ficha técnica
    d = parse_date(grab(t, ["Publication date", "Fecha de publicación"]))
    if d:
        r["pub_date"] = d.isoformat()
        r["age_days"] = (date.today() - d).days
    r["pages"] = to_int(grab(t, ["Print length", "Longitud de impresión"]))
    r["language"] = grab(t, ["Language", "Idioma"])
    r["publisher"] = grab(t, ["Publisher", "Editorial"])

    # serie
    m = re.search(r"(?:Book|Libro)\s+(\d+)\s+(?:of|de)\s+(\d+)\s*:?\s*([^\n]+)", t)
    if m:
        r["series_number"], r["series_total"] = int(m.group(1)), int(m.group(2))
        r["series_name"] = m.group(3).strip()
    else:
        m = re.search(r"(?:Part of|Parte de)\s*:\s*([^\n(]+)\((\d+)", t)
        if m:
            r["series_name"], r["series_total"] = m.group(1).strip(), int(m.group(2))

    r["bsr_overall"], r["category_ranks"] = parse_bsr(t)
    r["ku"] = int(bool(re.search(
        r"Read for Free|Lee gratis|Leer gratis|Gratis con Kindle Unlimited", t, re.I)))
    r["desc_chars"] = len(extra.get("desc_text") or "")
    return r


# --------------------------------------------------------------------------
#  NAVEGADOR (Playwright)
# --------------------------------------------------------------------------
def set_param(url, key, value):
    u = urlparse(url)
    q = dict(parse_qsl(u.query))
    q[key] = str(value)
    return urlunparse(u._replace(query=urlencode(q)))


def polite_sleep(a):
    time.sleep(random.uniform(a.min_delay, a.max_delay))


def is_blocked(page):
    c = page.content()[:30000].lower()
    return any(k in c for k in (
        "enter the characters you see", "type the characters you see",
        "api-services-support@amazon", "/errors/validatecaptcha", "robot check"))


def wait_if_blocked(page, headed):
    for _ in range(3):
        if not is_blocked(page):
            return
        if headed:
            input("\n>>> Amazon pide captcha. Resuélvelo en la ventana y pulsa Enter aquí... ")
            page.wait_for_timeout(1500)
        else:
            print("Bloqueo/captcha. Espero 120 s y reintento (usa --headed para resolverlo a mano)")
            time.sleep(120)
            page.reload()
    raise RuntimeError("Sigue bloqueado")


def first_text(page, selectors, attr=None):
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count():
                t = (loc.get_attribute(attr) if attr else loc.inner_text()) or ""
                t = t.strip()
                if t:
                    return t
        except Exception:
            continue
    return ""


def collect_asins(page, list_url, pages, a):
    found = []
    is_best = re.search(r"best-?sellers|zgbs|new-releases|movers", list_url, re.I)
    for pg in range(1, pages + 1):
        url = set_param(list_url, "pg" if is_best else "page", pg)
        print(f"Lista: {url}")
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        wait_if_blocked(page, a.headed)
        for _ in range(8):  # scroll para cargar los items perezosos
            page.mouse.wheel(0, 2500)
            page.wait_for_timeout(700)
        hrefs = page.eval_on_selector_all(
            "a[href*='/dp/']", "els => els.map(e => e.href)")
        for h in hrefs:
            m = ASIN_RE.search(h)
            if m and m.group(1).startswith("B") and m.group(1) not in found:
                found.append(m.group(1))
        polite_sleep(a)
    return found


def scrape_book(page, asin, base, a):
    url = f"{base}/dp/{asin}"
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    wait_if_blocked(page, a.headed)
    page.wait_for_timeout(1200)
    for _ in range(5):  # baja para que cargue la ficha técnica
        page.mouse.wheel(0, 2500)
        page.wait_for_timeout(500)
    extra = {
        "title": first_text(page, ["#productTitle"]),
        "author": first_text(page, ["#bylineInfo .author a", "#bylineInfo a", "span.author a"]),
        "price_text": first_text(page, [
            "#kindle-price", "#tmm-grid-swatch-KINDLE .slot-price",
            "span.kindle-price", "#tmm-grid-swatch-KINDLE .a-color-price",
            ".a-price .a-offscreen"]),
        "rating_text": first_text(page, ["#acrPopover"], attr="title"),
        "ratings_text": first_text(page, ["#acrCustomerReviewText"]),
        "desc_text": first_text(page, ["#bookDescription_feature_div"]),
    }
    r = parse_page(page.inner_text("body"), extra)
    r["asin"], r["url"] = asin, url
    r["scraped_at"] = datetime.now().isoformat(timespec="seconds")
    return r


def load_done(path):
    if not os.path.exists(path):
        return set()
    with open(path, encoding="utf-8-sig") as f:
        return {row["asin"] for row in csv.DictReader(f)}


def cmd_scrape(a):
    from playwright.sync_api import sync_playwright  # import tardío
    base = f"https://www.{a.domain}"
    done = load_done(a.out)
    new_file = not os.path.exists(a.out)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            a.profile, headless=not a.headed, locale="en-US",
            viewport={"width": 1280, "height": 900})
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        asins = []
        for u in a.list_url:
            asins += collect_asins(page, u, a.pages, a)
        if a.asin_file:
            asins += [l.strip() for l in open(a.asin_file) if l.strip()]
        asins = list(dict.fromkeys(asins))
        if a.max_books:
            asins = asins[:a.max_books]
        print(f"{len(asins)} libros en la lista, {len(done)} ya estaban en el CSV")

        with open(a.out, "a", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            if new_file:
                w.writeheader()
            for pos, asin in enumerate(asins, 1):
                if asin in done:
                    continue
                try:
                    r = scrape_book(page, asin, base, a)
                    r["list_pos"] = pos
                    w.writerow(r)
                    f.flush()
                    print(f"[{pos}/{len(asins)}] {asin} | {r['title'][:50]} | "
                          f"BSR {r['bsr_overall']} | {r['pub_date']} | "
                          f"serie: {r['series_name']} {r['series_number']}/{r['series_total']}")
                except Exception as e:
                    print(f"[{pos}/{len(asins)}] {asin} ERROR: {e}")
                polite_sleep(a)
        ctx.close()


# --------------------------------------------------------------------------
#  ANÁLISIS
# --------------------------------------------------------------------------
def fnum(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def med(v):
    v = [x for x in v if x is not None]
    return statistics.median(v) if v else None


def pct(n, d):
    return f"{100 * n / d:.0f}%" if d else "n/d"


def fmt(x, nd=0):
    return "n/d" if x is None else f"{x:,.{nd}f}"


def cmd_analyze(a):
    with open(a.csv, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        sys.exit("El CSV está vacío")
    for r in rows:
        for k in ("price", "rating", "n_ratings", "age_days", "pages",
                  "series_number", "series_total", "bsr_overall", "ku"):
            r[k] = fnum(r.get(k))
    n = len(rows)
    bsr = sorted(r["bsr_overall"] for r in rows if r["bsr_overall"])

    print(f"\n=== {n} libros analizados ({a.csv}) ===")

    print("\n-- DEMANDA (ranking general en Kindle; menor = vende más) --")
    for lim in (20_000, 50_000, 100_000, 250_000):
        c = sum(1 for b in bsr if b <= lim)
        print(f"  con ranking <= {lim:>7,}: {c:>4} libros ({pct(c, n)})")
    print(f"  ranking mediano: {fmt(med(bsr))}")
    for k in (10, 20, 50):
        if len(bsr) >= k:
            print(f"  el {k}º mejor ranking de la muestra: {fmt(bsr[k - 1])}")

    print("\n-- MERCADO --")
    prices = [r["price"] for r in rows if r["price"] is not None]
    print(f"  precio mediano: {fmt(med(prices), 2)}   "
          f"(gratis: {sum(1 for p in prices if p == 0)})")
    print(f"  en Kindle Unlimited (aprox.): {pct(sum(1 for r in rows if r['ku']), n)}")
    print(f"  páginas medianas: {fmt(med([r['pages'] for r in rows]))}")
    print(f"  reviews medianas: {fmt(med([r['n_ratings'] for r in rows]))}   "
          f"(con <100 reviews: {pct(sum(1 for r in rows if (r['n_ratings'] or 0) < 100), n)})")

    print("\n-- SERIES --")
    in_series = [r for r in rows if r["series_name"]]
    print(f"  libros que pertenecen a una serie: {pct(len(in_series), n)}")
    b1 = sum(1 for r in in_series if r["series_number"] == 1)
    b2 = sum(1 for r in in_series if (r["series_number"] or 0) >= 2)
    print(f"  entre ellos, libro 1: {b1}  |  libro 2 o posterior: {b2}")
    print(f"  tamaño mediano de serie: {fmt(med([r['series_total'] for r in in_series]))} libros")
    series = defaultdict(list)
    for r in in_series:
        series[r["series_name"]].append(r)
    print("  series con más libros en la lista:")
    top_series = sorted(series.items(), key=lambda kv: -len(kv[1]))[:5]
    for name, rs in top_series:
        best = min((x["bsr_overall"] for x in rs if x["bsr_overall"]), default=None)
        print(f"    {name[:45]:<45} {len(rs)} libros, mejor ranking {fmt(best)}")

    print("\n-- COMPETENCIA / DOMINIO DE AUTORES --")
    auth = Counter(r["author"] for r in rows if r["author"])
    top5 = auth.most_common(5)
    print(f"  los 5 autores con más títulos acaparan "
          f"{pct(sum(c for _, c in top5), n)} de la lista:")
    for name, c in top5:
        print(f"    {name[:40]:<40} {c} títulos")

    print("\n-- NOVEDADES (publicados hace <= 12 meses) --")
    new = [r for r in rows if r["age_days"] is not None and r["age_days"] <= 365]
    new_ok = [r for r in new if r["bsr_overall"] and r["bsr_overall"] <= a.bsr_ok]
    print(f"  novedades en la lista: {len(new)} ({pct(len(new), n)})")
    print(f"  novedades con ranking <= {a.bsr_ok:,}: {len(new_ok)}")

    gaps = sorted(
        [r for r in new_ok if (r["n_ratings"] or 0) <= a.max_reviews],
        key=lambda r: r["bsr_overall"])
    print(f"\n-- HUECOS: novedades <=12 meses, <= {a.max_reviews} reviews y "
          f"ranking <= {a.bsr_ok:,}: {len(gaps)} --")
    for r in gaps[:15]:
        print(f"  {fmt(r['bsr_overall']):>8} | {r['pub_date']} | "
              f"{fmt(r['n_ratings'])} rev | {fmt(r['price'], 2)} | {r['title'][:55]}")
    if gaps:
        out = os.path.splitext(a.csv)[0] + "_huecos.csv"
        with open(out, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
            w.writeheader()
            w.writerows(gaps)
        print(f"  (guardado en {out})")

    print("\n-- LECTURA RÁPIDA (heurística, no una verdad absoluta) --")
    if len(gaps) >= 5:
        print("  + Hay varias novedades con pocas reviews vendiendo bien: el nicho deja entrar a gente nueva.")
    elif len(gaps) == 0:
        print("  - Casi nadie nuevo y sin reviews logra buen ranking aquí: nicho cerrado o muy dominado.")
    else:
        print("  ~ Pocos casos de entrada fácil: posible, pero no abundante.")
    if bsr and sum(1 for b in bsr if b <= 100_000) / n >= 0.5:
        print("  + Más de la mitad de la lista vende de forma sostenida (ranking <= 100k): hay demanda.")
    else:
        print("  - Menos de la mitad de la lista tiene ranking <= 100k: demanda justa.")
    if top5 and sum(c for _, c in top5) / n >= 0.4:
        print("  - Muy pocos autores acaparan la lista: cuesta más hacerse hueco.")
    if in_series and len(in_series) / n >= 0.6:
        print("  ~ El nicho es de series: el lector espera continuidad; arrancar con libro 1 + secuela cercana.")
    print("  Nota: para pasar ranking -> ventas/día usa una calculadora de ABSR.\n")


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Investigación de nicho Kindle")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scrape", help="recorrer una lista y guardar CSV")
    s.add_argument("--list-url", action="append", default=[],
                   help="URL de bestsellers/búsqueda de la categoría (repetible)")
    s.add_argument("--asin-file", help="fichero con un ASIN por línea")
    s.add_argument("--pages", type=int, default=2, help="páginas de lista por URL")
    s.add_argument("--max-books", type=int, default=0, help="0 = sin límite")
    s.add_argument("--domain", default="amazon.com", help="amazon.com, amazon.es...")
    s.add_argument("--out", default="categoria.csv")
    s.add_argument("--headed", action="store_true", help="mostrar el navegador")
    s.add_argument("--profile", default="./chrome_profile")
    s.add_argument("--min-delay", type=float, default=3.0)
    s.add_argument("--max-delay", type=float, default=8.0)
    s.set_defaults(func=cmd_scrape)

    an = sub.add_parser("analyze", help="analizar un CSV ya generado")
    an.add_argument("csv")
    an.add_argument("--bsr-ok", type=int, default=100_000,
                    help="ranking máximo para considerar que un libro 'vende'")
    an.add_argument("--max-reviews", type=int, default=50,
                    help="reviews máximas para considerar 'sin trayectoria'")
    an.set_defaults(func=cmd_analyze)

    args = ap.parse_args()
    if args.cmd == "scrape" and not (args.list_url or args.asin_file):
        ap.error("pon al menos --list-url o --asin-file")
    args.func(args)


if __name__ == "__main__":
    main()
