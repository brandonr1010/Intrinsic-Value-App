"""
SEC EDGAR fundamentals for Intrinsic.

Free, primary-source, public-domain data (10-K / 10-Q / 20-F / 40-F XBRL), so it is
legal to display in the app. Two SEC calls per cold ticker (companyfacts + submissions),
cached by the caller.

Rules
- Flow items (revenue, EPS, operating cash flow, capex) are TTM when a 10-Q exists after
  the last annual report:  TTM = FY + current YTD - prior-year YTD.  Otherwise FY.
- Balance items (debt, cash) use the latest reported instant.
- Shares = cover-page shares outstanding (dei), summed across share classes.
- Every figure records the XBRL tag and period it came from.
SEC fair-access: <=10 req/s and a descriptive User-Agent (env SEC_USER_AGENT).
"""

import os
import threading
import time
from datetime import date

import requests

UA = os.environ.get("SEC_USER_AGENT", "Intrinsic valuation app (github.com/brandonr1010/Intrinsic-Value-App)")
HDR = {"User-Agent": UA, "Accept-Encoding": "gzip, deflate"}
MM = 1e6

_lock = threading.Lock()
_last_call = [0.0]
_tickers = {"ts": 0.0, "map": {}, "rows": []}


class EdgarError(Exception):
    def __init__(self, kind, status):
        super().__init__(kind)
        self.kind, self.status = kind, status


def _sec_get(url):
    with _lock:  # polite pacing: max ~8 req/s from this process
        wait = 0.125 - (time.time() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.time()
    try:
        r = requests.get(url, headers=HDR, timeout=15)
    except requests.RequestException:
        raise EdgarError("upstream", 502) from None
    if r.status_code == 404:
        raise EdgarError("not_found", 404)
    if r.status_code == 429:
        raise EdgarError("rate_limited", 429)
    if r.status_code != 200:
        raise EdgarError("upstream", 502)
    try:
        return r.json()
    except ValueError:
        raise EdgarError("upstream", 502) from None


# ------------------------------------------------------------------ ticker map
def ticker_map():
    """{TICKER: {cik, name, exchange}} from SEC, refreshed daily."""
    if _tickers["map"] and time.time() - _tickers["ts"] < 86400:
        return _tickers["map"]
    data = _sec_get("https://www.sec.gov/files/company_tickers_exchange.json")
    fields = data.get("fields", [])
    m, rows = {}, []
    for row in data.get("data", []):
        d = dict(zip(fields, row))
        t = str(d.get("ticker", "")).upper()
        if not t:
            continue
        rec = {"cik": int(d["cik"]), "name": d.get("name"), "exchange": d.get("exchange"), "ticker": t}
        m.setdefault(t, rec)  # first row wins (SEC lists primary class first)
        rows.append(rec)
    _tickers.update(ts=time.time(), map=m, rows=rows)
    return m


def search(q, limit=8):
    """Local search over SEC's ticker list: exact ticker, ticker prefix, then name match."""
    ticker_map()
    q = q.strip().upper()
    if not q:
        return []
    exact, prefix, name = [], [], []
    for r in _tickers["rows"]:
        t, n = r["ticker"], (r["name"] or "").upper()
        if t == q:
            exact.append(r)
        elif t.startswith(q):
            prefix.append(r)
        elif q in n:
            name.append(r)
    prefix.sort(key=lambda r: len(r["ticker"]))
    name.sort(key=lambda r: (not (r["name"] or "").upper().startswith(q), len(r["ticker"])))
    seen, out = set(), []
    for r in exact + prefix + name:
        if r["cik"] in seen:
            continue
        seen.add(r["cik"])
        out.append({"symbol": r["ticker"], "name": r["name"], "exchange": r["exchange"], "currency": "USD"})
        if len(out) >= limit:
            break
    return out


# ------------------------------------------------------------------ XBRL helpers
ANNUAL_FORMS = {"10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A", "10-KT"}


def _d(s):
    return date.fromisoformat(s)


def _days(f):
    return (_d(f["end"]) - _d(f["start"])).days if f.get("start") else 0


def _facts(cf, concept, units=("USD",)):
    """Return (taxonomy, unit, deduped fact list) for the first taxonomy that has the concept."""
    for tax in ("us-gaap", "ifrs-full"):
        node = cf.get("facts", {}).get(tax, {}).get(concept)
        if not node:
            continue
        for unit, arr in node.get("units", {}).items():
            if unit in units or (units == ("USD",) and len(unit) == 3 and unit.isupper()) \
               or (units == ("USD/shares",) and unit.endswith("/shares")):
                best = {}
                for f in arr:
                    k = (f.get("start"), f["end"])
                    if k not in best or f.get("filed", "") > best[k].get("filed", ""):
                        best[k] = f
                return tax, unit, list(best.values())
    return None, None, []


def flow(cf, concepts, units=("USD",)):
    """TTM (or FY) value for a duration concept. Returns (value, basis, tag, unit, period_end)."""
    for c in concepts:
        tax, unit, facts = _facts(cf, c, units)
        if not facts:
            continue
        annual = [f for f in facts if f.get("start") and 330 <= _days(f) <= 380
                  and f.get("form") in ANNUAL_FORMS]
        if not annual:
            annual = [f for f in facts if f.get("start") and 330 <= _days(f) <= 380]
        if not annual:
            continue
        A = max(annual, key=lambda f: f["end"])
        a_end = _d(A["end"])
        ytd = [f for f in facts if f.get("start") and 60 <= _days(f) < 330
               and _d(f["end"]) > a_end and 0 < (_d(f["start"]) - a_end).days <= 8]
        if ytd:
            Y = max(ytd, key=lambda f: f["end"])
            y_end, y_len = _d(Y["end"]), _days(Y)
            prior = [f for f in facts if f.get("start")
                     and abs((y_end - _d(f["end"])).days - 365) <= 10
                     and abs(_days(f) - y_len) <= 10]
            if prior:
                P = max(prior, key=lambda f: f.get("filed", ""))
                return A["val"] + Y["val"] - P["val"], "TTM", f"{tax}:{c}", unit, Y["end"]
        return A["val"], "FY", f"{tax}:{c}", unit, A["end"]
    return None, None, None, None, None


def instant(cf, concepts, units=("USD",), not_before=None):
    """Latest point-in-time value. Returns (value, tag, unit, end)."""
    best = None
    for c in concepts:
        tax, unit, facts = _facts(cf, c, units)
        pts = [f for f in facts if not f.get("start")]
        if not pts:
            continue
        f = max(pts, key=lambda f: (f["end"], f.get("filed", "")))
        if best is None or f["end"] > best[3]:
            best = (f["val"], f"{tax}:{c}", unit, f["end"])
    if best and not_before and best[3] < not_before:
        return None, None, None, None
    return best or (None, None, None, None)


def shares_outstanding(cf):
    """Cover-page shares, summed across classes reported for the same date."""
    node = cf.get("facts", {}).get("dei", {}).get("EntityCommonStockSharesOutstanding")
    if node:
        arr = node.get("units", {}).get("shares", [])
        if arr:
            latest = max(f["end"] for f in arr)
            by_accn = {}
            for f in arr:
                if f["end"] == latest:
                    by_accn.setdefault(f.get("accn"), []).append(f["val"])
            vals = max(by_accn.values(), key=len)
            return float(sum(vals)), "dei:EntityCommonStockSharesOutstanding", latest
    v, basis, tag, _, end = flow(cf, ["WeightedAverageNumberOfDilutedSharesOutstanding",
                                      "WeightedAverageNumberOfSharesOutstandingBasicAndDiluted"],
                                 units=("shares",))
    return v, tag, end


# SIC codes where a cash-flow DCF is meaningless: banks, thrifts, credit, brokers, insurers
def is_fin_sic(sic):
    try:
        s = int(sic)
    except (TypeError, ValueError):
        return False
    return 6020 <= s <= 6099 or 6140 <= s <= 6199 or 6200 <= s <= 6211 or 6311 <= s <= 6411


REVENUE = ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
           "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet",
           "RevenuesNetOfInterestExpense", "Revenue"]
