# Trading Agency Integrations — Safe Setup

The service is intentionally paper-only. The dashboard currently provides official documentation links and an integration-status view; broker execution adapters are **not connected**.

## Current capabilities
- Protected paper-order endpoint: `POST /paper/orders` using `X-API-Key`.
- Risk status endpoint: `GET /risk/status`.
- Public, non-secret integration status: `GET /api/integrations`.
- Optional NewsAPI business headlines endpoint: `GET /api/news`.
- PostgreSQL persistence via Neon when `DATABASE_URL` is configured.

## NewsAPI setup
1. Create an account and obtain an API key at https://newsapi.org/.
2. In Render, open the service Environment settings and add `NEWS_API_KEY` as a secret environment variable.
3. Redeploy and verify `GET /api/integrations` reports `newsapi.configured: true`.
4. Open the dashboard news section. The key stays on the server and is sent to NewsAPI in the `X-Api-Key` request header, not exposed to browser JavaScript or included in the query URL.
5. Review NewsAPI plan, geographic coverage, rate limits, caching, and redistribution terms before production use.

## Broker and platform integration roadmap
- **MetaTrader 5**: its Python package communicates with a locally installed MT5 terminal. A cloud-only Render service cannot attach to a user's desktop terminal without a separately secured bridge running on that device. Start with read-only market data and demo accounts.
- **Interactive Brokers**: use the official TWS API or Web API, with an authorized account and a running TWS/IB Gateway where applicable. Implement account/market-data scopes and paper account support first.
- **Alpaca**: use a paper account and paper endpoint credentials; do not mix paper and live credentials.
- **TradingView**: webhook alerts require a separately implemented HTTPS receiver, authentication/signature verification, replay protection, schema validation, rate limiting, and explicit paper-only routing.
- **QuantConnect LEAN / Backtrader / vectorbt**: use for reproducible research and backtesting. Keep data source, fees, spreads, slippage, financing, survivorship bias and out-of-sample splits explicit.

## Research policy
No strategy is guaranteed profitable. Every candidate must be pre-specified and evaluated using out-of-sample and walk-forward tests, realistic costs, drawdown and tail-risk metrics, sensitivity checks, and paper forward-testing. Avoid overfitting, look-ahead bias, and selecting strategies only because their historical results look attractive.

## Security requirements before any live trading
- Never store broker keys in GitHub or browser code.
- Use dedicated, least-privilege keys; prefer read-only keys until order routing is explicitly approved and tested.
- Add durable order-state reconciliation, broker-side idempotency, kill switch, alerting, audit trail, clock synchronization, rate limits, and tested recovery procedures.
- Do not activate live trading until an independent review and explicit user authorization.
