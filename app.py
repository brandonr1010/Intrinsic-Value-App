"""
Intrinsic Value — backend proxy v2 (Flask, deployed on Railway)
----------------------------------------------------------------
The app NEVER holds the data-API key. It calls this service, which calls
FMP (/stable API), normalizes the fields the valuation model needs, caches
aggressively, and returns clean JSON with every figure traced to its source.

Endpoints
  GET /health                 liveness, key present, FMP calls used today
  GET /quote/<sym>            price, previousClose, change %   (short cache)
  GET /quotes?symbols=A,B     batch of /quote
  GET /inputs/<sym>           full valuation bundle = fundamentals (12h) + quote
  GET /universe               /inputs for every ticker in FREE_UNIVERSE, one call
  GET /history/<sym>?months=6 daily closes for the price chart (12h cache)
  GET /search/<query>         ticker / company-name autocomplete

Error contract (frontend relies on these)
  404 {"error":"not_found"}     symbol unknown to the provider
  403 {"error":"locked"}        symbol exists but is outside the current data plan
  429 {"error":"rate_limited"}  provider quota hit and nothing cached
  502 {"error":"upstream"}      anything else, nothing cached
  Any cached copy is preferred over an error: served with "_stale": true.

Env vars (Railway → Variables)
  FMP_API_KEY        required
  FUND_TTL           fundamentals cache seconds         (default 43200 = 12h)
  QUOTE_TTL          quote cache seconds, market open   (default 300)
  QUOTE_TTL_CLOSED   quote cache seconds, market closed (default 21600)
  FMP_DAILY_BUDGET   max upstream calls per UTC day      (default 240; free tier = 250)
  FREE_UNIVERSE      comma list for /universe (default WMT,AAPL,MSFT,KO,NKE,XOM,DIS,BAC,JPM)
  ALLOWED_ORIGINS    CORS allow-list, comma list or *    (default *)
"""

import os
import re
import time
import threading
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask, jsonify, request

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    _ET = timezone(timedelta(hours=-5))

VERSION = "2.0.0"
app = Flask(__name__)

FMP_KEY = os.environ.get("FMP_API_KEY", "")
BASE = "https://financialmodelingprep.com/stable"
FUND_TTL = int(os.environ.get("FUND_TTL", "43200"))
QUOTE_TTL = int(os.environ.get("QUOTE_TTL", "300"))
QUOTE_TTL_CLOSED = int(os.environ.get("QUOTE_TTL_CLOSED", "21600"))
HIST_TTL = int(os.environ.get("HIST_TTL", "43200"))
DAILY_BUDGET = int(os.environ.get("FMP_DAILY_BUDGET", "240"))
FREE_UNIVERSE = [s.strip().upper() for s in os.environ.get(
    "FREE_UNIVERSE", "WMT,AAPL,MSFT,KO,NKE,XOM,DIS,BAC,JPM").split(",") if s.strip()]
ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "*").split(",") if o.strip()]
MM = 1e6
SYM_RE = re.compile(r"^[A-Z0-9.\-^]{1,15}$")

# ---------------------------------------------------------------- caches
_lock = threading.Lock()
_cache = {}          # key -> (timestamp, payload)
_budget = {"day": None, "used": 0}
_unavailable = set()  # FMP endpoints our plan rejects with 402/403 — stop spending calls on them


def _cache_get(key, ttl):
    with _lock:
        hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1], True
    return (hit[1] if hit else None), False


def _cache_put(key, payload):
    with _lock:
        _cache[key] = (time.time(), payload)


# ---------------------------------------------------------------- errors
class ProviderError(Exception):
    """Sanitized upstream error. Never carries the URL (it contains the key)."""

    def __init__(self, kind, status):
        super().__init__(kind)
        self.kind, self.status = kind, status


def _spend():
    today = datetime.now(timezone.utc).date().isoformat()
    with _lock:
        if _budget["day"] != today:
            _budget.update(day=today, used=0)
        if _budget["used"] >= DAILY_BUDGET:
            raise ProviderError("rate_limited", 429)
        _budget["used"] += 1