EPS_DIL = ["EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted",
           "DilutedEarningsLossPerShare", "BasicAndDilutedEarningsLossPerShare"]
CFO = ["NetCashProvidedByUsedInOperatingActivities",
       "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
       "CashFlowsFromUsedInOperatingActivities"]
CAPEX = ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets",
         "PaymentsForCapitalImprovements",
         "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
         "PurchaseOfPropertyPlantAndEquipment"]
OPINC = ["OperatingIncomeLoss", "ProfitLossFromOperatingActivities"]
DA = ["DepreciationDepletionAndAmortization", "DepreciationAndAmortization",
      "DepreciationAmortizationAndAccretionNet", "Depreciation",
      "DepreciationAndAmortisationExpense", "DepreciationPropertyPlantAndEquipment"]
CASH = ["CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents", "CashAndCashEquivalents"]
STI = ["ShortTermInvestments", "MarketableSecuritiesCurrent", "AvailableForSaleSecuritiesDebtSecuritiesCurrent",
       "CurrentFinancialAssetsAtFairValueThroughProfitOrLoss"]
DEBT_TOTAL = ["LongTermDebt", "DebtLongtermAndShorttermCombinedAmount", "Borrowings"]
DEBT_NONCUR = ["LongTermDebtNoncurrent", "LongTermDebtAndCapitalLeaseObligations",
               "NoncurrentPortionOfNoncurrentBorrowings", "NoncurrentBorrowings"]
DEBT_CUR = ["LongTermDebtCurrent", "LongTermDebtAndCapitalLeaseObligationsCurrent",
            "CurrentPortionOfNoncurrentBorrowings", "CurrentBorrowings"]
DEBT_ST = ["ShortTermBorrowings", "CommercialPaper", "OtherShortTermBorrowings"]


