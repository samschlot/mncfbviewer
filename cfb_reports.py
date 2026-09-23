"""Read each candidate's most recent report straight from its PDF.

Why this exists: CFB's aggregated district table lags behind the actual filings. It is
never *wrong* -- where it is current it matches the PDF to the penny -- but for some
filers it is weeks behind. In September 2026, for instance, the district table still
showed Amy Klobuchar's Pre-Primary numbers ($9.30M) days after her September report
($12.57M) had been accepted.

The filing itself is the only ground truth, and it is also the only place the Board
publishes a *filing date*: page 1 carries "Received by the Board <date>". Page 2 holds
the Committee Transaction Summary with every top-line number.

Two passes per candidate:

  1. ``reports_data`` on the candidate viewer lists their reports newest-first, with an
     amendment chain per report. The operative filing is the highest amendment of the
     newest period.
  2. That report's PDF, of which only the first two pages are parsed.

Reports are immutable once filed, so parsed results are cached on disk by
(registration, year, period, amendment) and a later refresh only fetches new filings.
"""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import io
import json
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import requests
from pypdf import PdfReader

from cfb_scraper import BASE, CANDIDATE_VIEWER, PDF_VIEWER, USER_AGENT

CANDIDATE_API = f"{CANDIDATE_VIEWER}/api"
CACHE_PATH = Path(__file__).parent / "data" / "report_cache.json"

# Anchors in the reports_data tab: javascript:viewPDF('26','pcc','D','0','19287',0)
_LINK = re.compile(
    r"""viewPDF\('(?P<yr>\d+)','(?P<type>\w+)','(?P<period>\w+)','(?P<se>\d+)',"""
    r"""'(?P<reg>\d+)',(?P<amend>\d+)\)"\s*>(?P<name>[^<]*)<"""
)

PERIOD_NAMES = {
    "A": "1st Quarter Report", "B": "June Report", "C": "Pre-Primary Report",
    "D": "September Report", "E": "Pre-General Report", "YE": "Year-End Report",
}

# Older filings are ignored: a committee whose newest report predates this is dormant, and
# its figures say nothing useful about the current cycle. It also sidesteps rptViewer, which
# stops serving documents for old reports and answers with an HTML error page instead.
MIN_REPORT_YEAR = 2024


@dataclass(frozen=True)
class Report:
    """The operative filing for a candidate: newest period, highest amendment."""

    year: str      # two-digit, as rptViewer wants it
    type: str      # "pcc"
    period: str    # "A".."E", "YE"
    se: str
    reg: str
    amend: int
    name: str      # as the viewer labels it, e.g. "2026 September Report"

    @property
    def key(self) -> str:
        return f"{self.reg}|{self.year}|{self.period}|{self.amend}"

    @property
    def pdf_url(self) -> str:
        return (f"{PDF_VIEWER}?do=viewPDF&downloadpdf=false&year={self.year}"
                f"&type={self.type}&period={self.period}&se={self.se}"
                f"&regnum={self.reg}&amend={self.amend}")

    @property
    def label(self) -> str:
        """'2026 September Report', reconstructed so amendment rows don't read 'Amendment #1'."""
        full_year = 2000 + int(self.year)
        base = f"{full_year} {PERIOD_NAMES.get(self.period, self.period)}"
        return f"{base} · Amendment #{self.amend}" if self.amend else base


# --- sessions ---------------------------------------------------------------

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
        s.get(f"{CANDIDATE_VIEWER}/19369/2026/", timeout=30)  # prime PHPSESSID
        _local.session = s
    return s


def _retry(call, attempts: int = 3, base_delay: float = 1.0):
    """Retry a request a couple of times.

    Hammering cfb.mn.gov with ~1,700 requests reliably turns up a handful of dropped
    connections and timeouts. They are transient, so a short backoff recovers them; on
    the last attempt the session is dropped in case its connection pool went bad.
    """
    for attempt in range(attempts):
        try:
            return call()
        except (requests.RequestException, OSError):
            if attempt == attempts - 1:
                raise
            _local.session = None  # force a fresh session and cookie on the next try
            time.sleep(base_delay * (2 ** attempt))