def _get(endpoint, **params):
    """One FMP GET. Maps provider failures to ProviderError; never leaks the key."""
    if endpoint in _unavailable:
        raise ProviderError("locked", 403)
    _spend()
    try:
        r = requests.get(f"{BASE}/{endpoint}", params={**params, "apikey": FMP_KEY}, timeout=10)
    except requests.RequestException:
        raise ProviderError("upstream", 502) from None
    if r.status_code in (401,):
        raise ProviderError("upstream", 502)       # bad key: our problem, not the user's
    if r.status_code in (402, 403):
        raise ProviderError("locked", 403)
    if r.status_code == 429:
        raise ProviderError("rate_limited", 429)
    if r.status_code == 404:
        raise ProviderError("not_found", 404)
    if r.status_code != 200:
        raise ProviderError("upstream", 502)
    try:
        data = r.json()
    except ValueError:
        raise ProviderError("upstream", 502) from None
    # FMP sometimes answers 200 with {"Error Message": "...premium..."}
    if isinstance(data, dict) and ("Error Message" in data or "error" in data):
        msg = str(data.get("Error Message") or data.get("error")).lower()
        if "premium" in msg or "subscription" in msg or "plan" in msg or "restricted" in msg:
            raise ProviderError("locked", 403)
        if "limit" in msg:
            raise ProviderError("rate_limited", 429)
        raise ProviderError("upstream", 502)
    if not data:
        raise ProviderError("not_found", 404)
    return data


def _optional(endpoint, remember=False, **params):
    """Best-effort call: returns None instead of raising. With remember=True, a plan-locked
    endpoint is skipped from then on (saves quota on endpoints our tier will never serve)."""
    try:
        return _get(endpoint, **params)
    except ProviderError as e:
        if e.kind == "rate_limited":
            raise  # never cache a degraded bundle just because quota ran out
        if remember and e.kind == "locked":
            _unavailable.add(endpoint)
        return None


def _first(data):
    if isinstance(data, list):
        return data[0] if data else {}
    return data or {}


def _num(x):
    try:
        v = float(x)
        return v if v == v else None  # drop NaN
    except (TypeError, ValueError):
        return None


def _clean_sym(sym):
    s = (sym or "").strip().upper()
    if not SYM_RE.match(s):
        raise ProviderError("not_found", 404)
    return s


# ---------------------------------------------------------------- market clock
def market_open(now=None):
    """Regular US session, Mon–Fri 09:30–16:00 ET. Holidays ignored (costs a few calls, harmless)."""
    t = (now or datetime.now(timezone.utc)).astimezone(_ET)
    if t.weekday() >= 5:
        return False
    mins = t.hour * 60 + t.minute
    return 9 * 60 + 30 <= mins < 16 * 60


# ---------------------------------------------------------------- quote
def get_quote(sym):
    ttl = QUOTE_TTL if market_open() else QUOTE_TTL_CLOSED
    cached, fresh = _cache_get(("q", sym), ttl)
    if fresh:
        return {**cached, "_cached": True}
    try:
        q = _first(_get("quote", symbol=sym))
    except ProviderError:
        if cached:
            return {**cached, "_cached": True, "_stale": True}
        raise
    price = _num(q.get("price"))
    if price is None:
        if cached:
            return {**cached, "_cached": True, "_stale": True}
        raise ProviderError("not_found", 404)
    prev = _num(q.get("previousClose"))
    payload = {
        "ticker": sym,
        "name": q.get("name"),
        "exchange": q.get("exchange"),
        "price": price,
        "prevClose": prev,
        "changePct": (price / prev - 1) * 100 if prev else _num(q.get("changePercentage")),
        "marketCapM": (_num(q.get("marketCap")) or 0) / MM or None,
        "quoteTs": q.get("timestamp") or int(time.time()),
    }
    _cache_put(("q", sym), payload)
    return {**payload, "_cached": False}


# ---------------------------------------------------------------- fundamentals
FIN_INDUSTRY = re.compile(
    r"^(banks|insurance|financial - (capital markets|mortgages|credit services|conglomerates))", re.I)


def _cagr(series):
    """series newest-first; CAGR over the span, None if not computable (sign change / zero)."""
    vals = [v for v in series if v is not None]
    if len(vals) < 3:
        return None
    new, old, n = vals[0], vals[-1], len(vals) - 1
    if new <= 0 or old <= 0:
        return None
    return ((new / old) ** (1 / n) - 1) * 100