def fundamentals(sym):
    """Normalized bundle in the same shape app.py's FMP path produces."""
    t = sym.upper()
    rec = ticker_map().get(t) or ticker_map().get(t.replace(".", "-"))
    if not rec:
        raise EdgarError("locked", 403)  # not an SEC filer -> outside our coverage
    cik = f"{rec['cik']:010d}"
    cf = _sec_get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json")
    sub = _sec_get(f"https://data.sec.gov/submissions/CIK{cik}.json")
    src = {}

    rev, rev_b, rev_tag, ccy, rev_end = flow(cf, REVENUE)
    eps, eps_b, eps_tag, _, eps_end = flow(cf, EPS_DIL, units=("USD/shares",))
    cfo, cfo_b, cfo_tag, _, cfo_end = flow(cf, CFO)
    capex, cap_b, cap_tag, _, _ = flow(cf, CAPEX)
    if eps is not None:
        src["epsTTM"] = f"{eps_tag} ({eps_b} to {eps_end})"
    fcf = None
    if cfo is not None:
        fcf = cfo - (capex or 0)
        src["fcf0M"] = f"{cfo_tag} - {cap_tag or 'no capex tag'} ({cfo_b} to {cfo_end})"
    fcf_b = cfo_b if cfo is not None and (capex is None or cap_b == cfo_b) else ("FY" if cfo is not None else None)

    op, _, op_tag, _, _ = flow(cf, OPINC)
    da, _, da_tag, _, _ = flow(cf, DA)
    ebitda = op + da if op is not None and da is not None else None
    if ebitda is not None:
        src["ebitdaM"] = f"{op_tag} + {da_tag}"

    cash, cash_tag, _, cash_end = instant(cf, CASH)
    sti, sti_tag, _, _ = instant(cf, STI, not_before=cash_end)
    debt, debt_tag, _, debt_end = instant(cf, DEBT_TOTAL, not_before=cash_end)
    if debt is None:
        nc, nc_tag, _, _ = instant(cf, DEBT_NONCUR, not_before=cash_end)
        cur, cur_tag, _, _ = instant(cf, DEBT_CUR, not_before=cash_end)
        if nc is not None or cur is not None:
            debt = (nc or 0) + (cur or 0)
            debt_tag = " + ".join(x for x in (nc_tag, cur_tag) if x)
    st, st_tag, _, _ = instant(cf, DEBT_ST, not_before=cash_end)
    total_debt = (debt or 0) + (st or 0)
    total_cash = (cash or 0) + (sti or 0)
    src["netDebtM"] = (f"({debt_tag or 'no debt tag'}{' + ' + st_tag if st_tag else ''}) - "
                       f"({cash_tag or 'no cash tag'}{' + ' + sti_tag if sti_tag else ''}) at {cash_end}")

    shares, sh_tag, sh_end = shares_outstanding(cf)
    if shares:
        src["sharesM"] = f"{sh_tag} ({sh_end})"

    # 5-year revenue history for the growth reference
    _, _, rfacts = (None, None, [])
    for c in REVENUE:
        _, _, rfacts = _facts(cf, c)
        if rfacts:
            break
    annual_rev = sorted([f for f in rfacts if f.get("start") and 330 <= _days(f) <= 380],
                        key=lambda f: f["end"], reverse=True)
    yearly, seen = [], set()
    for f in annual_rev:
        y = f["end"][:4]
        if y not in seen:
            seen.add(y)
            yearly.append(f["val"])
        if len(yearly) == 5:
            break

    sic = sub.get("sic")
    return {
        "ticker": t,
        "cik": rec["cik"],
        "companyName": sub.get("name") or rec["name"],
        "currency": "USD",
        "reportedCurrency": ccy,
        "exchange": rec.get("exchange"),
        "country": (sub.get("addresses", {}).get("business", {}) or {}).get("stateOrCountryDescription"),
        "sector": sub.get("sicDescription"),
        "industry": sub.get("sicDescription"),
        "sic": sic,
        "isEtf": False,
        "fin": is_fin_sic(sic),
        "asOf": eps_end or cfo_end or rev_end,
        "balanceAsOf": cash_end,
        "epsTTM": eps,
        "epsBasis": eps_b,
        "fcf0M": fcf / MM if fcf is not None else None,
        "fcfBasis": fcf_b,
        "revenueM": rev / MM if rev is not None else None,
        "ebitdaM": ebitda / MM if ebitda is not None else None,
        "netDebtM": (total_debt - total_cash) / MM,
        "_edgarSharesM": shares / MM if shares else None,
        "_fyWeightedSharesM": None,
        "hist": {"years": len(yearly), "revenueCagr": _cagr(yearly), "fcfCagr": None},
        "_sources": {k: "SEC EDGAR " + v for k, v in src.items()},
        "_source": "edgar",
    }


def _cagr(vals):
    vals = [v for v in vals if v is not None]
    if len(vals) < 3 or vals[0] <= 0 or vals[-1] <= 0:
        return None
    return ((vals[0] / vals[-1]) ** (1 / (len(vals) - 1)) - 1) * 100
