# RFE Signal Match

A FastAPI + vanilla JS tool for Cynet PMs to instantly identify which customer RFEs (feature requests) have already been delivered, are planned in the current PI, or are coming up — so no matched request ever gets missed in a customer conversation.

**Current version: v2.4 — Report workspaces** — Weekly Analysis and PI Report each take your own CSV/Excel upload and build reports that are yours alone, restructured navigation, and a Signal Match banner making the shared dataset explicit

---

## What it does

Upload a CSV export of Salesforce RFE cases. The tool:

1. **Scores** every RFE individually against your uploaded context documents using semantic AI models
2. **Clusters** matched RFEs that are asking for the same thing
3. **Surfaces** each cluster with a match status: **Delivered**, **In Current PI**, or **Planned**
4. **Generates** a ready-to-send email for each matched cluster, referencing the specific customer account

---

## Scoring Algorithm (v2.0)

The v2 pipeline replaces keyword matching with a two-stage neural scoring engine.

### Stage 1 — Corpus Embedding (one-time per run, ~25s)

Every context document (release note, ADO epic, Confluence page, roadmap item) is converted into a list of 768 numbers called a **vector** by the `BAAI/bge-base-en-v1.5` bi-encoder model. These numbers encode the semantic *meaning* of the text, not just its keywords. Done once per run, cached in memory.

### Stage 2 — Vector Recall / Bi-encoder (~2ms per RFE)

The same bi-encoder converts the RFE string `"{subject}. {description}"` into a query vector. A single matrix multiplication gives a cosine similarity score for every corpus document simultaneously. The **top 12** most similar documents are shortlisted as candidates.

If the RFE has a `domain` tag, documents for other domains are filtered out.

> **Analogy:** An HR assistant writes a sticky note for each of 550 CVs and compares them to a sticky note for the job description. Fast, rough — narrows 550 CVs down to 12 worth reading carefully.

### Stage 3 — Cross-encoder Reranking (~120ms per RFE on GPU)

The top-12 candidates each go through a second model: `cross-encoder/ms-marco-MiniLM-L-6-v2`. Unlike the bi-encoder which encodes texts separately, the cross-encoder **reads the RFE text and each candidate document together as one input** — so it can catch semantic equivalences like "GPO-based deployment" matching "Active Directory automation". The top-5 scoring candidates are kept.

> **Analogy:** A senior recruiter reads each CV and the job description side-by-side, line by line, and gives a precise relevance score. Far more accurate than sticky-note comparison, but only feasible for the 12 pre-filtered candidates.

> **Why not run this on all 550 docs?** The cross-encoder must read every (RFE, doc) pair fresh — it cannot be pre-computed. Running it on all 550 docs × 1,463 RFEs would take ~27 hours. The bi-encoder pre-filter makes it feasible.

### Stage 4 — Fusion Formula

Four signals are combined into a single fused score:

```
fused_score = normalise(rerank_score)
            × source_weight
            × recency_factor
            × fulfillment
```

| Factor | Description |
|---|---|
| `normalise(rerank_score)` | Sigmoid of cross-encoder logit → converts to 0–1 scale |
| `source_weight` | Trustworthiness of the matched document type (see table below) |
| `recency_factor` | 1.0 if doc < 12 months old, 0.8 if older |
| `fulfillment` | How completely the evidence meets the RFE: normalised rerank score (default), or 0–1 verdict from LLM when judge is enabled |

**Source weights (configurable in Settings):**

| Source | Weight | Rationale |
|---|---|---|
| Release notes | 1.0 | Shipped — highest certainty |
| ADO Current PI | 0.9 | Actively in development |
| ADO Upcoming PI | 0.6 | Committed backlog |
| Confluence (dated) | 0.5 | Design doc, dated |
| Roadmap | 0.4 | Directional intent |
| Confluence (undated) | 0.3 | Weakest signal |

### Stage 5 — Status Thresholds (configurable)

The fused score is compared against thresholds:

| Status | Minimum fused score |
|---|---|
| **Delivered** | ≥ 0.65 |
| **In Current PI** | ≥ 0.50 |
| **Planned** | ≥ 0.35 |
| **No Match** | < 0.35 |

All thresholds are adjustable in the **Settings** tab — no code change needed.

