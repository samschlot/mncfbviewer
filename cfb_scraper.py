"""Scrape top-line campaign finance numbers from the Minnesota CFB viewer.

The public viewer at
https://cfb.mn.gov/reports-and-data/viewers/campaign-finance/districts-constitutional-offices/
is an Angular/jQuery shell that loads its tab content from an internal endpoint:

    POST /reports-and-data/viewers/campaign-finance/districts-constitutional-offices/api
        office=House&district=1A&year=2026&tabname=financial

The response is JSON with a single ``tabcontent`` key holding an HTML table of every
candidate in that office/district -- one request per district covers the whole field,
so there is no need to open or parse the report PDFs themselves.

Two things the endpoint is picky about:
  * it only reads parameters from a POST body (a GET with a query string returns
    "No information found"), and
  * the POST is rejected with a 403 unless the session already holds a PHPSESSID
    cookie from an ordinary page load.

Report PDFs are a separate host-side script that *does* accept GET:

    https://cfb.mn.gov/rptViewer/Main.php?do=viewPDF&year=26&type=pcc&period=D&se=0&regnum=19287&amend=0
"""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import re
import threading
from dataclasses import dataclass

import pandas as pd
import requests
from bs4 import BeautifulSoup

BASE = "https://cfb.mn.gov"
VIEWER = f"{BASE}/reports-and-data/viewers/campaign-finance/districts-constitutional-offices"
API = f"{VIEWER}/api"
CANDIDATE_VIEWER = f"{BASE}/reports-and-data/viewers/campaign-finance/candidates"
PDF_VIEWER = f"{BASE}/rptViewer/Main.php"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

DEFAULT_YEAR = 2026
MAX_WORKERS = 8


@dataclass(frozen=True)
class Office:
    """One entry in the viewer's office/district picker."""

    code: str       # "GC", "House", "Senate", ...
    district: str   # "", "1A", "67", ...
    chamber: str    # display grouping
    label: str      # display name

    @property
    def url(self) -> str:
        tail = f"{self.district}/" if self.district else ""
        return f"{VIEWER}/{self.code}/{tail}"


def all_offices() -> list[Office]:
    """The 205 races we care about: 4 constitutional offices + 134 House + 67 Senate."""
    offices = [
        Office("GC", "", "Governor", "Governor"),
        Office("AG", "", "Attorney General", "Attorney General"),
        Office("SS", "", "Secretary of State", "Secretary of State"),
        Office("SA", "", "State Auditor", "State Auditor"),
    ]
    for n in range(1, 68):
        for half in ("A", "B"):
            offices.append(Office("House", f"{n}{half}", "State House", f"House {n}{half}"))
    for n in range(1, 68):
        offices.append(Office("Senate", str(n), "State Senate", f"Senate {n}"))
    return offices


# --- session handling -------------------------------------------------------
# Each worker thread gets its own primed session; requests.Session is not
# designed to be shared across threads.

_local = threading.local()


def _session() -> requests.Session:
    s = getattr(_local, "session", None)
    if s is None:
        s = requests.Session()
        s.headers.update({
            "User-Agent": USER_AGENT,
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        })
        # Prime the PHPSESSID cookie; without it the API POST 403s.
        s.get(f"{VIEWER}/GC/", timeout=30)
        _local.session = s
    return s


def _fetch_tab(office: Office, year: int, tabname: str) -> str:
    payload = {
        "office": office.code,
        "district": office.district,
        "year": str(year),
        "tabname": tabname,
    }
    resp = _session().post(API, data=payload, headers={"Referer": office.url}, timeout=30)
    resp.raise_for_status()
    return resp.json().get("tabcontent", "")


# --- parsing ----------------------------------------------------------------

_COL_ID = re.compile(r"\bcol-(\d+)\b")


