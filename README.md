# RFE Signal Match

A FastAPI + vanilla JS tool for Cynet PMs to instantly identify which customer RFEs (feature requests) have already been delivered, are planned in the current PI, or are coming up — so no matched request ever gets missed in a customer conversation.

**Current version: v2.1** — PI Planning Report tab, Outlook/SMTP email sending, live Salesforce pull

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
| **v2.1** | **PI Planning Report tab (18 domain tabs, exec dashboard, Plotly charts, timeframe filter). Outlook email integration: editable To/CC, in-drawer Send via Office 365 SMTP, Outlook draft fallback, Graph API path (IT-gated). Live Salesforce pull (SOQL, no CSV export).** |

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
- **📤 Send Email** — sends directly via Office 365 SMTP (`POST /api/send-email-smtp`). No Azure/Entra app registration needed; requires `SMTP_USER` / `SMTP_PASS` in `.env` and SMTP AUTH enabled on the mailbox.
- **✉️ Outlook draft** — opens a pre-filled draft in the user's own Outlook via `mailto:` (zero setup, always works)
- **Microsoft Graph path** — `POST /api/send-email-graph` for fully-automated service-mailbox sending. Requires an Azure AD app registration with `Mail.Send`; set `GRAPH_TENANT_ID` / `GRAPH_CLIENT_ID` / `GRAPH_CLIENT_SECRET` / `GRAPH_SENDER_UPN`.
- Every send/draft is logged to the `email_log` table for audit (`sent_smtp` / `sent_graph` / `drafted_outlook`)

### ☁️ Live Salesforce pull
The "Salesforce API" card on Data Sources now pulls RFE cases live via SOQL (`POST /api/pull`) — no manual CSV export.
- Configurable "days back" window
- Live progress, then auto-scores the pulled RFEs
- Requires SF API access on the account. Note: SSO-enforced orgs block username/password API login (`INVALID_SSO_GATEWAY_URL`) — needs an integration user or Connected App from your SF admin.

### Environment variables (v2.1)

| Variable | Purpose | Required for |
|---|---|---|
| `SMTP_USER`, `SMTP_PASS` | Office 365 mailbox for sending | 📤 Send Email |
| `SMTP_HOST`, `SMTP_PORT` | Defaults `smtp.office365.com` / `587` | (optional) |
| `SMTP_FROM` | From address (defaults to `SMTP_USER`) | (optional) |
| `GRAPH_TENANT_ID`, `GRAPH_CLIENT_ID`, `GRAPH_CLIENT_SECRET`, `GRAPH_SENDER_UPN` | Azure AD app for Graph send | Graph send (optional) |
| `SF_USERNAME`, `SF_PASSWORD`, `SF_SECURITY_TOKEN`, `SF_DOMAIN` | Salesforce API pull | ☁️ SF pull |

---

## Sharing with Teammates (ngrok)

```bash
~/bin/ngrok http 8000
```

Copy the `https://` URL ngrok prints and share it. Requires a free ngrok account.

---

## Security Notes

- `rfe_dedup.db` is gitignored — contains customer data
- CSV / Excel files are gitignored
- `.env` is gitignored — contains API keys and credentials
- Never commit `.env` or credentials to this repo
