# PI Planning Report v2 — design record

**Status:** implemented on branch `feat/pi-planning-v2`, not yet merged
**Supersedes:** the `cynet-pi-planning-report` skill v1 as implemented in
`scoring/pi_report_generator.py` before this branch
**Reference dataset:** `report1789054045014.xls` — 331 open RFEs, 168 accounts,
Jan-2025 → Aug-2026, exported from Salesforce as an HTML table named `.xls`

This document is the source material for **skill v2.0**. Every decision below is
recorded with the reason it was taken and, where a number is quoted, the
evidence from the reference dataset. Read it as the specification; read the
module docstrings for the mechanics.

---

## 1. What this report is for

One meeting: the whole PM team sits down and decides, theme by theme, **what to
start developing this PI, what stays in the backlog, and what to close with the
customer.** People pre-read the report and then walk it during the meeting.

v1 was built to answer a different question — *where is customer demand
loudest* — and did that well. Everything that changed in v2 follows from
swapping the question. The test is no longer "is this chart informative" but
"can a PM open this tab and know what to decide, and defend the decision to the
person whose request is being closed".

Three consequences shaped the whole design:

1. **Every ranking must be explainable out loud.** A rank nobody can justify is
   worse than no rank, because it will be argued about instead of acted on.
2. **The report may not make commercial decisions.** It can say "no second
   customer has asked for this"; it may not say "close this $1.2M account's
   request" (see §4.4).
3. **Nothing may be hidden.** Not a truncated summary, not a clipped table
   column, not a chart quietly showing its top 40 of 213.

---

## 2. Data contract

### 2.1 Columns read from the Salesforce export

| Export column | Stored as | Used for |
|---|---|---|
| `Case Number` | `case_number` | identity, de-duplication, copy-to-clipboard |
| `Subject` | `subject` | clustering, theme naming, subject signal |
| `Description` | `description` | subject signal, PM summary source, "original customer text" |
| `Account Name` | `account_name` | repetition breadth, ARR de-duplication |
| `Account's ARR` | `account_arr` | ARR signal |
| `Account's ARR Currency` | `arr_currency` | **new in v2** — data-quality disclosure |
| `Severity` | `severity` | severity signal (**Critical now handled**) |
| `Business Impact` | `business_impact` | **new in v2** — ranking signal + escalation floor |
| `Business Impact Reason` | `business_impact_reason` | **new in v2** — strongest human signal |
| `Case Owner` | `case_owner` | **new in v2** — stored, not yet surfaced |
| `Product Domain` | `domain` | classification, second pass |
| `Sub-Domain` | `sub_domain` | classification, **first pass** (new in v2) |
| `Date/Time Opened` | `created_date` | recency, timeframe, momentum |
| `Status` | `status` | shown on the case row |

Four columns were added to `rfe_pulls` by migration (`business_impact`,
`business_impact_reason`, `case_owner`, `arr_currency`). `migrate_db()` runs at
import, so a server restart applies them; a report generated against an
un-migrated database degrades to "no business impact recorded" rather than
failing, because `load_data` selects only the v2 columns that actually exist.

**Business Impact is a checkbox**, exported as `1`/`0` — 22 of 331 set on the
reference file — with a free-text `Business Impact Reason` beside it, filled on
only 3. It is the only field in the whole export where a human states a
consequence, which is why it earns both a weight and a floor.

`parse_flag()` accepts `1 / true / yes / y / x / checked` and nothing else. An
unrecognised value must never silently promote an RFE up the ranking.

### 2.2 Ingest paths

Both file-upload paths (the shared Signal Match CSV import and the per-PM report
upload) write through one helper, `insert_rfe_pull()`. They had drifted apart
before; when only one captured Business Impact, a report built from the other
ranked as if no customer had ever asserted one.

The SOQL path is **deliberately not** extended: the custom-field API names for
Business Impact are not confirmed against the org, and guessing one would raise
a Salesforce error on every pull. A report built from a SOQL run therefore
scores as if no business impact were asserted. This is the one known gap in the
data contract (§8).

---

## 3. Classification — Sub-Domain first

v1 classified on `Product Domain`, then a keyword regex. v2 inserts
`Sub-Domain` ahead of both.

**Why:** on the reference export `Product Domain` puts 111 of 331 rows into a
single bucket called "Endpoint", while `Sub-Domain` is filled on 310 rows (94%)
across 31 values that map almost one-to-one onto the report's tabs — *Endpoint
Protection*, *SIEM / CLM*, *Alert UI*, *Web Access Control*, *OS Support*,
*ITDR*, *SSPM / CSPM*. It is the field a PM actually curates per case.

Order: **Sub-Domain → Product Domain → keyword regex on Subject + Description.**
`Any`, `Other`, `N/A`, `None`, `TBD` and blank are treated as carrying no
information and fall through rather than creating a junk bucket.