def fetch_fundamentals(sym):
    """Everything except the live quote. ~4-6 FMP calls on a cold ticker, then cached FUND_TTL."""
    src = {}

    prof = _first(_get("profile", symbol=sym))  # hard requirement: 404/403 propagate from here

    inc_a = _optional("income-statement", symbol=sym, limit=5) or \
        _optional("income-statement", symbol=sym, limit=1) or []
    cf_a = _optional("cash-flow-statement", symbol=sym, limit=5) or \
        _optional("cash-flow-statement", symbol=sym, limit=1) or []
    bal = _first(_optional("balance-sheet-statement", symbol=sym, limit=1) or [])
    if not inc_a and not cf_a:
        # profile exists but no statements on this plan -> treat as plan-locked
        raise ProviderError("locked", 403)
    inc_a = inc_a if isinstance(inc_a, list) else [inc_a]
    cf_a = cf_a if isinstance(cf_a, list) else [cf_a]
    inc, cf = (inc_a[0] if inc_a else {}), (cf_a[0] if cf_a else {})

    inc_ttm = _first(_optional("income-statement-ttm", remember=True, symbol=sym) or {})
    cf_ttm = _first(_optional("cash-flow-statement-ttm", remember=True, symbol=sym) or {})

    # EPS — prefer TTM, fall back to last fiscal year; label which one we used
    eps = _num(inc_ttm.get("epsDiluted")) or _num(inc_ttm.get("eps"))
    if eps is not None:
        eps_basis, src["epsTTM"] = "TTM", "income-statement-ttm.epsDiluted"
    else:
        eps = _num(inc.get("epsDiluted")) or _num(inc.get("eps"))
        eps_basis, src["epsTTM"] = "FY", f"income-statement.epsDiluted (FY {inc.get('date')})"

    # FCF — same rule
    fcf = _num(cf_ttm.get("freeCashFlow"))
    if fcf is not None:
        fcf_basis, src["fcf0M"] = "TTM", "cash-flow-statement-ttm.freeCashFlow"
    else:
        fcf = _num(cf.get("freeCashFlow"))
        fcf_basis, src["fcf0M"] = "FY", f"cash-flow-statement.freeCashFlow (FY {cf.get('date')})"

    ebitda = _num(inc_ttm.get("ebitda"))
    if ebitda is not None:
        src["ebitdaM"] = "income-statement-ttm.ebitda"
    else:
        ebitda = _num(inc.get("ebitda"))
        src["ebitdaM"] = f"income-statement.ebitda (FY {inc.get('date')})"

    debt = _num(bal.get("totalDebt")) or 0.0
    cash = _num(bal.get("cashAndShortTermInvestments")) or _num(bal.get("cashAndCashEquivalents")) or 0.0
    src["netDebtM"] = f"balance-sheet.totalDebt - cashAndShortTermInvestments ({bal.get('date')})"

    sector, industry = prof.get("sector") or "", prof.get("industry") or ""
    fin = sector == "Financial Services" and bool(FIN_INDUSTRY.match(industry))

    return {
        "ticker": sym,
        "companyName": prof.get("companyName") or inc.get("symbol"),
        "currency": prof.get("currency") or inc.get("reportedCurrency") or "USD",
        "reportedCurrency": inc.get("reportedCurrency"),
        "exchange": prof.get("exchange") or prof.get("exchangeShortName"),
        "country": prof.get("country"),
        "sector": sector or None,
        "industry": industry or None,
        "isEtf": bool(prof.get("isEtf")),
        "fin": fin,
        "asOf": inc.get("date"),
        "balanceAsOf": bal.get("date"),
        "epsTTM": eps,
        "epsBasis": eps_basis,
        "fcf0M": fcf / MM if fcf is not None else None,
        "fcfBasis": fcf_basis,
        "ebitdaM": ebitda / MM if ebitda is not None else None,
        "netDebtM": (debt - cash) / MM,
        "_fyWeightedSharesM": (_num(inc.get("weightedAverageShsOutDil"))
                               or _num(inc.get("weightedAverageShsOut")) or 0) / MM or None,
        # reference only — the app's stage-1 growth is an analyst assumption, not a feed
        "hist": {
            "years": max(len(inc_a), len(cf_a)),
            "revenueCagr": _cagr([_num(r.get("revenue")) for r in inc_a]),
            "fcfCagr": _cagr([_num(r.get("freeCashFlow")) for r in cf_a]),
        },
        "_sources": src,
    }


def get_inputs(sym):
    cached, fresh = _cache_get(("f", sym), FUND_TTL)
    stale = False
    if fresh:
        fund = cached
    else:
        try:
            fund = fetch_fundamentals(sym)
            _cache_put(("f", sym), fund)
        except ProviderError:
            if not cached:
                raise
            fund, stale = cached, True

    q = get_quote(sym)  # ProviderError propagates with its real kind
    stale = stale or bool(q.get("_stale"))

    price = q["price"]
    src = dict(fund["_sources"])
    src["price"] = "quote.price"
    # current share count: market cap / price (reflects buybacks), else FY weighted diluted
    if q.get("marketCapM") and price:
        shares = q["marketCapM"] / price
        src["sharesM"] = "quote.marketCap / quote.price"
    else:
        shares = fund.get("_fyWeightedSharesM")
        src["sharesM"] = f"income-statement.weightedAverageShsOutDil (FY {fund.get('asOf')})"

    out = {k: v for k, v in fund.items() if not k.startswith("_")}
    out.update({
        "price": price,
        "prevClose": q.get("prevClose"),
        "changePct": q.get("changePct"),
        "sharesM": shares,
        "companyName": fund.get("companyName") or q.get("name"),
        "_sources": src,
        "_cached": bool(fresh and q.get("_cached")),
    })
    if stale:
        out["_stale"] = True
    return out