---

## LLM Judge (optional, off by default)

When enabled (`RFE_JUDGE_ENABLED=1`), an LLM reads each RFE alongside the top-5 candidate documents and returns a structured verdict: match status, a quoted evidence snippet, and a fulfillment score (0–1). This replaces the rerank-only verdict in the fusion formula, improving accuracy on RFEs with paraphrased or indirect matches.

**Cascade gate:** The LLM only runs on RFEs whose cross-encoder score ≥ 0.45 (the best ~150 per run). This caps LLM cost regardless of total RFE volume.

**Provider cascade:** Anthropic Claude → OpenAI GPT-4o-mini → Ollama (local) → rerank-only fallback.

**Cost estimate (GPT-4o-mini, cascade gate ON):** ~$0.06 per full rescore of 1,463 RFEs.

---

## Clustering (Phase 2)

Matched RFEs are compared pairwise using keyword overlap (Jaccard similarity on stemmed subject tokens). RFEs above the similarity threshold are grouped into a cluster (demand signal). The cluster inherits the strongest match status among its members.

**Domain bonus:** same domain → threshold −0.10; same domain + sub-domain → threshold −0.20.

---

## Full Pipeline

```
Phase 1:
  ┌─ Embed all corpus docs (one-time, ~25s) ─────────────────────┐
  │                                                               │
  │  For each RFE:                                                │
  │    1. Bi-encoder: query vector → cosine sim → top-12 docs    │
  │    2. Domain filter (drop off-domain docs)                    │
  │    3. Cross-encoder: rerank top-12 → keep top-5              │
  │    4. [LLM judge if enabled AND score ≥ gate]                 │
  │    5. Fusion formula → fused score                            │
  │    6. Status threshold → Delivered / In PI / Planned / None   │
  └───────────────────────────────────────────────────────────────┘
           ↓
Phase 2:
  Cluster matched RFEs by subject similarity + domain
           ↓
Phase 3:
  Write cluster records to DB
```

**Performance (Apple M4, MPS GPU auto-detected):**

| Stage | Time |
|---|---|
| Corpus embedding (550 docs) | ~25s (once) |
| Reranking 1,463 RFEs | ~4 min |
| LLM judge on shortlist (when enabled) | ~7 min for ~150 RFEs |
| Full run — vector only | ~4 min 30s |
| Full run — with LLM judge | ~11 min |

---

## Product Domains

12 canonical buckets: Endpoint · Cloud · SIEM · Identity · ESPM · Automation · Platform · MSP · Email · Mobile · On-prem · Reports

Domain is inferred automatically from the RFE subject/description if the CSV field is blank.

---

## Setup

### Requirements
- Python 3.9+
- macOS / Linux
- Apple Silicon GPU auto-detected (MPS); NVIDIA GPU also supported (CUDA)

### Install

```bash
git clone https://github.com/sagiShaham/rfe-signal-match.git
cd rfe-signal-match

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Environment Variables (optional)

Create a `.env` file:

```
# LLM Judge (optional — off by default)
RFE_JUDGE_ENABLED=0          # set to 1 to enable
ANTHROPIC_API_KEY=...        # preferred judge (Claude Sonnet)
OPENAI_API_KEY=...           # fallback judge (~$0.06/run with cascade gate)