**Measured effect:** the "Other" tab went from 26 candidate rows (14 Salesforce
"Other" + 12 blank) to **3 of 331 (0.9%)**. Six of the eleven that first landed
in Other were placed by adding five keyword patterns found by reading the tab:
`e-?mail settings?`, `logstash|windows event`, `\bninja\b|\brmm\b`,
`msp level|mssp level`, `device control|usb device`. The remaining three
("Request for integration", "Salesforce tickets notification and closing",
"YARIX | Different versions in last seen") are genuinely unclassifiable from
their text and belong in Other.

Tab order and the 18 tabs are unchanged and remain fixed. SIEM and Identity
stay separate tabs.

---

## 4. The ranking model

Implemented in `scoring/priority.py`. One scoring function, applied at three
levels — RFE, theme (Epic), domain — so a domain's rank is defensible in the
same language as an RFE's.

v1 ranked everything on `arr + count * 80000`. That expression cannot answer
this meeting's question: it is blind to severity, blind to the business-impact
flag, treats ten requests from one account exactly like ten accounts asking
once, and scores a detection blind spot and a tooltip colour identically.

### 4.1 The six signals

| Signal | Weight | Definition |
|---|---|---|
| `severity` | 0.22 | Salesforce Severity. Critical 1.0, High 0.75, Medium 0.45, Low 0.20, **unset 0.30** |
| `arr` | 0.20 | `sqrt(min(arr, 2_000_000) / 2_000_000)` |
| `business_impact` | 0.16 | flag + written reason 1.0; flag alone 0.85; none 0.0 |
| `repetition` | 0.22 | `0.65 × breadth + 0.35 × depth` (§4.2) |
| `recency` | 0.08 | 1.0 for 120 days, then a 300-day half-life; **missing date 0.5** |
| `subject_signal` | 0.12 | keyword families over Subject + Description + reason (§4.3) |

Weights sum to 1.0 and `_composite` asserts it, so a future edit cannot silently
rescale every number in the report. Output is 0–100.

**Why these weights.** Repetition is deliberately equal to severity and above
ARR. Severity is one support engineer's judgement at intake; ARR is the size of
the account, not the value of the request. How many *different* customers
independently asked for the same capability is the only signal in the export
that is about the product rather than about one relationship.

**Why `sqrt` and not `log` for ARR.** `log10` compresses so hard that a $250K
account scores 0.86 of a $2M one, which is not how the business reads those two
numbers. `sqrt` gives $250K → 0.35, $1M → 0.71, $2M → 1.0. The cap matters
independently: without it the single $1.9M account on the reference file would
set the scale and every other request would score near zero on ARR.

**Why unset severity sits above Low.** Blank usually means nobody triaged it,
not that it is harmless. Same reasoning for a missing date scoring 0.5 on
recency: unknown age is not evidence of staleness, and scoring it zero would
quietly demote every row of an export whose date column got renamed.

### 4.2 Repetition — the field Salesforce does not have

Repetition is derived from cluster membership and split in two, because these
are different business situations and the report says which one it is looking at:

* **breadth** — distinct accounts asking: `min(1, log2(customers) / 3)`, so
  1 → 0.00, 2 → 0.33, 4 → 0.67, 8 → 1.00. The 1→2 step is the most informative
  in the entire model: it is the moment a request stops being anecdotal. The
  7→8 step is noise, hence saturation at 8.
* **depth** — most requests filed by any single account: `min(1, (n-1)/2)`, so
  1 → 0.0, 2 → 0.5, 3+ → 1.0. A customer who has asked three times has made
  their point; counting further only rewards a noisy account.

Repetition is measured **inside the selected timeframe** — two customers asking
last month is a different signal from two customers asking two years apart —
but clustering itself happens once over the whole upload (§5).

### 4.3 Subject signal — "common sense", made auditable

Nine keyword families over Subject + Description + business-impact reason, in
order of how much they should move a PI decision: `security_gap` 1.00,
`compliance` 0.92, `data_loss` 0.90, `stability` 0.86, `deal_risk` 0.74,
`scale_mssp` 0.62, `coverage` 0.54, `efficiency` 0.46, `visibility` 0.42, plus a
`nice_to_have` **penalty** of −0.35.

Rules: the strongest matched family sets the base (families do not add up — a
request is not twice as urgent for being describable two ways); a corroboration
bonus of +0.05 per extra family, capped at +0.10; the nice-to-have penalty
applies last, so "would be nice to rename the Hosts tab" cannot ride a $2M ARR
into the commit list.

The matched family also produces the human phrase shown in the "why" line
("compliance or audit obligation"). **A regex or a weight is never printed on a
page.**

*Bug found in calibration:* `hang` matched inside "c-**hang**-e", so every
request using the word "change" scored 0.86 on stability. All short patterns are
now word-bounded. `tests/test_priority.py::test_subject_signal_does_not_match_inside_words`
guards it.

### 4.4 Bands and escalation floors

Score maps to the four decisions the meeting can actually take:

| Score | Band | Action |
|---|---|---|
| ≥ 68 | **Start now** | Commit to this PI |
| ≥ 54 | **Plan** | Size now, commit next PI |
| ≥ 38 | **Keep in backlog** | Revisit next cycle |
| < 38 | **Drop candidate** | Propose closing with the customer |

Thresholds are **absolute, not percentile**, so a score means the same thing in
every report, two PI cycles are comparable, and "nothing is urgent this quarter"
is an answer the model is allowed to give. Percentile banding would always
nominate a top 10% even in a quiet quarter. Confirmed on the reference data: the
3-month window produces **zero** Start-now themes.

Then five facts carry a **floor**, because they need a decision rather than a
default. Calibration produced two results that were arithmetically correct and
wrong for a meeting:

* A **Critical**, business-impact-flagged request from a single $185K customer
  scored 49.7 → "Keep in backlog". One customer means no repetition, and $185K
  is a fifth of the ARR scale.
* A theme asked for by **two customers carrying $1.2M** of ARR was labelled
  "Candidate to close" because its severity was Low and it was old.

| Fact | Floor | Reason |
|---|---|---|
| Critical severity | Plan | Support set it to Critical; the team may decline it, but not by default |
| Business-impact flag **with a written reason** | Plan | Someone took the trouble to state the consequence |
| Business-impact flag alone | Keep in backlog | A customer asserted an impact; never closed silently |
| ≥ $1M ARR at stake | Keep in backlog | Telling a customer that size no is an account conversation |
| ≥ 3 distinct customers asking | Keep in backlog | Repeat demand is what this report exists to surface |

A floor **raises the score to the band threshold** rather than overriding the
band beneath it, so score, band and sort order stay consistent, and the reason
is appended to the row's "why" line ("ranked up because severity is Critical").
Applied to an RFE and to a theme alike: a theme containing a request that must
be decided also must be decided.

**Measured effect:** drop-candidate themes fell from 181 to 167; Keep-in-backlog
rose from 24 to 33; every one of the 22 business-impact-flagged requests is now
Keep-in-backlog or better, where three of them were previously drop candidates.

### 4.5 Aggregate scoring, and why domains are ranked but never banded

`score_group` reads the same six signals at group level: worst severity present
pulled toward the group mean by how rare it is; distinct-account ARR; share of
members flagged with a floor of 0.5 for any flag at all; breadth and depth;
share opened recently; strongest subject signal pulled 40% toward the mean so
one dramatic subject line cannot carry a whole theme.

At **domain** level this produces a usable ordering but useless bands: breadth
and ARR both saturate (74 customers and $11.8M are already maximum), so twelve
of sixteen domains scored above 56 and seven came out "Start now". It is also a
category error — a team commits to a theme, never to a domain. So the report
**ranks** domains (the header shows "rank 3 of 16 active domains") and never
bands them, and the executive chart counts themes by the decision each needs
instead (§6.3).

### 4.6 ARR is counted once per account

v1 summed `account_arr` per row. An account with 13 open RFEs contributed its
ARR 13 times, and the portfolio headline read **$64.1M against a true $20.0M** —
a 3.2× overstatement on a leadership page. Every ARR total in v2 goes through
`priority.distinct_account_arr()`, which takes the maximum per account name.
Unnamed accounts are kept separate rather than collapsed, since two blank names
are not evidence of one customer.

Non-USD ARR (8 of 331 rows, EUR) is **reported as exported, never converted** —
inventing an exchange rate would put a number on a leadership page that no
system can reproduce — and disclosed in the data-quality note.

---

## 5. Clustering

Within a domain, RFEs are merged into themes by subject-token Jaccard similarity
with union-find, at **0.30** normally and **0.17** when Salesforce already says
the two cases share a meaningful Sub-Domain (a curated taxonomy match is
corroborating evidence, so less textual overlap is needed).

**Naming uses the medoid** — the member whose subject is most similar to the
rest, shorter subject winning ties. v1 named a theme after its highest-ARR
member, which let one big account's idiosyncratic phrasing become the label for
everyone else's request.

Theme names are cleaned but **never truncated**: `[RFE]`, `[EXTERNAL]`,
`[BETA]`, `RFE:`, `FR -` prefixes; leading bullets and dashes (calibration
produced "- New CLM data sources"); a trailing inline case number
("Email Security Filters [00392493]"); one trailing full stop. If cleaning would
empty the name, the raw subject is kept — an ugly name beats an empty one.

**Clustering runs once over the whole upload, not per timeframe.** Otherwise a
theme's identity would change when the reader switched windows, and "3 customers
asking" could become "1 customer asking" purely because the theme got re-cut.

Reference result: 331 requests → **213 themes**; 176 singletons, 33 with more
than one customer, largest 23 requests / 16 customers. Median name length 44
characters, p95 87, max 142.

---

## 6. The page

`scoring/pi_report_assets.py` owns CSS, JS and the HTML shell as plain module
strings. v1 built the entire page inside one Python f-string, so every brace in
the CSS and JS had to be doubled — which is how v1 shipped a broken chart and
dead CSS for panels that were never rendered. `render()` now injects data
through unique placeholders and the markup is readable as markup.