def newest_report(candidate_id: str, year: int) -> Report | None:
    """The operative filing, or None when the candidate has filed nothing this segment."""
    body = {"id": str(candidate_id), "year": str(year), "tabname": "reports_data"}
    resp = _retry(lambda: _session().post(CANDIDATE_API, data=body, timeout=30))
    resp.raise_for_status()
    html = resp.json().get("tabcontent", "")

    # The page holds two tables; only the first is campaign finance reports. The second
    # ("Large contributions") is 24-hour notices and would otherwise win on recency.
    head = html.split("<caption>Large contributions</caption>")[0]
    hits = [m.groupdict() for m in _LINK.finditer(head)
            if 2000 + int(m.group("yr")) >= MIN_REPORT_YEAR]
    if not hits:
        return None

    newest = hits[0]  # the viewer lists reports newest-first
    chain = [h for h in hits if h["period"] == newest["period"] and h["yr"] == newest["yr"]]
    best = max(chain, key=lambda h: int(h["amend"]))
    return Report(year=best["yr"], type=best["type"], period=best["period"], se=best["se"],
                  reg=best["reg"], amend=int(best["amend"]), name=best["name"])


# --- PDF parsing ------------------------------------------------------------

# Each numbered line of the Committee Transaction Summary ends with a run of money
# columns. The first is Cash; the last is Total (Cash + in-kind). Cash is what the
# Board's own viewer reports and what makes #1 + #10 - #19 = #20 reconcile, so Cash is
# the headline figure and Total is kept alongside it.
_RUN = r"((?:\s*\(?-?[\d,]+\.\d{2}\)?)+)"
_MONEY = re.compile(r"\(?-?([\d,]+\.\d{2})\)?")

_LINES = {
    "Beginning cash on hand": r"\b1 Beginning cash balance\b[^\d]*\d{2}/\d{2}/\d{4}[^\d]*" + _RUN,
    "Individual contributions": r"\b2 Individual Contributions\b.*?IND" + _RUN,
    "Lobbyist contributions": r"\b3 Lobbyist Contributions\b.*?LOB" + _RUN,
    "Committee / fund contributions": r"\b4 Political committee and political fund contributions\b.*?PCF" + _RUN,
    "Party unit contributions": r"\b5 Political party and terminating principal campaign committee contributions\b.*?(?:PTY/ ?TERM PCC|TERM PCC)" + _RUN,
    "Other receipts": r"\b6 Other contributions\b.*?OTH" + _RUN,
    "Public subsidy payments": r"\b7 Public Subsidy Payment\b.*?PS" + _RUN,
    "Total receipts": r"\b10 Total Receipts Sum #2 to #9" + _RUN,
    "Campaign expenditures": r"\b13 Total Campaign Expenditures Sum #11 to #12" + _RUN,
    "Noncampaign expenditures": r"\b14 Noncampaign disbursements\b.*?NCD" + _RUN,
    "Other expenditures": r"\b18 Other disbursements\b.*?Sch. B3" + _RUN,
    "Total expenditures": r"\b19 Total Expenditures and Disbursements Sum #13 to #18" + _RUN,
    "Ending cash balance": r"Ending cash balance on \d{2}/\d{2}/\d{4}" + _RUN,
    "Unpaid bills and loans": r"\b23 Total debt of committee Sum #21C \+ #22C" + _RUN,
}

_FILED = re.compile(r"Received by the Board ([A-Z][a-z]+ \d{1,2}, \d{4})")
_PERIOD = re.compile(r"Period Covered: \d{2}/\d{2}/\d{4} through (\d{2}/\d{2}/\d{4})")


def _money(run: str, last: bool) -> float | None:
    hits = _MONEY.findall(run)
    if not hits:
        return None
    value = float(hits[-1 if last else 0].replace(",", ""))
    return -value if run.rstrip().endswith(")") else value


