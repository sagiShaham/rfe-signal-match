# RFE Signal Match

A FastAPI + vanilla JS tool for Cynet PMs to instantly identify which customer RFEs (feature requests) have already been delivered, are planned in the current PI, or are coming up — so no matched request ever gets missed in a customer conversation.

---

## What it does

Upload a CSV export of Salesforce RFE cases. The tool:

1. **Scores** every RFE individually against your uploaded context documents (release notes, ADO current PI, roadmap)
2. **Clusters** matched RFEs that are asking for the same thing
3. **Surfaces** each cluster with a match status: **Delivered**, **In Current PI**, or **Planned**
4. **Generates** a ready-to-send email for each matched cluster, referencing the specific customer account

---

## Match Status Logic

| Status | Condition |
|---|---|
| **Delivered** | Release notes score ≥ 0.45 |
| **In Current PI** | ADO Current PI score ≥ 0.12 |
| **Planned** | Any context doc score ≥ 0.10 |
| **No Match** | Below all thresholds |

Only matched RFEs (Delivered / In PI / Planned) appear in the Signal Match table. Unmatched RFEs are scored and stored but not shown.

---

## Scoring Algorithm

Each RFE subject + description is compared against each context document using **token containment**:

```
score = |RFE_tokens ∩ doc_tokens| / max(|RFE_tokens|, 1) × doc_weight
```

This measures how much of what the customer asked for is covered by the document — not just keyword overlap.

---

## Clustering Algorithm

Matched RFEs are grouped using a **Union-Find** structure with pairwise similarity:

```
similarity = Jaccard(0.65) + SequenceMatcher(0.35)
```

Two RFEs are merged into the same cluster if their similarity exceeds **0.55** (threshold). A **domain bonus** is applied to the threshold:

- Same product domain → +0.10
- Same domain **and** sub-domain → +0.20 (additional)

The bonus makes it easier to cluster RFEs from the same product area without lowering the global threshold.

---

## Pipeline (v1.2)

```
Phase 1: Score ALL RFEs individually against context docs
           ↓
Phase 2: Cluster only the matched RFEs (by domain group)
           ↓
Phase 3: Write cluster records to DB (with best match status per cluster)
```

**Why this order matters:** Previous versions clustered first, then scored — so isolated RFEs from the same product area that matched context docs could be missed if they had no similar peers. Now every RFE is evaluated on its own merits first.

---

## Product Domains (12 canonical buckets)

Endpoint · Cloud · SIEM · Identity · ESPM · Automation · Platform · MSP · Email · Mobile · On-prem · Reports

Domain is inferred automatically from the RFE subject/description if the CSV field is blank.

---

## Setup

### Requirements
- Python 3.9+
- macOS / Linux

### Install

```bash
git clone https://github.com/sagiShaham/rfe-signal-match.git
cd rfe-signal-match

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
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
- **Release Notes** (doc_type: `release_notes`) — what has been delivered
- **ADO Current PI** (doc_type: `ado_current_pi`) — what is in the current planning interval
- **Roadmap / Planned** (doc_type: `planned`) — what is coming

Each doc gets a weight (default 1.0). Higher weight = stronger influence on match scores.

### 2. Upload RFE CSV
Go to the **Import** tab and upload a CSV export from Salesforce. Required columns:

| Column | Description |
|---|---|
| `Case Number` | Salesforce case ID |
| `Subject` | RFE title |
| `Description` | Full RFE text |
| `Account Name` | Customer name |
| `Product Domain` | Cynet product area (optional — inferred if blank) |
| `Sub-Domain` | Sub-area (optional) |

### 3. Run Scoring
Click **Run Scoring**. The pipeline scores all RFEs and builds clusters. Takes ~10–30 seconds depending on volume.

### 4. Browse Signal Match
The main table shows matched clusters grouped by domain. Each cluster:
- Shows match status badge (Delivered / In PI / Planned)
- Lists all RFE case IDs and subjects
- Shows which context document matched and why
- Has a **Generate Email** button

### 5. Generate Email
Click **✉ Generate Email** on any cluster. Select the customer account and the specific RFE — the tool drafts a personalised email you can copy and send.

### 6. Search
Use the search bar to filter by case number, customer name, keyword, or domain.

---

## Architecture

```
rfe-signal-match/
├── main.py              # FastAPI backend — all scoring, clustering, API endpoints
├── static/
│   └── index.html       # Single-page frontend (vanilla JS, no build step)
├── requirements.txt
└── .gitignore
```

**Database:** SQLite (`rfe_dedup.db`, gitignored). Tables:
- `run_meta` — one row per import run
- `rfe_pulls` — individual RFE rows with scores
- `rfe_clusters` — computed clusters with match status
- `context_docs` — uploaded reference documents
- `app_config` — scoring thresholds (editable via UI)

---

## Version History

| Version | Description |
|---|---|
| v1.0 | Baseline: clusters render, expand/collapse, email drawer, copy IDs |
| v1.1 | Search (case ID / subject / keyword), 12 canonical domain buckets, email drawer in `<head>` |
| v1.2 | Score-first pipeline: individual scoring before clustering, domain/sub-domain bonus, domain inference for blank fields |

---

## Sharing with Teammates (ngrok)

To expose the local server over a public URL:

```bash
~/bin/ngrok http 8000
```

Copy the `https://` URL ngrok prints — share it with anyone who needs access. Requires a free ngrok account and authtoken configured (`~/bin/ngrok config add-authtoken YOUR_TOKEN`).

---

## Security Notes

- The SQLite database (`rfe_dedup.db`) is gitignored — it contains customer data
- CSV / Excel files are gitignored
- `.claude/settings.local.json` is gitignored — it may contain API tokens
- Never commit `.env` files or credentials to this repo