# Tuning
RFE_TOP_K_RECALL=12          # bi-encoder candidates per RFE (speed vs. recall)
RFE_JUDGE_GATE=0.45          # min cross-encoder score to qualify for LLM judge
RFE_JUDGE_MAX=150            # max RFEs sent to LLM per run (cost cap)
RFE_DEVICE=mps               # force device: cpu | cuda | mps (auto-detected if blank)
```

### Run

```bash
source venv/bin/activate
uvicorn main:app --host 0.0.0.0 --port 8000
```

Open [http://localhost:8000](http://localhost:8000)

---

## Usage

### 1. Upload Context Documents
Go to the **Context Docs** tab and upload:
- **Release Notes** (`release_notes`) — what has shipped
- **ADO Current PI** (`ado_current_pi`) — in active development this PI
- **ADO Upcoming PI** (`ado_upcoming_pi`) — committed next cycle
- **Roadmap / Confluence** (`roadmap`, `confluence_dated`, `confluence_undated`)

### 2. Upload RFE CSV
Go to the **Import** tab and drop the Salesforce RFE export CSV.

Required columns:

| Column | Description |
|---|---|
| `Case Number` | Salesforce case ID |
| `Subject` | RFE title |
| `Description` | Full RFE text |
| `Account Name` | Customer name |
| `Product Domain` | Cynet product area (optional — inferred if blank) |
| `Sub-Domain` | Sub-area (optional) |

### 3. Run Scoring
Click **Re-score**. The pipeline processes all RFEs:
- Phase 1 (~4 min on M4): GPU-accelerated vector scoring with live progress bar
- Phase 2 (~1 min): clustering matched RFEs

### 4. Browse Signal Match
The main table shows matched clusters grouped by domain. Expand any cluster to see individual RFEs, the matched source, and the evidence snippet.

### 5. Generate Email
Click **✉ Generate Email** on any cluster for a draft customer email, ready to copy and send.

---

## Architecture

```
rfe-signal-match/
├── main.py              # FastAPI backend — scoring orchestration, clustering, API
├── scoring/
│   └── v2_pipeline.py   # v2 neural pipeline (bi-encoder, cross-encoder, LLM judge, fusion)
├── static/
│   └── index.html       # Single-page frontend (vanilla JS, no build step)
├── requirements.txt
└── .gitignore
```

**Database:** SQLite (`rfe_dedup.db`, gitignored). Tables: `run_meta`, `rfe_pulls`, `rfe_clusters`, `context_docs`, `matches`, `app_config`

---

## Version History

| Version | Description |
|---|---|
| v1.0 | Baseline: clusters render, expand/collapse, email drawer, copy IDs |
| v1.1 | Search (case ID / subject / keyword), 12 canonical domain buckets |
| v1.2 | Score-first pipeline, domain/sub-domain bonus, domain inference for blank fields |
| v1.3 | Configurable match thresholds via UI, subject-only clustering, domain+sub-domain grouping |
| v2.0 | Semantic vector scoring: bi-encoder + cross-encoder + GPU (MPS/CUDA). LLM judge cascade (off by default, ~$0.06/run with GPT-4o-mini). Real-time progress bar. ~4 min full run on Apple M4. |
| v2.1 | PI Planning Report tab (18 domain tabs, exec dashboard, Plotly charts, timeframe filter). Outlook email integration: editable To/CC, in-drawer Send via Office 365 SMTP, Outlook draft fallback, Graph API path (IT-gated). Live Salesforce pull (SOQL, no CSV export). |
| v2.2 — UI makeover | Decluttered Signal Match: one status color language (pill), monochrome signal-bar match strength (Strong/Likely/Weak) replacing colored 0-1 chips, raw scores moved to hover/expand, 4 clean summary tiles, collapsed toolbar with a Filters popover, progressive-disclosure rows. Tabs reordered — lands on Signal Match; Scoring Config moved last under "Advanced". Plus: internal test-email — submit controlled `@cynet.com`-only `[TEST]` batches to the production RFE Mail Broker, open its Entra review portal, and monitor status (server-side keys, no customer sending). |
| v2.3 — Weekly Analysis + customer email | New Weekly Analysis tab: a native build of the Cynet weekly RFE report for deciding which state to assign each RFE — 10 domain sections plus Executive, cluster-based grouping covering 100% of a domain's RFEs, priority scoring, NEW/GROWING/STABLE trends, Plotly charts, and persisted state decisions. Email: the `@cynet.com` recipient restriction is lifted (any domain, including real customers) with subjects used verbatim — the broker's Entra-gated review-and-approve step remains the guardrail. Security: the unreviewed direct-send path is gone — the "Send Email" button and the `/api/send-email-smtp` and `/api/send-email-graph` endpoints were removed, so the Mail Broker is the only way an email can leave the platform. |
| **v2.4 — Report workspaces** | **Weekly Analysis and PI Report become per-PM workspaces: upload your own CSV or Excel file and get a report built from exactly that set, saved in your own browser, with a New report action always available and live build progress. Uploaded data is deliberately kept out of `run_meta`, so it can never become "the latest run" and change what Signal Match shows for everyone. Uploads accept `.csv`, `.xlsx`, `.xlsm` and `.xls`, plus the HTML-table, XML-Spreadsheet and tab-separated files that tools commonly emit with an `.xls` extension — the format is detected from the file's bytes, not its name. Navigation restructured: Signal Match with Data Sources beneath it, an *Other tools* section for Weekly Analysis and PI Report (both Beta), and *Advanced* for Scoring Config. Signal Match gains a banner naming the dump it is showing and stating that uploading replaces it for every PM.** |

---

## v2.1 Features

### 📊 PI Planning Report tab
A portfolio-level strategic demand report generated natively from the RFE data in SQLite.
- 18 domain tabs (Executive + 17 product domains) in fixed order
- Executive page: Top 5 Strategic Epics (ranked by ARR + RFE count × $80K), Portfolio Demand Snapshot, Top 10 Domains, Trends Chart
- Per-domain: Top 5 Epics, sortable request tables, ARR/customer/momentum charts, volume trend
- Global timeframe filter (12 / 6 / 3 months / All Time) that recomputes all charts and epics client-side
- Light-mode Plotly charts, copy-to-clipboard case numbers, full untruncated PM summaries
- Served at `GET /api/pi-report` (self-contained HTML), embedded in the platform via iframe
- Generator: [`scoring/pi_report_generator.py`](scoring/pi_report_generator.py)

### ✉️ Outlook / email integration
The email drawer (Signal Match → expand cluster → Generate Email) now supports sending, not just copying.
- **Editable To** (customer contact) + **CC** (CSM / stakeholders) + editable Subject/Body
- **📤 Send via Mail Broker** — the only send path. Stages the email in the RFE Mail Broker for review; a reviewer approves before it is delivered. See [RFE Mail Broker](#rfe-mail-broker--sending-email).
- **✉️ Outlook draft** — opens a pre-filled draft in the user's own Outlook via `mailto:` (zero setup, always works). Sends nothing itself — you press send in Outlook.
- **📋 Copy** — copies the drafted text to the clipboard.
- Every send/draft is logged to the `email_log` table for audit.

> **Removed in v2.3:** the old **📤 Send Email** button and the `POST /api/send-email-smtp`
> and `POST /api/send-email-graph` endpoints. Both delivered mail with no review and no
> approval, and the app has no authentication, so anyone able to reach it could have called
> them. Do not reintroduce a direct-send path.

### ☁️ Live Salesforce pull
The "Salesforce API" card on Data Sources now pulls RFE cases live via SOQL (`POST /api/pull`) — no manual CSV export.
- Configurable "days back" window
- Live progress, then auto-scores the pulled RFEs
- Requires SF API access on the account. Note: SSO-enforced orgs block username/password API login (`INVALID_SSO_GATEWAY_URL`) — needs an integration user or Connected App from your SF admin.

### Environment variables (v2.1)

| Variable | Purpose | Required for |
|---|---|---|
| `SF_USERNAME`, `SF_PASSWORD`, `SF_SECURITY_TOKEN`, `SF_DOMAIN` | Salesforce API pull | ☁️ SF pull |

The `SMTP_*` and `GRAPH_*` variables are gone — they configured the direct-send endpoints
removed in v2.3. Sending is configured via `RFE_BROKER_*` instead.

---

## Sharing with Teammates (ngrok)

```bash
~/bin/ngrok http 8000
```

Copy the `https://` URL ngrok prints and share it. Requires a free ngrok account.