def _parse_matrix(tabcontent: str) -> tuple[dict[str, dict[str, str]], dict[str, str], set[str]]:
    """Turn a candidate-per-column table into {candidate_id: {row label: value}}.

    Both the financial and information tabs use the same shape: a header row of
    ``<th class="cancol col-19287 running">Demuth, Lisa</th>`` and then one row per
    metric whose cells carry the matching ``col-<id>`` class. The ``running`` class
    marks candidates who have filed to be on the ballot.
    """
    soup = BeautifulSoup(tabcontent, "html.parser")
    table = soup.find("table")
    if table is None:
        return {}, {}, set()

    names: dict[str, str] = {}
    on_ballot: set[str] = set()
    for th in table.select("thead th.cancol"):
        classes = th.get("class", [])
        match = _COL_ID.search(" ".join(classes))
        if not match:
            continue
        cid = match.group(1)
        names[cid] = th.get_text(strip=True)
        if "running" in classes:
            on_ballot.add(cid)

    values: dict[str, dict[str, str]] = {cid: {} for cid in names}
    for row in table.find_all("tr"):
        label_cell = row.find("th")
        if label_cell is None or "cancol" in (label_cell.get("class") or []):
            continue
        label = label_cell.get_text(strip=True)
        if not label or label == "Candidate":
            continue
        for cell in row.select("td.cancol"):
            match = _COL_ID.search(" ".join(cell.get("class", [])))
            if not match:
                continue
            text = cell.get_text(strip=True).replace("\xa0", "").strip()
            if text:
                values.setdefault(match.group(1), {})[label] = text

    return values, names, on_ballot


def _money(text: str | None) -> float | None:
    """'$443,080.48' -> 443080.48; '($12.00)' -> -12.0; blank -> None."""
    if not text:
        return None
    cleaned = text.replace("$", "").replace(",", "").strip()
    negative = cleaned.startswith("(") and cleaned.endswith(")")
    cleaned = cleaned.strip("()")
    if not cleaned or cleaned in {"-", "--"}:
        return None
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return -value if negative else value


def _date(text: str | None) -> dt.date | None:
    if not text:
        return None
    try:
        parsed = dt.datetime.strptime(text.strip(), "%m/%d/%Y").date()
    except ValueError:
        return None
    # CFB emits 1/1/1900 as a "no report on file" sentinel.
    return parsed if parsed.year >= 2000 else None


# Report period codes used by rptViewer, keyed off the period end date. The CFB
# viewer never exposes which report a row came from, but the "most recent report
# through" date identifies it unambiguously.
_PERIOD_BY_MONTH_DAY = {
    (3, 31): ("A", "1st Quarter Report"),
    (5, 31): ("B", "June Report"),
    (9, 15): ("D", "September Report"),
    (12, 31): ("YE", "Year-End Report"),
}


def _report_for(through: dt.date | None) -> tuple[str | None, str | None]:
    """Map a period end date to (period code, human report name)."""
    if through is None:
        return None, None
    key = (through.month, through.day)
    if key in _PERIOD_BY_MONTH_DAY:
        code, name = _PERIOD_BY_MONTH_DAY[key]
    elif through.month in (7, 8):
        code, name = "C", "Pre-Primary Report"
    elif through.month in (10, 11):
        code, name = "E", "Pre-General Report"
    else:
        return None, None
    return code, f"{through.year} {name}"


def pdf_url(registration_id: str, through: dt.date | None) -> str | None:
    """Direct-download URL for the most recent report's PDF, or None if undetermined.

    Links the original filing (``amend=0``); the CFB viewer would need a separate
    per-candidate request to resolve the newest amendment.
    """
    period, _ = _report_for(through)
    if period is None:
        return None
    return (
        f"{PDF_VIEWER}?do=viewPDF&downloadpdf=false&year={through.year % 100:02d}"
        f"&type=pcc&period={period}&se=0&regnum={registration_id}&amend=0"
    )


# --- one race ---------------------------------------------------------------

_FINANCIAL_FIELDS = {
    "Beginning cash on hand": "beginning cash on hand",
    "Individual contributions": "individual contributions",
    "Lobbyist contributions": "lobbyist contributions",
    "Committee / fund contributions": "committee / fund contributions",
    "Party unit contributions": "party unit contributions",
    "Public subsidy payments": "public subsidy payments",
    "Other receipts": "other receipts",
    "Total receipts": "total receipts",
    "Campaign expenditures": "campaign expenditures",
    "Noncampaign expenditures": "noncampaign expenditures",
    "Other expenditures": "other expenditures",
    "Total expenditures": "total expenditures",
    "Ending cash balance": "ending cash balance",
    "Unpaid bills and loans": "unpaid bills and loans",
}