### 6.1 Precomputed timeframes

Scores, aggregates **and the written narratives** are precomputed in Python for
each timeframe (All time / 12 / 6 / 3 months) and embedded as
`PAYLOAD.tf[<key>]`. Switching timeframe therefore never leaves stale prose
beside fresh numbers, and no scoring logic is duplicated in JavaScript. Cost:
about 1.6 MB of self-contained HTML for 331 requests.

**The default timeframe is All time**, changed from v1's Last 12 months. This
report decides the fate of an accumulated backlog; on the reference file a
12-month default silently hides 22 requests — and those are the oldest ones,
which is exactly what a cleanup session needs to see.

### 6.2 Filters narrow, they never re-score

The header carries one **Sort by** dropdown (PI Priority, Severity, ARR at
stake, Business impact, Repetition, Most recent, Oldest) and four filters:
severity as five toggle chips, business impact, ARR band, repetition.

A theme's score is a property of the theme, so ticking a filter box must not
change it — otherwise two people reading the same report with different filters
would argue about different numbers. Filters select what is **listed**; the
timeframe selects the **scoring window**. The rule is stated in the
"How PI Priority is calculated" panel, and a filtered theme row shows
"2 of 12 match the filters" rather than pretending the theme is smaller.

Sort comparators in JS mirror `priority.sort_key` exactly, so "Sort by ARR"
means the same thing on every page. PI Priority is the universal tie-breaker, so
equal ARR or equal severity still lands in a defensible order rather than
insertion order.

### 6.3 Executive page

Order: **narrative → KPI strip → decision queue → top 5 epics → two decision
charts → two demand charts → business-impact escalations → data-quality note.**

* **Narrative** — 3 paragraphs written at build time (§7).
* **KPI strip** — six tiles, each with a "so what" line, computed live from the
  filtered set: requests in scope, ARR at stake, themes to start now,
  business-impact flags, Critical-or-High, requests to consider closing.
* **Decision queue** — the centre of the page. Every theme in scope ranked by
  the chosen sort: theme, PI Priority + band chip, top severity, ARR at stake,
  customers, requests (with "up to N from one"), impact flags, and
  *"why now, and what deferring means"*. Click a row for its cases.
* **Where to act first** — breadth (x) against PI Priority (y), bubble size =
  ARR, colour = band, with quadrant guides labelled *commit first / account
  conversation / plan it / candidate to close*. Clicking a bubble lists its
  cases. **This is what replaced the removed chart.**
* **Where this PI's capacity has to go** — themes per domain, stacked by the
  decision each needs, domains ordered by PI Priority.
* **What is heating up** — last 90 days against the 90 before, as one diverging
  series per domain.
* **Demand arriving over time** — month-by-month, gap-filled in Python so a
  quiet month reads as a zero instead of being skipped and flattering the trend.
* **Business-impact escalations** — every flagged request in scope, in full,
  with the reason text where one was written.
* **What these numbers include** — the data-quality note (§7.3).

#### The removed chart

v1's **"Portfolio Demand Snapshot"** plotted RFE count, customer count and
ARR $M as three grouped bar series with ARR bound to a secondary y-axis. Plotly
does not group-offset a bar trace bound to a different axis, so the green ARR
bars were drawn **on top of** the blue count bars — the overlap in the
screenshot — and its ten rotated x-axis labels collided with each other.

It was removed rather than repaired, for a reason that outlives the bug: even
drawn correctly it invited the reader to compare a count against a dollar figure
on one canvas, which is the opposite of a decision aid. **No chart in this
report may use a secondary axis or grouped bars.** Both are asserted by tests.

### 6.4 Domain tabs

Order: **header + narrative → what to decide here → top 5 epics → all themes →
two charts → two charts → interpretation + actions.**

"What to decide here" is three columns — Start now / Plan / Candidates to close
— each listing named themes with counts and ARR, clickable straight to the table
row. Interpretation and Actions are the panels whose CSS shipped unused in v1;
they are now written from the domain's own data (§7.2).

Epic cards deliberately break v1's rule that a card shows only title, RFE count
and ARR. They now carry the band, customers, top severity, impact flags and the
"why now" line, because in this meeting people read the cards.

### 6.5 Layout rules — why columns cannot overlap any more

* Every table is `table-layout: fixed` with an explicit `<colgroup>`, breaks long
  words (`word-break: break-word; overflow-wrap: anywhere`), and sits in its own
  `overflow-x: auto` container with a sensible `min-width`. Columns can never
  ride over each other; a narrow screen scrolls instead.
* PM Summary cells are explicitly unclamped:
  `white-space: normal !important; overflow: visible !important; text-overflow: clip !important`.
  Verified against computed style in the browser, not just in the stylesheet.
* Chart category labels are **wrapped, never ellipsised**, onto as many lines as
  they need, and row height is computed from the number of lines. Left margins
  come from Plotly's `automargin`, never hardcoded (v1 hardcoded `l: 320`).