---

## Weekly Analysis tab

A native implementation of the Cynet weekly/bi-weekly RFE report, built to answer
one question: **which state should each RFE be assigned?** (Add to Backlog ·
Out of Scope · Needs Info · Deferred).

Generated directly from the SQLite DB — no XLS upload. Served at
`GET /api/weekly-report` as a self-contained light-mode HTML document and embedded
in the platform via an iframe. Generator:
[`scoring/weekly_report_generator.py`](scoring/weekly_report_generator.py).

### Structure

- **11 sections:** Executive Overview + EPP · Web Access Control · Email Security ·
  SIEM · Identity · CSPM · Platform · Reporting · Automations · AI Initiatives.
  (SIEM and Identity are always separate sections.)
- **Executive page:** global KPI row, 6–7 executive bullets naming specific case
  numbers, clickable domain-overview cards, 4 Plotly charts (count by domain,
  ARR by domain, severity pie, recent-vs-older stacked bar), and a global
  at-risk-accounts table.
- **Each domain page:** pastel hero banner, KPI row, executive summary, **cluster-based**
  Top Priority Requests, a momentum bubble chart, an at-risk-accounts table, and
  3–5 recommended actions.

### Cluster-based, not one-row-per-RFE

Every RFE in a domain is grouped into exactly **one** thematic cluster (e.g.
"Kubernetes & Container Coverage", "USB & Storage Device Control") — so a domain
page covers 100% of its requests, not just the top 10. Predefined themes match
first; anything left over is grouped by subject-token similarity. Clusters are
ranked by combined priority score, with a `READ FIRST` badge on rank #1.