def _match_label(row_labels: dict[str, str], wanted: str) -> str | None:
    """Row labels carry dates ('Ending cash balance as of 3/31/2026'), so match loosely."""
    for label in row_labels:
        if label.lower().startswith(wanted):
            return label
    return None


def scrape_office(office: Office, year: int = DEFAULT_YEAR, with_party: bool = True) -> list[dict]:
    financial, names, on_ballot = _parse_matrix(_fetch_tab(office, year, "financial"))
    if not names:
        return []

    info: dict[str, dict[str, str]] = {}
    if with_party:
        info, _, _ = _parse_matrix(_fetch_tab(office, year, "information"))

    rows = []
    for cid, name in sorted(names.items(), key=lambda kv: kv[1]):
        cells = financial.get(cid, {})
        details = info.get(cid, {})
        through = _date(cells.get(_match_label(cells, "most recent report through") or ""))
        _, report_name = _report_for(through)

        row = {
            "Candidate": name,
            "Office": office.label,
            "Chamber": office.chamber,
            "District": office.district or "Statewide",
            "Party": details.get("Party", ""),
            "Incumbent": details.get("Incumbent", ""),
            "On ballot": cid in on_ballot,
            "Candidate ID": cid,
            "Report period through": through,
            "Report": report_name,
            "Report PDF": pdf_url(cid, through),
            "Candidate page": f"{CANDIDATE_VIEWER}/{cid}/{year}/",
        }
        for out_name, prefix in _FINANCIAL_FIELDS.items():
            label = _match_label(cells, prefix)
            row[out_name] = _money(cells.get(label)) if label else None
        rows.append(row)
    return rows


# --- the whole field --------------------------------------------------------

COLUMN_ORDER = [
    "Candidate", "Party", "Office", "Chamber", "District", "On ballot", "Incumbent",
    "Ending cash balance", "Total receipts", "Total expenditures", "Unpaid bills and loans",
    "Report", "Report period through", "Report PDF",
    "Beginning cash on hand", "Individual contributions", "Lobbyist contributions",
    "Committee / fund contributions", "Party unit contributions", "Public subsidy payments",
    "Other receipts", "Campaign expenditures", "Noncampaign expenditures",
    "Other expenditures", "Candidate ID", "Candidate page",
]


def scrape_all(
    year: int = DEFAULT_YEAR,
    with_party: bool = True,
    offices: list[Office] | None = None,
    progress=None,
) -> pd.DataFrame:
    """Pull every race in parallel. ``progress(done, total, label)`` is called per race."""
    targets = offices if offices is not None else all_offices()
    total = len(targets)
    rows: list[dict] = []
    errors: list[str] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(scrape_office, o, year, with_party): o for o in targets}
        for done, future in enumerate(concurrent.futures.as_completed(futures), start=1):
            office = futures[future]
            try:
                rows.extend(future.result())
            except Exception as exc:  # keep going; one bad district shouldn't kill a refresh
                errors.append(f"{office.label}: {exc}")
            if progress:
                progress(done, total, office.label)

    df = pd.DataFrame(rows)
    if df.empty:
        return df

    df = df.reindex(columns=COLUMN_ORDER)
    df["Report period through"] = pd.to_datetime(df["Report period through"], errors="coerce")
    # attrs ride along into Parquet, so keep them JSON-serializable.
    df.attrs["scraped_at"] = dt.datetime.now().isoformat(timespec="seconds")
    df.attrs["year"] = year
    df.attrs["errors"] = errors
    return df.sort_values(["Chamber", "District", "Candidate"], kind="stable").reset_index(drop=True)


if __name__ == "__main__":
    import sys

    sample = [o for o in all_offices() if o.code in {"GC", "AG"} or o.district in {"1A", "1"}]
    frame = scrape_all(offices=sample, progress=lambda d, t, l: print(f"{d}/{t} {l}", file=sys.stderr))
    print(frame.head(20).to_string())
    print(f"\n{len(frame)} candidates; errors: {frame.attrs.get('errors')}")
