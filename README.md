# Minnesota Campaign Finance dashboard

A Streamlit app that pulls top-line campaign finance numbers for every **State House** (134
districts), **State Senate** (67 districts), **Governor**, **Attorney General**, **Secretary of
State**, and **State Auditor** candidate straight from the Minnesota Campaign Finance and Public
Disclosure Board, and puts them in one sortable, filterable table.

## Run it

```bash
pip install -r requirements.txt
```

```bash
streamlit run app.py
```

Press **🔄 Update table** in the sidebar to pull fresh data. Results are cached to
`data/cfb_<year>.parquet`, so the app opens instantly afterwards and only re-pulls when you ask.

There are two refresh modes:

| Mode | Cost | Accuracy |
| --- | --- | --- |
| **Full** (default) | ~3 min cold, ~1.5 min warm | Reads each candidate's actual filed report |
| **Fast** | ~30s | The Board's summary tables, which lag for some candidates |

## Why full mode reads the PDFs

The obvious approach — and the one this app started with — is the Board's aggregated district
table. It is not *wrong*: wherever it is current it matches the filings to the penny. But **it
lags**, and by weeks for some filers. On 23 September 2026 it still showed Amy Klobuchar's
Pre-Primary figures ($9.30M raised) although her September report ($12.57M) had been accepted on
the 22nd. Five of 29 statewide candidates were stale that day.

Worse, the two aggregate views disagree with each other. For Klobuchar that morning:

| Source | Most recent report | Raised |
| --- | --- | --- |
| Candidate page, "financial" tab | 3/31 (1st Quarter) | $4.86M |
| District table | 7/20 (Pre-Primary) | $9.30M |
| **Her September report PDF** | **9/15 (September)** | **$12.57M** |

So the filing is the only ground truth. It is also the only place the Board publishes a **filing
date** — page 1 of every report carries `Received by the Board <date>`, which appears in no HTML
view and in none of the bulk downloads.

## How the data is fetched

Three passes, in `cfb_scraper.py` (1) and `cfb_reports.py` (2 and 3):

**1. The roster.** The viewer loads its tab content from an internal JSON endpoint that returns
the whole candidate field for an office/district at once:

```
POST /reports-and-data/viewers/campaign-finance/districts-constitutional-offices/api
     office=House&district=34B&year=2026&tabname=financial
```

This gives every candidate, their ID, party, ballot status, and the Board's summary figures —
which also serve as the fallback when a report can't be read.

**2. Which report is operative.** `tabname=reports_data` on the candidate viewer lists that
candidate's reports newest-first with an amendment chain per period. The operative filing is the
highest amendment of the newest period. (Careful: that page has a second table of 24-hour
"Large contributions" notices which would otherwise win on recency.)

**3. The numbers.** Only pages 1 and 2 of that report are parsed — page 1 for the filing date,
page 2 for the Committee Transaction Summary. Filed reports never change, so results are cached
in `data/report_cache.json` keyed by (registration, year, period, amendment); a later refresh
only downloads reports it hasn't seen. The per-candidate lookup in step 2 still runs every
time, which is why a warm refresh is ~1.5 min rather than instant.

Across the full 826-candidate field this reconciles exactly: for all 716 rows with a complete
set of figures, beginning cash + receipts − expenditures equals the reported ending cash to the
cent.

Two quirks worth knowing if you maintain this:

- The endpoint only reads parameters from a **POST body**. A GET with the same query string
  returns `No information found`.
- The POST is **403'd unless the session already carries a `PHPSESSID` cookie** from an ordinary
  page load. `cfb_scraper` primes one session per worker thread.

`tabname=financial` gives the money; `tabname=information` gives party, incumbency, and the
registration number. Report PDFs come from a separate script that *does* accept GET:

```
https://cfb.mn.gov/rptViewer/Main.php?do=viewPDF&year=26&type=pcc&period=D&se=0&regnum=19287&amend=0
```

## What's in the table

Per candidate: party, office, district, on-the-ballot flag, **cash on hand** (ending cash
balance), **raised** (total receipts), **spent** (total expenditures), **debts** (unpaid bills and
loans), the report itself with its **filed** date and period end date, and a direct link to its
PDF. Toggle **Show all line items** for the full breakdown — individual, lobbyist, committee/fund
and party-unit contributions, public subsidy, other receipts, campaign / noncampaign / other
expenditures, in-kind totals, amendment number, and source.

### Cash vs. in-kind

**Raised** and **Spent** are the **cash** columns. The report also has an in-kind column and a
Total, but cash is what the Board's own viewer reports and it is the only choice that makes the
summary reconcile: beginning cash + raised − spent = cash on hand, exactly, verified across the
whole field. The in-kind-inclusive totals are available as separate columns.

### Where a row can fall back

`Source` says where every row's numbers came from:

- `Report PDF` — read from the filing. This is the normal case.
- `Scanned filing — not machine-readable` — the candidate filed on paper and the Board scanned
  it, so the PDF is an image with no text layer. The row keeps the Board's summary figures and
  still links the PDF. OCR would fix this and is not implemented. (~22 candidates.)
- `CFB summary (may lag)` — the whole snapshot was taken in fast mode.
- `No report since 2024` — nothing recent on file; money columns are blank.

Filings older than `MIN_REPORT_YEAR` (2024, in `cfb_reports.py`) are ignored outright. A
committee whose newest report predates that is dormant and its figures say nothing about the
current cycle — and `rptViewer` stops serving those documents anyway, answering with an HTML
error page rather than a PDF.

Note the Board emits `1/1/1900` as a "no report" sentinel in its summary table; the scraper
drops it rather than reporting a 1900 filing.

## Files

| File | Purpose |
| --- | --- |
| `cfb_scraper.py` | Session handling, race enumeration, threaded fetch, HTML→DataFrame parsing. Runnable on its own for a quick sample scrape. |
| `cfb_reports.py` | Resolving each candidate's operative filing and parsing its PDF, with the on-disk report cache. |
| `app.py` | The Streamlit UI. |
| `data/` | Cached snapshots and the parsed-report cache. Git-ignored. |

## Deploying

The app runs fine on Streamlit Community Cloud — point it at `app.py`, no secrets needed. Note
that Community Cloud's filesystem is ephemeral, so the cache doesn't survive a restart and the
first visitor sees an empty app until someone presses **Update table**. To ship a seed snapshot
instead:

```bash
git add -f data/cfb_2026.parquet data/cfb_2026.json data/report_cache.json
```