Priority score per RFE: `(ARR / 50,000) × 2 + severity_weight × 3 + recency × 2`.
Trend badge: `NEW` ≤ 7 days, `GROWING` 8–14 days, `STABLE` older.

### PM Decision Summaries (requires an LLM)

Every RFE card carries a 2–4 sentence PM Decision Summary answering what is broken,
the operational consequence, and a routing signal (`⚠️ bug/defect — route to
engineering`, `Deal blocker`, `⚠️ Regression`, …).

These are **LLM-generated** (`claude-opus-5`) and cached in the `pm_summaries` table.
Click **🧠 Generate PM summaries** on the tab; progress is shown live and the report
re-renders when done. Requires `ANTHROPIC_API_KEY` in `.env`.

> **A raw Salesforce description is never used as a summary.** When a description is
> missing, the report shows the mandated flag: *"⚠️ No description provided — follow
> up with TAM before routing."* When a description exists but no summary has been
> generated yet, the card says so explicitly rather than pasting the description.
> This is enforced both at render time and at the storage boundary.

### Assigning state

Each RFE card has **Assign state** buttons that persist immediately to the
`rfe_decisions` table via `POST /api/weekly-report/decision`. Decisions survive
report regeneration and server restarts, and appear as a chip on the card.

### Description backfill

The Salesforce SOQL pull does not always include `Description`. When the latest run
lacks one, the same case number from an earlier run (e.g. a CSV import) is used so
summaries can still be written. The tab's status line reports how many RFEs have a
usable description.

### Endpoints

| Endpoint | Purpose |
|---|---|
| `GET /api/weekly-report` | The self-contained HTML report |
| `GET /api/weekly-report/status` | Coverage: RFE count, summaries cached, states assigned, LLM availability |
| `POST /api/weekly-report/summaries` | Background LLM job to generate missing PM summaries |
| `POST /api/weekly-report/decision` | Persist (or clear) an RFE's assigned state |

## RFE Mail Broker — sending email

RFE Signal Match can submit emails to the production **RFE Mail Broker**
(Azure Function `func-rfe-notif-prd-1791a`), open the broker's browser review portal, and
monitor batch status.

> **Recipients may be on any domain, including real customers.** There is no client-side
> domain allowlist. The guardrail is the broker's review step: submitting only *stages* a
> batch as `PendingReview`. Nothing is delivered until someone on the broker's Entra
> allowlist (`RFE_REVIEWER_EMAILS`) approves it in the review portal. The platform cannot
> send an email on its own.

### Architecture

```
Browser (send drawer)  ──►  POST /api/test-email/submit        (local backend only)
                       ──►  GET  /api/test-email/status/{id}
                       ──►  GET  /api/test-email/config          (safe booleans, no secrets)
   main.py  ──►  broker_client.py  ──►  https://func-rfe-notif-prd-1791a.azurewebsites.net/api
                    (x-functions-key header — server-side only)
```

- The browser calls **only** local backend endpoints. It never calls the Azure Function
  directly and never sees a Function key.
- The submit/status Function keys are read from server environment variables, never returned
  to the browser, never logged, never committed.
- The broker returns a `reviewUrl` containing an **expiring token in the URL fragment** — a
  temporary credential. It is passed to the browser once for the reviewer to open, kept in
  memory only, and never logged or persisted.
- Approval/rejection happens **only** in the broker's Entra-authenticated review portal — this
  app does not reproduce or bypass it.

### Environment variables