# ---------------------------------------------------------------- history
def get_history(sym, months):
    key = ("h", sym, months)
    cached, fresh = _cache_get(key, HIST_TTL)
    if fresh:
        return cached
    start = (datetime.now(timezone.utc) - timedelta(days=int(months * 30.5) + 3)).date().isoformat()
    try:
        data = _get("historical-price-eod/light", symbol=sym, **{"from": start})
    except ProviderError:
        if cached:
            return {**cached, "_stale": True}
        raise
    rows = data if isinstance(data, list) else data.get("historical", [])
    pts = []
    for r in rows:
        c = _num(r.get("price")) if r.get("price") is not None else _num(r.get("close"))
        if r.get("date") and c is not None:
            pts.append([r["date"], c])
    pts.sort(key=lambda p: p[0])  # oldest -> newest
    if not pts:
        raise ProviderError("not_found", 404)
    payload = {"ticker": sym, "months": months, "points": pts}
    _cache_put(key, payload)
    return payload


# ---------------------------------------------------------------- http layer
def _err(e):
    return jsonify(error=e.kind), e.status


@app.after_request
def _cors(resp):
    origin = request.headers.get("Origin")
    if "*" in ALLOWED_ORIGINS:
        resp.headers["Access-Control-Allow-Origin"] = "*"
    elif origin and origin in ALLOWED_ORIGINS:
        resp.headers["Access-Control-Allow-Origin"] = origin
        resp.headers["Vary"] = "Origin"
    resp.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


def _guard():
    if not FMP_KEY:
        return (jsonify(error="server missing FMP_API_KEY"), 503)
    return None


@app.route("/health")
def health():
    return jsonify(ok=True, version=VERSION, hasKey=bool(FMP_KEY), marketOpen=market_open(),
                   callsToday=_budget["used"] if _budget["day"] else 0, dailyBudget=DAILY_BUDGET,
                   planLockedEndpoints=sorted(_unavailable), cached=len(_cache))


@app.route("/quote/<sym>")
def quote(sym):
    if (g := _guard()):
        return g
    try:
        return jsonify(get_quote(_clean_sym(sym)))
    except ProviderError as e:
        return _err(e)


@app.route("/quotes")
def quotes():
    if (g := _guard()):
        return g
    syms = [s for s in (request.args.get("symbols") or "").upper().split(",") if s][:25]
    out, errors = {}, {}
    for s in syms:
        try:
            out[s] = get_quote(_clean_sym(s))
        except ProviderError as e:
            errors[s] = e.kind
    return jsonify(quotes=out, errors=errors)


@app.route("/inputs/<sym>")
def inputs(sym):
    if (g := _guard()):
        return g
    try:
        return jsonify(get_inputs(_clean_sym(sym)))
    except ProviderError as e:
        return jsonify(error=e.kind, ticker=sym.upper()), e.status


@app.route("/universe")
def universe():
    if (g := _guard()):
        return g
    out, errors = {}, {}
    for s in FREE_UNIVERSE:
        try:
            out[s] = get_inputs(s)
        except ProviderError as e:
            errors[s] = e.kind
    return jsonify(tickers=FREE_UNIVERSE, data=out, errors=errors, marketOpen=market_open())


@app.route("/history/<sym>")
def history(sym):
    if (g := _guard()):
        return g
    try:
        months = max(1, min(24, int(request.args.get("months", "6"))))
    except ValueError:
        months = 6
    try:
        return jsonify(get_history(_clean_sym(sym), months))
    except ProviderError as e:
        return _err(e)


@app.route("/search/<query>")
def search(query):
    """Ticker/company-name search for autocomplete. Returns up to 8 matches."""
    if (g := _guard()):
        return g
    q = query.strip()[:40]
    if not q:
        return jsonify(results=[])
    cached, fresh = _cache_get(("s", q.lower()), FUND_TTL)
    if fresh:
        return jsonify(results=cached)
    try:
        data = _optional("search-symbol", query=q, limit=8) or _optional("search-name", query=q, limit=8)
    except ProviderError as e:
        return jsonify(error=e.kind, results=[]), e.status
    if data is None:
        return jsonify(error="upstream", results=[]), 502
    results = [
        {"symbol": d.get("symbol"), "name": d.get("name"),
         "exchange": d.get("exchangeFullName") or d.get("exchange"), "currency": d.get("currency")}
        for d in (data if isinstance(data, list) else [])
    ]
    _cache_put(("s", q.lower()), results)
    return jsonify(results=results)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
