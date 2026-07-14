"""
Intrinsic Value — backend proxy (Flask, deploy on Railway)
------------------------------------------------------------
Purpose: the mobile app NEVER holds the data-API key. It calls THIS
service, which calls FMP, normalizes the fields the valuation model
needs, and returns clean JSON.

Endpoints:
  GET /health            -> liveness check
  GET /inputs/<ticker>   -> normalized valuation inputs for one ticker

Env vars (set in Railway):
  FMP_API_KEY   your Financial Modeling Prep key
  CACHE_TTL     seconds to cache a ticker (default 43200 = 12h)

STATUS: logic-complete, UNTESTED against live FMP (no key/network here).
Verify field names against your FMP plan before shipping — Uses FMP /stable API (v3 legacy returns 403 for new accounts).
"""

import os
import time
import requests
from flask import Flask, jsonify

app = Flask(__name__)

FMP_KEY = os.environ.get("FMP_API_KEY", "")
BASE = "https://financialmodelingprep.com/stable"
CACHE_TTL = int(os.environ.get("CACHE_TTL", "43200"))
_cache = {}  # {ticker: (timestamp, payload)}


def _get(path):
    """One FMP GET (stable API). Raises on non-200 or empty."""
    url = f"{BASE}/{path}"
    sep = "&" if "?" in path else "?"
    r = requests.get(f"{url}{sep}apikey={FMP_KEY}", timeout=10)
    r.raise_for_status()
    data = r.json()
    if not data:
        raise ValueError(f"empty response for {path}")
    return data


def _first(data):
    return data[0] if isinstance(data, list) else data


def fetch_inputs(ticker):
    """Assemble the normalized input bundle the DCF/relative model needs.
    Every returned figure is tagged with its FMP source field so the app
    (and you) can trace it. Values in millions where noted."""
    t = ticker.upper()

    quote = _first(_get(f"quote?symbol={t}"))                       # price, eps, shares, mktCap
    income = _first(_get(f"income-statement?symbol={t}&limit=1"))   # ebitda, eps
    cash = _first(_get(f"cash-flow-statement?symbol={t}&limit=1"))  # freeCashFlow
    bal = _first(_get(f"balance-sheet-statement?symbol={t}&limit=1"))  # debt, cash

    total_debt = bal.get("totalDebt") or 0
    cash_sti = (bal.get("cashAndShortTermInvestments")
                or bal.get("cashAndCashEquivalents") or 0)
    net_debt = total_debt - cash_sti

    MM = 1e6  # convert absolute dollars -> $M to match the model's scale
    # stable /quote trimmed some fields vs legacy v3 — fall back to income stmt
    shares = (quote.get("sharesOutstanding")
              or income.get("weightedAverageShsOutDil")
              or income.get("weightedAverageShsOut") or 0)
    eps = quote.get("eps") or income.get("epsDiluted") or income.get("eps")
    return {
        "ticker": t,
        "companyName": quote.get("name"),
        "asOf": income.get("date"),
        "currency": income.get("reportedCurrency", "USD"),
        # --- price & share data (source: /quote, fallback income stmt) ---
        "price": quote.get("price"),
        "sharesM": shares / MM,
        "epsTTM": eps,
        # --- DCF drivers ---
        "fcf0M": (cash.get("freeCashFlow") or 0) / MM,       # source: cash-flow.freeCashFlow
        "ebitdaM": (income.get("ebitda") or 0) / MM,          # source: income.ebitda
        "netDebtM": net_debt / MM,                            # totalDebt - cash&STI
        "_sources": {
            "price": "quote.price",
            "sharesM": "quote.sharesOutstanding",
            "epsTTM": "quote.eps",
            "fcf0M": "cash-flow.freeCashFlow",
            "ebitdaM": "income.ebitda",
            "netDebtM": "balance.totalDebt - balance.cashAndShortTermInvestments",
        },
    }


@app.route("/health")
def health():
    return jsonify(ok=True, hasKey=bool(FMP_KEY))


@app.route("/search/<query>")
def search(query):
    """Ticker/company-name search for autocomplete. Returns up to 8 matches."""
    if not FMP_KEY:
        return jsonify(error="server missing FMP_API_KEY"), 503
    q = query.strip()
    if len(q) < 1:
        return jsonify(results=[])
    try:
        data = _get(f"search-symbol?query={q}&limit=8")
    except Exception:
        # fall back to name search if symbol search fails/empty
        try:
            data = _get(f"search-name?query={q}&limit=8")
        except Exception as e:
            return jsonify(error=str(e), results=[]), 502
    results = [
        {
            "symbol": d.get("symbol"),
            "name": d.get("name"),
            "exchange": d.get("exchangeFullName") or d.get("exchange"),
            "currency": d.get("currency"),
        }
        for d in (data if isinstance(data, list) else [])
    ]
    return jsonify(results=results)


@app.route("/inputs/<ticker>")
def inputs(ticker):
    if not FMP_KEY:
        return jsonify(error="server missing FMP_API_KEY"), 503

    t = ticker.upper()
    hit = _cache.get(t)
    if hit and time.time() - hit[0] < CACHE_TTL:
        return jsonify({**hit[1], "_cached": True})

    try:
        payload = fetch_inputs(t)
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else 502
        return jsonify(error=f"data provider returned {code}", ticker=t), 502
    except Exception as e:
        return jsonify(error=str(e), ticker=t), 502

    _cache[t] = (time.time(), payload)
    return jsonify({**payload, "_cached": False})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