Add to `.env` (see [`.env.example`](.env.example)). Never commit real values.

| Variable | Purpose |
|---|---|
| `RFE_BROKER_BASE_URL` | Broker API base, e.g. `https://func-rfe-notif-prd-1791a.azurewebsites.net/api` |
| `RFE_BROKER_SUBMIT_KEY` | `submit_batch` Function key (server only) |
| `RFE_BROKER_STATUS_KEY` | `get_batch_status` Function key (server only) |
| `RFE_BROKER_TEST_MODE` | Master on/off switch — must be `true` to allow submits (default `true`) |

If any required value is missing, the send action is **disabled** in the UI with an
administrator-facing message; the rest of the app starts and runs normally.

### Local development

```bash
cp .env.example .env         # then fill in the two Function keys (ask IT / broker owner)
source venv/bin/activate
uvicorn main:app --port 8000
```

Open the app → **Signal Match** → expand a cluster → **✉ Generate Email** → **📤 Send via Mail Broker**.

### Safety controls

- **The review step is the control.** A submitted batch is `PendingReview` and inert. Approval
  happens only in the broker's Entra-gated portal, by a reviewer on `RFE_REVIEWER_EMAILS`.
- Recipient must be a syntactically valid address (matching the broker's own rule). Any
  domain is accepted; malformed input such as `a@b.com@evil.com` is rejected server-side.
- Subject is used **verbatim** — no prefix is injected, since a real customer must never
  receive a marker they didn't write. Subject and body must be non-empty.
- Body is HTML-escaped server-side before being wrapped as `bodyHtml`.
- Batch size is capped at 5 messages.
- Submits are refused unless `RFE_BROKER_TEST_MODE=true`.
- The sender mailbox (`product-notifications@cynet.com`) is fixed by the broker and is not
  user-editable or present in the payload.
- The broker re-verifies a content hash before queueing, so an approved body cannot be
  swapped after the fact.

### CC / Reply-To limitation

The RFE Mail Broker does **not** support CC or Reply-To. The send drawer shows a disabled CC
field with the message *"CC is not yet supported by the RFE Mail Broker and will not be
submitted."* No workaround (e.g. injecting CC into the body or making extra mail calls) is
implemented.

### Sample request / response

`POST /api/test-email/submit`
```json
{
  "recipient": "schudinov@cynet.com",
  "subject": "Your RFE status update",
  "body": "Your requested capability is planned.",
  "rfe_id": "RFE-12345",
  "customer_name": "Acme Corp",
  "cluster_id": 42
}
```
Response (sanitized — no Function keys):
```json
{
  "batchId": "RFE-TEST-20260717-131310-a1b2c3",
  "status": "PendingReview",
  "reviewUrl": "https://func-rfe-notif-prd-1791a.azurewebsites.net/api/review/RFE-TEST-...#token=…",
  "messageCount": 1
}
```

`GET /api/test-email/status/{batchId}` returns `batchId`, `status`, `messageCount`, `counts`
(missing counters normalized to `0`), `approvedBy`, `rejectedBy`, and `items`
(`recipient`, `status`, `attemptCount`, `errorCode`).

### Status lifecycle

```
PendingReview → Queuing → Queued → Sending → Completed
                                            → PartiallyFailed / Failed
PendingReview → Rejected            (no email sent)
```
The UI polls status every 5 seconds and stops at a final state:
`Completed`, `PartiallyFailed`, `Failed`, `DryRunCompleted`, `Rejected`.

### Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| "Internal test sending is unavailable" | `RFE_BROKER_*` not set on the server, or `RFE_BROKER_TEST_MODE` ≠ `true`. |
| `503` on submit | Broker not configured on the server. |
| `502` "rejected the server's credentials" | Wrong/expired submit key — check `RFE_BROKER_SUBMIT_KEY` with the broker owner. |
| Batch stuck at `PendingReview` | Nobody has approved/rejected in the broker review portal yet. |
| `Failed` with `MailboxScope…` error code | Broker/Exchange mailbox scoping issue — a broker-side concern. |

## Security Notes

- `rfe_dedup.db` is gitignored — contains customer data
- CSV / Excel files are gitignored
- `.env` is gitignored — contains API keys and credentials
- Never commit `.env` or credentials to this repo