* A chart that plots a subset **says so** underneath it ("Showing the 40
  highest-ranked of 213 themes"). Found by a test written for text truncation
  that caught the matrix silently dropping 173 themes.

### 6.6 Kept from v1

Light mode only (`#f7f8fa` page, white cards, white Plotly backgrounds); 18 tabs
in the fixed order; SIEM and Identity separate; case numbers copy to the
clipboard everywhere with `stopPropagation` so the row does not toggle;
PM Summaries never truncated.

---

## 7. Written prose — no template captions

`scoring/pi_narrative.py`. v1's prose was a caption: *"<Domain> demand spans N
RFEs from M+ unique accounts, representing $X in ARR"* — three numbers already
shown in the header beside it.

Rules, all enforced in code and tests:

* **No placeholders, no ellipsis, no "TBD".** A sentence that cannot be grounded
  in the data is not written; the section renders shorter instead of padded.
* **Never restate the KPI strip.** A number appears in prose only when it
  carries an argument.
* **Always land on a decision.**
* **Never assert what the data does not say** — "N customers stay on their
  current workaround", never "customers will churn".
* **Deterministic.** The same upload produces the same words: the report is
  re-opened during the meeting and must not change under the people reading it.
* **Everything interpolated is HTML-escaped** (`_e()`), because subjects and
  account names are customer-authored text and these sentences are injected as
  HTML. A subject containing `<script>` renders as text.

### 7.1 Executive narrative

Three paragraphs: the shape of the demand and where it concentrates; the
shortlist with what carries each one; then the counterweight — the
customer-stated escalations that need answering one by one, what should leave the
backlog, and how much has been waiting over a year.

### 7.2 Domain narrative, interpretation, actions

Narrative: what the demand is *about* (leading themes) → who is pushing and how
hard → the risk read (severity split, flags, median age) → the recommendation,
naming the theme to start with.

Interpretation: three labelled read-outs a chart cannot give — **Demand shape**
(breadth vs depth), **Where the revenue sits**, **Age profile**.

Actions: numbered, each naming a theme or case number so it can be pasted into a
PI board or a Salesforce comment without another lookup.

### 7.3 Data-quality note

Only issues actually present in the upload are listed: accounts with no ARR
(16 of 168), non-USD ARR (8 requests), unreadable dates, unset severity, and
always the reminder that ARR counts each account once. A PI meeting argues about
numbers; it should be able to see the caveats without asking.

### 7.4 PM Decision Summaries

Unchanged mechanism, and it was already correct: `scoring/pm_summary.py` (v2.5)
writes every summary during generation, rule-based and deterministic. What v2
changes is that a raw description can no longer *leak into* a summary cell:

* `description` and `pm` are separate fields in the payload. The customer's own
  words appear only behind an explicitly labelled **"Original customer text"**
  expander inside the expanded case row — never in the PM Summary column.
* v1's JavaScript rendered `c.pm_summary || c.description || ''` in two places,
  so a missing summary silently printed raw Salesforce prose. That fallback is
  gone; the only fallback goes back through the summary writer.
* v1 truncated subjects with `slice(0, 90) + '…'` and capped expanded case lists
  at 50 rows. Both removed: the reference file's largest theme shows all 23
  cases.

Measured on the reference file: 331 of 331 summaries written, median 60 words,
most common opening phrase 12% of the set, zero containing `...`, 4 using the
skill's mandated no-description TAM flag (those 4 cases have no description in
Salesforce).

---

## 7b. Review round two — fewer charts, badges that explain themselves

Feedback on the shipped v2.6 was that the platform "looks great but: less charts
more insights; more indicative badges with an explanation on hover; less unclear
charts." A screenshot of the **Reporting** tab carried the evidence, including a
defect nobody had spotted.

### The entity bug the screenshot exposed

Bar labels read `$318K &middot; 1 customer`, and a y-axis label read
`Critical alerts classified as &quot;high&quot; in alerts report`. Plotly draws
SVG text and **does not decode HTML entities**, so two things leaked:

* `&middot;` written directly into Plotly `text` and `hovertemplate` strings, and
* `&quot;` / `&amp;` produced by running the page's HTML escaper, `esc()`, over
  category labels inside `wrapLabel`.

Fixed with a separate `plotlyText()` escaper for anything that reaches a chart:
quotes and ampersands pass through exactly as the customer typed them, and only
`<` / `>` are swapped for the look-alike glyphs `‹` `›`, so `value < 10` keeps
its meaning while no tag can be parsed out of a subject line. Every `&middot;` in
a Plotly string became a literal `·`. `test_no_entity_leaks_in_chart_text`
guards it, and the browser check asserts no `svg text` node matches
`/&(quot|middot|amp|lt|gt);/`.

### Four charts per domain became at most one

On Reporting — 13 requests, 8 themes — the four charts were:

| Chart | Why it went |
|---|---|
| Themes ranked by PI Priority | Redrew the Priority column of the table directly above it |
| ARR at stake by theme | Redrew the ARR column of that same table |
| Requests opened over time | A line wobbling between 0 and 5 at domain volume |
| Where to act first | Six bubbles, five stacked on top of each other at x=1 |

The first three are gone. The decision matrix is now **gated**: it renders only
where it can show a pattern — `themes >= 10 && themes with >1 customer >= 3`. On
the reference export that is 4 domains of 16 (EPP, SIEM, Endpoint Management,
User Management); the other twelve tabs carry no chart at all. The exec page
lost "Demand arriving over time" for the same reason — it described volume
without implying an action, and "What is heating up" answers the only question it
raised, against a baseline rather than against nothing. Exec is now three charts.

### What replaced them: "What stands out"

`pi_narrative.domain_insights()` writes three to seven findings per domain, each
emitted **only when true of that domain**, so a tab shows what it has rather than
a fixed grid of filler. The findings cover: whether any theme has breadth at all;
whether one account is driving the tab (fires only above a 25% share — an earlier
version reported "the most active account is X with 2 requests", which is true
and not worth a row); whether demand has stopped, and when it last arrived;
the severity ceiling; customer-stated business impact; the shape of the decision;
revenue concentration; and anything old that still scores well.

`test_insights_are_specific_not_filler` asserts every finding carries a digit or
a named theme. It failed twice on first run and both failures were real — two
findings were generalities until a count was put in them.

### One panel, not two

Adding the findings panel left the tab saying the same thing twice: the finding
"Only one theme here has more than one customer — *Quarterly Reports*, at 6
customers" sat directly above the Interpretation read-out "1 theme of 8 are asked
for by multiple customers… *Quarterly Reports* is the broadest at 6 customers".
**"More insights" cannot mean stating one insight twice**, so `domain_interpretation`
was deleted, its two unique read-outs (revenue concentration, age profile) moved
into the findings, and its CSS removed. `test_no_second_panel_of_observations`
stops it coming back. Recommended actions stays and now runs full width — it is
the only panel that says what to *do* rather than what is true.

### Badges that explain themselves

Each band badge now carries a **shape as well as a colour** — `▶` Start now,
`◆` Plan, `■` Keep in backlog, `○` Drop candidate — so the four stay
distinguishable in greyscale, in print, and to a colour-blind reader. Hovering
one shows its full meaning: the score range, the floors that can lift something
into it, and what the band asks the reader to do.

The tooltip is a single element appended to `document.body` and positioned on
hover, not a CSS `::after`. A pseudo-element tooltip is clipped by the
`overflow-x: auto` container every table sits in, which is exactly where most
badges are. It flips above or below the badge depending on room.

A **Decision bands key** strip also renders above the first thing on each page
that uses a badge — the exec decision queue, and each domain's decide columns.

### A floor that contradicted the model

Reviewing the new drop-band wording surfaced a theme with **2 customers and
$577K** badged *Drop candidate*. The repetition model treats the 1→2 customer
step as the largest single jump in the backlog — the moment a request stops being
anecdotal — so recommending closure at 2 customers contradicted the model's own
premise. **The repeat-customer floor moved from 3 customers to 2.** Drop themes
fell from 167 to 158, Keep-in-backlog rose from 33 to 42, and no drop candidate
now has a second customer or $1M behind it.

Separately, `epic_rationale` was not band-aware, so a card badged *Drop
candidate* carried the line "defensible to keep in the backlog if capacity is
tight" — the rationale arguing with the badge above it. The second clause now
branches on the band, and for a drop-band theme with real revenue behind it says
so out loud: "Closing it means telling a $318K account no, so make that call
deliberately rather than in bulk."

## 7c. The Weekly Analysis report, classified the same way

The request was for the weekly tab to classify on exactly the dimensions the PI
report does — severity, business impact, ARR, repetition, timeframe — plus a
per-domain PDF.

### Why "the same" had to mean the same code

A PM reads both reports about the same backlog in the same week. If the weekly
report calls a request a drop candidate while the PI report has it in Plan, the
reader is right to distrust both. So the parts that must not drift moved into
`scoring/report_ui.py`: the band metadata and their hover explanations,
`bandBadge()`, `bandKey()`, the tooltip engine, and the control bar. Both asset
layers inject them verbatim, and `test_badges_match_the_pi_report_exactly`
asserts the same string is in both pages.

The weekly report's own `priority_score()` — ARR/50K×2 + severity×3 + recency×2,
unbounded, with no business impact and no repetition — is deprecated in place and
replaced by `scoring.priority.score_rfe`.

### Two mechanisms, one set of semantics

The PI report renders rows from an embedded payload and can re-render on every
control change. The weekly report renders its cards in Python. Rewriting it to
match would be a large change for no reader-visible gain, so the same five
controls drive a different mechanism: every card carries `data-sev`, `data-bi`,
`data-arr`, `data-cust`, `data-days` and `data-score`, and `report_ui.FILTER_JS`
shows, hides and reorders the DOM. The semantics are identical — same option
boundaries, same sort comparators, filters narrow but never re-score, and an
undated card belongs to All time only.

