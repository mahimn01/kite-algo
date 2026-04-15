## Summary

<!-- 1–3 bullet points. What does this PR do, and why? -->

## Changes

<!-- Files touched, commands added, behavior changes. Be specific. -->

## Test plan

<!-- How was this verified? Check off what applies. -->

- [ ] `pytest` passes locally
- [ ] Manual smoke test against live Kite session (`kite_tool profile` + one affected command)
- [ ] `SimBroker` sim path tested if broker logic changed
- [ ] `.env.example` updated if env vars changed
- [ ] `CHANGELOG.md` updated

## Kite-specific checks

- [ ] Respects Kite rate limits (3 req/s most endpoints, 10 req/s for /quote)
- [ ] No hardcoded session tokens or api secrets
- [ ] Daily token rotation still works (no assumptions of persistent session)
- [ ] Market hours respected (NSE/BSE/NFO/MCX/CDS)

## Risk

<!-- Anything the reviewer should watch for: side effects on live trading,
     order routing, product type (CNC/MIS/NRML), margin requirements. -->

## Notes for reviewer
