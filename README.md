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

Press **🔄 Update table** in the sidebar to pull fresh data. A full refresh is 410 requests across
8 threads and takes about 30 seconds; results are cached to `data/cfb_<year>.parquet`, so the app
opens instantly afterwards and only re-pulls when you ask it to.

## How the data is fetched

No PDF parsing is involved. The CFB viewer loads its tab content from an internal JSON endpoint
that returns the whole candidate field for an office/district in one shot:

```
POST /reports-and-data/viewers/campaign-finance/districts-constitutional-offices/api
     office=House&district=34B&year=2026&tabname=financial
```

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
loans), the most recent report and its period end date, and a direct link to that report's PDF.
Toggle **Show all line items** for the full breakdown — individual, lobbyist, committee/fund and
party-unit contributions, public subsidy, other receipts, and campaign / noncampaign / other
expenditures.

### Two caveats on the dates and PDFs

- **There is no filing date.** The Board does not publish one in this viewer, and it isn't in the
  bulk data downloads either. The **Through** column is the *reporting period end date*, which
  identifies the report unambiguously (3/31 → 1st Quarter, 5/31 → June, ~7/20 → Pre-Primary,
  9/15 → September, 12/31 → Year-End).
- **PDF links point to the original filing** (`amend=0`). Resolving the newest amendment would
  mean one extra request per candidate — over a thousand — so amendments are left to the
  **CFB page** link in the last column.

Candidates with no report on file this election segment show blank money columns; CFB emits
`1/1/1900` as its "no report" sentinel and the scraper drops it.

## Files

| File | Purpose |
| --- | --- |
| `cfb_scraper.py` | Session handling, race enumeration, threaded fetch, HTML→DataFrame parsing, PDF URLs. Runnable on its own for a quick sample scrape. |
| `app.py` | The Streamlit UI. |
| `data/` | Cached snapshots, one Parquet + one JSON metadata file per election segment. Git-ignored. |

## Deploying

The app runs fine on Streamlit Community Cloud — point it at `app.py`, no secrets needed. Note
that Community Cloud's filesystem is ephemeral, so the cache doesn't survive a restart and the
first visitor sees an empty app until someone presses **Update table**. To ship a seed snapshot
instead:

```bash
git add -f data/cfb_2026.parquet data/cfb_2026.json
```
