# Hosted reviewer inference gateway

These are the deployed request handler, policy and database schema. The Mac app does not run this server locally. Deployment uses a Cloudflare Worker through Sites with a D1 binding named `DB`, plus secret runtime values `META_API_KEY` and `TRIAL_TOKEN_SHA256` (SHA-256 of the reviewer invitation).

A Worker fetch entry point calls `gateway(request, env, ctx)` and returns its non-null response; other routes may serve a status page. Apply `schema.sql` through migrations before accepting requests. Never seed or reset `budget.used` during redeployment. Existing reservations and spending must persist. `policy.mjs` pins the allowance, expiry, allowed models and cost bounds; do not raise them without the owner's authorization and updated price checks.

The hosted source revision is `41c82ddcea6481ac69d127192be148bd6e7d406e`. See [reviewer trial documentation](../../docs/reviewer-trial.md) for data flow, limits and verification. The invitation is deliberately distributed outside this public repository.

Run the policy and ledger checks with Node.js 24 (SQLite and type stripping):

```sh
node --experimental-strip-types --test services/reviewer-gateway/gateway.test.mjs
```

The tests use an in-memory SQLite database and mocked upstream calls; they incur no Meta cost. Hosted smoke checks were run separately.
