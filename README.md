# Intrinsic Value — Data Proxy

Flask proxy for the Intrinsic Value app. Keeps the FMP API key server-side.

## Endpoints
- `GET /health` — liveness + key check
- `GET /inputs/<ticker>` — normalized valuation inputs (price, sharesM, epsTTM, fcf0M, ebitdaM, netDebtM)

## Deploy (Railway)
1. Deploy from this GitHub repo
2. Variables: set `FMP_API_KEY`
3. Settings → Networking → Generate Domain
4. Test: `/health` then `/inputs/BAC`

Never commit the API key.