### Measuring whether they actually agree

Worth measuring rather than assuming. On the reference export, first attempt:

| | agreement |
|---|---|
| identical decision band | 96% |
| identical repetition | **76%** |

The repetition gap had one cause. Clustering only ever compares requests inside
one domain, and the two reports partition differently — the weekly keeps Web
Access Control as its own section where the PI report folds it into EPP — so the
same request landed in different-sized groups. Repetition is now measured inside
the **PI report's** domain partition in both reports; the weekly's ten display
sections are untouched. That moved it to:

| | agreement |
|---|---|
| identical decision band | **98%** (326/331) |
| identical repetition | **99%** (327/331) |
| score within 5 points | **99%** |

The residue is four cases whose subject cleaning differs slightly between the two
modules, and a score difference of a point or two because the weekly report pins
"now" to its report date by design while the PI report uses the current date.

### Two bugs the alignment exposed

**The catch-all bucket was collecting repetition it had not earned.**
`build_clusters` sweeps everything unmatched into "Other Requests in this
Domain". Scored as a theme, it became the **top-ranked theme in EPP at 25
customers**, and pushed 298 of 331 requests past a "3+ customers" filter. It is
not a theme — its members have nothing in common beyond not fitting elsewhere.
Catch-alls now measure each member on its own account, are ranked by the
strongest request inside them rather than by breadth, and always sort last.
After the fix, "3+ customers" matches 112 rather than 298.

**Unset severity was being rewritten as "Low"** on load — a claim the export does
not make, and the opposite of the PI report's reading (unset usually means nobody
triaged it). The default is gone; cluster severity now ranks unset below
everything that is set rather than level with Low. This was caught by a test
written for the change, against an earlier edit of mine that had patched the
wrong line.

### The per-domain PDF

Each domain gets an **Export this domain as PDF** button that prints a sheet
built for the purpose — not the screen. Restyling the interactive layout would
mean hiding a sidebar, a control bar, charts, expand arrows and decision buttons
and hoping what remains lands on one page; it would not, because every theme
carries its full case detail.

The sheet answers what a reviewer who was not in the meeting needs: how big this
is (five stat tiles), what has to be decided (the four bands with their shapes),
which themes carry it (top 8, with PI score, band, severity, ARR, customers,
cases and the reason each is ranked where it is), and which cases need an
individual answer (business-impact escalations, with case numbers). A footer
states how the ranking works and that ARR counts each account once.

Printing is the browser's own print-to-PDF: nothing to install on the VM, works
offline, and the reader keeps their own paper size. A server-side PDF library
would add a dependency to a machine where installing one has been painful, for a
worse-looking result.

Measured on the reference export, every domain fits one A4 page — EPP, the
largest at 116 requests, uses 0.89 of a page; the smallest 0.49.

One bug found in review: the screen layout had become a CSS grid with a 250px
sidebar column, so the print sheet rendered **inside that column**. `@media
print` now resets `body` to `display:block`.

## 7d. Filters drive everything below them

Review of v2.7 in the platform: the weekly report was not centred; the KPI
tiles, executive summary and charts did not respond to the filters; "if the
classifications are at the top they need to apply on any component" — with the
executive summary excluded and the filters placed underneath it; and the PDF
button could not be found.

### The rule

**Above the bar describes the whole section and never changes. Below the bar is
the working area and always reflects the filters.** A filter bar at the top of
the page that changed one panel of six was claiming to drive numbers it did not
touch.

Per section, top to bottom: hero (with the export button) → Executive Summary →
Recommended Actions, labelled *Section overview — all N requests, not affected
by the filters* → **the filter bar** → KPI tiles → domain overview (exec) →
charts → request clusters → at-risk accounts. Recommended Actions moved up
because it describes the whole domain. There is one bar element; it is moved
into whichever section is showing, so it always sits directly above what it
drives, and it stays pinned while the working area scrolls. Its count is
section-aware — inside EPP it reads "Showing 2 of 55 in EPP · 10 of 287 in the
report", where it used to say "287 requests in scope" beside 55 EPP cards.

### One implementation per panel

The KPI tiles, the domain overview cards, the four executive charts, the cluster
bubble chart, the at-risk tables and the sidebar counts are drawn in the page
from the request cards the filters leave visible. Their server-side builders
(`_kpi_row`, `_risk_table`, the domain-card block) were deleted, not left in
parallel: two implementations of one panel drift apart. The at-risk table keeps
the server's eligibility rule — ARR over $100K, any High/Critical request, or
two or more requests from the account; top ten by ARR — re-applied to the
filtered set.