def parse_report_pdf(blob: bytes) -> dict | None:
    """Top-line numbers from pages 1-2, or None if the numbers can't be read.

    Two ways that happens, both handled by the caller falling back rather than guessing:
    some candidates file on paper and the Board scans it, giving an image-only PDF with
    no text layer; and for old reports rptViewer serves an HTML error page instead of a
    document, so the bytes aren't a PDF at all.
    """
    if not blob.startswith(b"%PDF"):
        return None
    reader = PdfReader(io.BytesIO(blob))
    pages = [(reader.pages[i].extract_text() or "") for i in range(min(2, len(reader.pages)))]
    flat = re.sub(r"\s+", " ", "\n".join(pages))

    if "Committee Transaction Summary" not in flat:
        return None

    filed = _FILED.search(flat)
    through = _PERIOD.search(flat)
    out: dict = {
        "Filed date": dt.datetime.strptime(filed.group(1), "%B %d, %Y").date() if filed else None,
        "Report period through": (dt.datetime.strptime(through.group(1), "%m/%d/%Y").date()
                                  if through else None),
    }
    for field, pattern in _LINES.items():
        match = re.search(pattern, flat)
        out[field] = _money(match.group(1), last=False) if match else None
    # In-kind is excluded from the cash columns above but is worth showing on its own.
    for field in ("Total receipts", "Total expenditures"):
        match = re.search(_LINES[field], flat)
        out[f"{field} incl. in-kind"] = _money(match.group(1), last=True) if match else None
    return out


# --- cache ------------------------------------------------------------------

def load_cache() -> dict:
    if not CACHE_PATH.exists():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_cache(cache: dict) -> None:
    CACHE_PATH.parent.mkdir(exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache))


def _decode(entry: dict) -> dict:
    out = dict(entry)
    for field in ("Filed date", "Report period through"):
        if out.get(field):
            out[field] = dt.date.fromisoformat(out[field])
    return out


def _encode(values: dict) -> dict:
    out = dict(values)
    for field in ("Filed date", "Report period through"):
        if isinstance(out.get(field), dt.date):
            out[field] = out[field].isoformat()
    return out


# --- per candidate ----------------------------------------------------------

def fetch_candidate(candidate_id: str, year: int, cache: dict) -> dict:
    """Resolve and read one candidate's operative filing.

    Returns a dict that is always safe to merge onto a roster row. ``Source`` says where
    the numbers came from so a fallback is never silently presented as a fresh read.
    """
    report = newest_report(candidate_id, year)
    if report is None:
        return {"Source": f"No report since {MIN_REPORT_YEAR}", "Report": None,
                "Amendment": None, "Report PDF": None, "Filed date": None}

    base = {
        "Report": report.label,
        "Amendment": report.amend,
        "Report PDF": report.pdf_url,
    }

    hit = cache.get(report.key)
    if hit is not None:
        return {**base, **_decode(hit), "Source": "Report PDF"}

    blob = _retry(lambda: _session().get(report.pdf_url, timeout=180)).content
    try:
        values = parse_report_pdf(blob)
    except Exception:  # a malformed document shouldn't cost us the row
        values = None

    if values is None:
        # Keep the roster's summary numbers and say why they're being used.
        if not blob.startswith(b"%PDF"):
            # rptViewer stops serving documents for long-dormant committees; don't offer
            # a link that only leads to an error page.
            return {**base, "Report PDF": None, "Filed date": None,
                    "Source": "Report no longer served by CFB"}
        return {**base, "Source": "Scanned filing — not machine-readable", "Filed date": None}

    cache[report.key] = _encode(values)
    return {**base, **values, "Source": "Report PDF"}


def enrich(
    roster: list[dict],
    year: int,
    cache: dict,
    progress=None,
    max_workers: int = 8,
) -> tuple[list[dict], list[str]]:
    """Read every candidate's filing and merge it over the roster rows.

    Roster values survive wherever the PDF could not supply a number, so a scanned or
    unparseable filing degrades to the Board's summary figures rather than to blanks.
    """
    errors: list[str] = []
    total = len(roster)

    def job(row: dict) -> dict:
        try:
            return fetch_candidate(row["Candidate ID"], year, cache)
        except Exception as exc:
            errors.append(f"{row['Candidate']}: {exc}")
            return {"Source": "Lookup failed — showing CFB summary"}

    merged: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(job, row): row for row in roster}
        for done, future in enumerate(concurrent.futures.as_completed(futures), start=1):
            row = futures[future]
            update = {k: v for k, v in future.result().items() if v is not None}
            if update.get("Source", "").startswith("No report since"):
                # Nothing on file this segment: clear any stale roster figures.
                row = {**row, **{k: None for k in row if k in _LINES}}
            merged.append({**row, **update})
            if progress:
                progress(done, total, row["Candidate"])

    return merged, errors
