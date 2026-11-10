# Gabay Pricing — offline demo readiness

**Status: ready for a local, replayed-data demonstration; not an approved pricing recommendation.** The workflow below uses the preserved real market snapshot and previously saved AI review. It makes no new market or AI calls. Source artifacts and screenshots are private local files under the ignored, owner-only `.local-demo/` directory; do not redistribute provider data without permission. They are not part of a Git checkout or verified on another machine.

## Start the demo

From the repository root, on the machine holding the preserved artifacts:

```bash
cd poc
umask 077
export UV_OFFLINE=1
uv sync --locked
DEMO_DIR="$(cd ../.local-demo && pwd)"
uv run --locked python -m gabay_pricing.web --port 18765 \
  --artifacts "$DEMO_DIR/runs" \
  --offline-demo-snapshot "$DEMO_DIR/inputs/market.json" \
  --offline-demo-ai "$DEMO_DIR/ai/pricing_results.json" \
  --offline-demo-checkpoint "$DEMO_DIR/ai/AI_UNITS_DURABLE.json"
```

The offline sync requires Python 3.12 and all locked dependencies already installed or cached; it does not download them. Open <http://127.0.0.1:18765/>, leave **saved data** selected, upload `.local-demo/inputs/input.xlsx` from the repository root, leave apartment IDs empty and the AI checkbox unchecked, then select **הפקת דוח**. Offline demo mode rejects live collection and new AI requests. The saved AI is included only if snapshot, workbook, configuration, calculated results and EvidencePacks match; the provided checkpoint must also match the saved AI. A mismatch fails rather than silently presenting stale analysis.

The report labels the market data as replayed and the AI as saved. `calls=0` means **this offline regeneration only**. The complete historical call total is not recorded and is displayed as unknown. The actual provider/model and retry histories remain available; the verified checkpoint path is displayed separately from the saved report path. Analyst and Critic reuse are shown independently.

## Verified browser journey

On 2026-11-10, headless Google Chrome opened the local upload form, uploaded the preserved workbook, submitted the form and rendered the report in one uninterrupted flow. Browser requests were restricted to localhost; none attempted an external destination. The offline server path used deterministic pricing and saved AI only. The resulting report is:

`.local-demo/runs/run-v38yezua/report/pricing_results.json` (generated from the persistent copies using the command above). The original verified browser report is preserved separately in `.local-demo/results/verified-report/`; its embedded provenance retains the original `/tmp` paths.

- Hebrew RTL dashboard: **39 apartment rows**; all three pricing scenarios are present for each unit (a scenario can be unavailable or indicative, not a recommendation).
- Evidence details include original transaction provenance and limitations. The dashboard shows approval/review restrictions and the unverified location assumption.
- Apartment 4: saved Gemini Analyst **VALIDATED** and Gemini Critic **SUCCESS**, both `gemini-3.1-flash-lite`; recorded fallback history includes Groq quota exhaustion. The review has **6 SUPPORTED** and **2 INSUFFICIENT_EVIDENCE** verdicts. The two unsupported strategy claims remain visible; the interpretation is withheld. This is not all-claims-supported.
- Checkpoint and saved report were compared before reuse; the resulting report records their paths and stage-specific reuse.
- The full deterministic pipeline covered 39 units. The complete 39-unit live AI workflow is **not proven**; no 39-unit AI run was performed for this demo. See README for the current test command; tests requiring private inputs are skipped when those inputs are absent.

Private screenshots from that browser run:

- Upload form: `.local-demo/screenshots/final/01-upload.png`
- Full dashboard: `.local-demo/screenshots/final/02-dashboard.png`
- Apartment 4 AI review: `.local-demo/screenshots/final/03-ai-review.png`

## Suggested business walkthrough

1. **Apartment 4 — standard apartment:** calculated market scenario ₪4,300,179, based on 10 selected sale records; `REVIEW_REQUIRED`, `NOT_APPROVED`. Quick / balanced / high scenarios are ₪4,170,000 / ₪4,300,000 / ₪4,520,000. Open its evidence and saved AI review; explain the two insufficient-evidence verdicts and why the text is withheld.
2. **Apartment 1 — garden:** `UNPRICED`, `INSUFFICIENT_EVIDENCE`, no eligible sale records; garden area is inferred from a workbook column. Indicative scenarios ₪6,400,000 / ₪6,600,000 / ₪6,930,000 are **not** supported prices or recommendations. Decision is blocked pending review.
3. **Apartment 36 — triplex:** `UNPRICED`, no eligible selected sales, no explicit duplex/triplex transaction or benchmark segment. All three scenarios are unavailable; no standard-apartment proxy was substituted. Decision is blocked pending review.

## Limitations and blockers

- **32 REVIEW_REQUIRED and 7 UNPRICED; all 39 NOT_APPROVED.** Human approval is separate and no pricing recommendation is automatically approved.
- Reported transaction-area semantics are not verified as built area; apartment workbook areas are not legally measured. Sale subtype, geography, floor and other attributes may also be uncertain.
- The reference address is a market anchor, **not verified as the exercise project's actual location**.
- Competitor unit-price evidence is limited: asking prices, missing unit areas, availability and date uncertainty do not establish comparable transaction values or a reliable price per square metre.
- The data is a saved snapshot, not a fresh collection. Provider caches, licensing and timestamps remain material. No claim of current market coverage or certified appraisal is made.
- Only apartment 4 has a saved completed AI review in this demo; Critic SUCCESS is an opinion, not proof. Other apartments are not AI-reviewed. The full historical live-call total is unknown from the saved report; zero offline calls must not be read as zero past calls.
- The private local artifacts and permission to use underlying third-party data are prerequisites. No deployment or externally accessible service has been verified.