QA on a 287-request production upload: **15 filter combinations, 610
assertions** that every panel agrees with the visible cards (totals, distinct
ARR, domain-card sums, chart totals, at-risk membership, all ten sidebar counts,
every domain's KPIs) — zero failures. Card counts were separately checked
against expectations computed from the raw Salesforce columns.

### Bugs found and fixed on the way

* **Content offset twice.** The original layout is a fixed sidebar plus
  `#main { margin-left: 250px }`; the v2.7 control bar added a 250px grid column
  on top. Removed; the bar now lives inside `#main`.
* **ARR still counted per request in the weekly report** — the bug the PI report
  fixed in v2.6 — in six places, including the KPI tile and the domain cards. A
  41-request upload's "Total ARR" fell from $3.9M to $3.1M once each account was
  counted once.
* **A backslash-escaped quote inside a Python f-string** emitted
  `showSection('' + sid + '')` — two adjacent string literals and a SyntaxError
  that stopped every live panel. Replaced with `&#39;`, which has no escape to
  lose.
* **`plotlyText` existed only in the PI page**, so the weekly bubble chart would
  have thrown on first draw. Moved to `report_ui.CHART_TEXT_JS`, shared.
* **The bubble chart spilled over the at-risk table.** Plotly's responsive mode
  sizes a chart to its box; the box had no height, collapsed to 0px, and a 450px
  chart drew out of it. Live charts now have a definite height.
* **The TAM-chase action cut its case list at 120 characters**, which could stop
  part-way through a case number. Now "#… and N more".
* **"Other Requests in this Domain" appeared under "Themes worth discussing"** on
  domain PDF sheets. Catch-alls are excluded from every sheet's theme table.

### "0 flagged" now says which kind of zero it is

The review's "Flagged only → 0 of 41" was correct: that upload had no flagged
requests (the 287-row upload has 10). But nothing could tell a reader that,
because an export *without* the Business Impact column was stored identically —
as 0. `insert_rfe_pull` now stores **NULL when the column is absent**, both
reports carry `bi_available`, and the page and the PI data-quality note say
either "this export has no Business Impact column" (and disable the control) or
"the column is there; nobody set it". Uploads made before this change were
stored as 0 and read as the second case.

### The PDF button, and a sheet for the whole report

The button is a real one now, pinned top-right of every hero — it was a small
white pill among the stat chips and was missed. It sits above the bar, so it
exports the whole section, which is what its label says. The Executive Overview
gained its own one-pager: whole-report stat tiles, the decision split, the top
seven themes portfolio-wide with their domain named, and the top five
escalations. At eight themes and six escalations it measured exactly 1.00 of an
A4 page, with no allowance for print rendering differing from screen; at seven
and five it uses 0.92. Domain sheets use 0.41–0.78.

## 8. Known gaps

1. **SOQL ingest does not capture Business Impact** (§2.2). A report built from
   a Salesforce API pull rather than a file upload scores as if no impact were
   asserted. Fix needs the custom-field API names confirmed in the org.
2. **`case_owner` is stored but not surfaced.** A per-PM view ("my requests in
   this domain") is the obvious next feature and the data is now there.
3. **Non-USD ARR is not converted** (§4.6) — deliberate, and disclosed.
4. **Theme names come from a member subject.** A medoid subject is a good proxy
   for a theme name, not a substitute for one an editor would write.
5. **167 of 213 themes are drop candidates** on the reference file. That is what
   the data says — 176 themes are single-request and 141 of 331 requests are Low
   severity — but it is a large number to work through, and the domain tabs
   group it rather than solving it.

---

## 9. Test coverage

`tests/test_priority.py` (60) and `tests/test_pi_report_v2.py` (50); 217 tests
pass across the suite. The tests that matter most are the ones asserting what
the report may **not** do:

* ARR is never double-counted; unnamed accounts are never collapsed.
* A Critical, a flagged request, a ≥$1M theme and a ≥3-customer theme can never
  be labelled a drop candidate.
* No secondary axis and no grouped bars anywhere in the page.
* No ellipsis character, no `-webkit-line-clamp`, no `text-overflow: ellipsis`,
  and no `.slice()` on any string field.
* A PM Summary is never equal to, or a superset of, the raw description.
* Customer-authored text cannot inject script into the markup or the prose.
* A payload containing `</script>` cannot close the script block.
* An undated request appears under All time only, never as "recent".
* Domain request counts and theme request counts both reconcile to the total.

---

## 10. Files

| File | Role |
|---|---|
| `scoring/priority.py` | **new** — the ranking engine: signals, bands, floors, sort keys |
| `scoring/pi_narrative.py` | **new** — all written prose |
| `scoring/pi_report_assets.py` | **new** — CSS, JS, HTML shell |
| `scoring/pi_report_generator.py` | rewritten — load, classify, cluster, score, narrate |
| `scoring/pm_summary.py` | unchanged — PM Decision Summary writer |
| `main.py` | 4 migration columns, 4 column aliases, `parse_flag`, `insert_rfe_pull` |
| `tests/test_priority.py` | **new** — 60 tests |
| `tests/test_pi_report_v2.py` | **new** — 50 tests |
