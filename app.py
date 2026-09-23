"""Streamlit dashboard for Minnesota legislative and statewide campaign finance."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

import cfb_scraper as cfb

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)

CHAMBER_ORDER = [
    "Governor", "Attorney General", "Secretary of State", "State Auditor",
    "State Senate", "State House",
]

DETAIL_COLUMNS = [
    "Beginning cash on hand", "Individual contributions", "Lobbyist contributions",
    "Committee / fund contributions", "Party unit contributions", "Public subsidy payments",
    "Other receipts", "Campaign expenditures", "Noncampaign expenditures", "Other expenditures",
]

# CFB party abbreviations, in the order they should appear in a legend.
PARTY_COLORS = {
    "DFL": "#2E6FBA",   # Democratic-Farmer-Labor
    "RPM": "#C8322D",   # Republican Party of Minnesota
    "GPM": "#2E8B57",   # Green Party of Minnesota
    "LPM": "#C9A227",   # Libertarian Party of Minnesota
    "LMP": "#8E6FB5",   # Legal Marijuana Now
    "IPMN": "#5FA8A0",  # Independence-Alliance
    "Other": "#8A8A96",
}


# --- cache ------------------------------------------------------------------

def _paths(year: int) -> tuple[Path, Path]:
    return DATA_DIR / f"cfb_{year}.parquet", DATA_DIR / f"cfb_{year}.json"


def save_snapshot(df: pd.DataFrame, year: int) -> None:
    data_path, meta_path = _paths(year)
    df.to_parquet(data_path, index=False)
    meta_path.write_text(json.dumps({
        "scraped_at": dt.datetime.now().isoformat(timespec="seconds"),
        "candidates": len(df),
        "errors": df.attrs.get("errors", []),
    }))


def load_snapshot(year: int) -> tuple[pd.DataFrame | None, dict]:
    data_path, meta_path = _paths(year)
    if not data_path.exists():
        return None, {}
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    return pd.read_parquet(data_path), meta


# --- formatting helpers -----------------------------------------------------

def money(value) -> str:
    """Compact currency for the headline metrics, so they never truncate."""
    if value is None or pd.isna(value):
        return "—"
    for cutoff, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(value) >= cutoff:
            return f"${value / cutoff:,.1f}{suffix}"
    return f"${value:,.0f}"


def horizontal_bars(source: pd.DataFrame, metric: str) -> alt.Chart:
    """Ranked bars coloured by party, with a readable dollar axis and rich tooltips."""
    present = [p for p in PARTY_COLORS if p in set(source["Party"])]
    return (
        alt.Chart(source)
        .mark_bar(cornerRadiusEnd=3)
        .encode(
            x=alt.X(f"{metric}:Q", axis=alt.Axis(format="$,.3~s", title=metric)),
            y=alt.Y("Label:N", sort="-x", title=None,
                    axis=alt.Axis(labelLimit=260, labelFontSize=12)),
            color=alt.Color(
                "Party:N",
                scale=alt.Scale(domain=present, range=[PARTY_COLORS[p] for p in present]),
                legend=alt.Legend(orient="top", title=None, direction="horizontal"),
            ),
            tooltip=[
                alt.Tooltip("Label:N", title="Candidate"),
                alt.Tooltip("Office:N"),
                alt.Tooltip("Party:N"),
                alt.Tooltip(f"{metric}:Q", format="$,.2f"),
                alt.Tooltip("Report:N", title="Most recent report"),
            ],
        )
        .properties(height=max(280, 26 * len(source)))
    )


def district_sort_key(value: str) -> tuple[int, int, str]:
    """Order districts naturally: 1A, 1B, 2A ... 67B, with 'Statewide' first."""
    if value == "Statewide":
        return (0, 0, "")
    digits = "".join(c for c in value if c.isdigit())
    suffix = "".join(c for c in value if c.isalpha())
    return (1, int(digits) if digits else 0, suffix)


# --- page -------------------------------------------------------------------

st.set_page_config(
    page_title="MN Campaign Finance",
    page_icon="🗳️",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown("""
<style>
  div[data-testid="stMetricValue"] { font-size: 1.6rem; }
  div[data-testid="stMetric"] {
      background: rgba(128,128,128,0.08);
      border: 1px solid rgba(128,128,128,0.18);
      border-radius: 10px;
      padding: 0.75rem 1rem;
  }
  .stale { color: #b8860b; }
</style>
""", unsafe_allow_html=True)

st.title("🗳️ Minnesota Campaign Finance")
st.caption(
    "Top-line numbers for every State House, State Senate, and constitutional-office "
    "candidate, pulled live from the Campaign Finance Board's public viewer."
)

with st.sidebar:
    st.header("Data")
    year = st.selectbox("Election segment", [2026, 2025, 2024, 2023, 2022], index=0)
    with_party = st.toggle("Include party & incumbency", value=True,
                           help="One extra request per race for party and incumbency.")
    fast = st.toggle(
        "Fast refresh (may lag)", value=False,
        help="Uses only the Board's summary tables. Much quicker, but those tables run "
             "behind the actual filings for some candidates, so recent reports can be missed.",
    )
    refresh = st.button("🔄 Update table", type="primary", width="stretch")
    st.caption("Fast mode: ~30s, figures may be behind the latest filings." if fast else
               "Full read: ~3 min cold, ~1.5 min once reports are cached.")

if refresh:
    # Two phases with very different costs, so give each its own bar.
    roster_bar = st.progress(0.0, text="Finding candidates…")
    report_bar = None if fast else st.progress(0.0, text="Waiting for the candidate list…")

    def on_roster(done: int, total: int, label: str) -> None:
        roster_bar.progress(done / total, text=f"Races {done}/{total} — {label}")

    def on_report(done: int, total: int, label: str) -> None:
        report_bar.progress(done / total, text=f"Reading report {done}/{total} — {label}")

    with st.spinner("Pulling from cfb.mn.gov…"):
        fresh = cfb.scrape_all(
            year=year, with_party=with_party, progress=on_roster,
            read_reports=not fast, report_progress=None if fast else on_report,
        )
    roster_bar.empty()
    if report_bar is not None:
        report_bar.empty()

    if fresh.empty:
        st.error("No data came back. The CFB site may be down or have changed shape.")
    else:
        save_snapshot(fresh, year)
        read = int((fresh["Source"] == "Report PDF").sum()) if "Source" in fresh else 0
        detail = f" — {read:,} read straight from the filed report." if read else ""
        st.success(f"Pulled {len(fresh):,} candidates across {fresh['Office'].nunique()} races.{detail}")

df, meta = load_snapshot(year)

if df is None:
    st.info(f"No data cached for {year} yet. Press **Update table** in the sidebar to pull it.")
    st.stop()

scraped_at = meta.get("scraped_at")
if scraped_at:
    stamp = dt.datetime.fromisoformat(scraped_at)
    age = dt.datetime.now() - stamp
    hours = age.total_seconds() / 3600
    note = f"Last updated {stamp:%b %-d, %Y at %-I:%M %p} ({int(hours)}h ago)" if hours >= 1 \
        else f"Last updated {stamp:%b %-d, %Y at %-I:%M %p}"
    (st.warning if hours > 24 else st.caption)(note)

if meta.get("errors"):
    with st.expander(f"⚠️ {len(meta['errors'])} race(s) failed to load"):
        st.write(meta["errors"])

# Be explicit about where the numbers came from: the Board's summary tables lag behind
# the filings for some candidates, so a summary-sourced row is a caveat, not a detail.
if "Source" in df:
    counts = df["Source"].value_counts()
    lagging = int(counts.get("CFB summary (may lag)", 0))
    if lagging:
        st.warning(
            f"This snapshot was taken in **fast mode**, so all {lagging:,} rows come from the "
            "Board's summary tables, which run behind the actual filings for some candidates. "
            "Turn off *Fast refresh* and update again for figures read from the reports."
        )
    odd = {k: int(v) for k, v in counts.items()
           if k != "Report PDF" and not k.startswith("No report since")}
    if odd and not lagging:
        with st.expander(f"ℹ️ {sum(odd.values()):,} row(s) not read from a filed report"):
            st.write(odd)
            st.caption(
                "Mostly candidates who file on paper: the Board scans those, and a scanned "
                "page has no machine-readable numbers. Those rows keep the Board's summary "
                "figures and still link the PDF."
            )

# --- filters ----------------------------------------------------------------

with st.sidebar:
    st.header("Filters")
    chambers = [c for c in CHAMBER_ORDER if c in set(df["Chamber"])]
    picked_chambers = st.multiselect("Office", chambers, default=chambers)

    scoped = df[df["Chamber"].isin(picked_chambers)] if picked_chambers else df

    parties = sorted(p for p in scoped["Party"].fillna("").unique() if p)
    picked_parties = st.multiselect("Party", parties, default=parties)

    ballot_only = st.toggle("Only candidates on the ballot", value=True)
    filed_only = st.toggle("Only candidates with a filed report", value=False)
    query = st.text_input("Search candidate or district", placeholder="e.g. Demuth, 34B")

view = scoped.copy()
if picked_parties:
    view = view[view["Party"].fillna("").isin(picked_parties)]
if ballot_only:
    view = view[view["On ballot"]]
if filed_only:
    view = view[view["Report period through"].notna()]
if query:
    needle = query.strip().lower()
    view = view[
        view["Candidate"].str.lower().str.contains(needle, na=False)
        | view["District"].str.lower().str.contains(needle, na=False)
        | view["Office"].str.lower().str.contains(needle, na=False)
    ]

if view.empty:
    st.warning("No candidates match those filters.")
    st.stop()

# --- headline numbers -------------------------------------------------------

filed = view["Report period through"].notna().sum()
cols = st.columns(5)
cols[0].metric("Candidates", f"{len(view):,}", help="After filters")
cols[1].metric("With a filed report", f"{filed:,}", f"{filed / len(view):.0%} of shown")
cols[2].metric("Raised", money(view["Total receipts"].sum()),
               help=f"${view['Total receipts'].sum():,.2f}")
cols[3].metric("Spent", money(view["Total expenditures"].sum()),
               help=f"${view['Total expenditures'].sum():,.2f}")
cols[4].metric("Cash on hand", money(view["Ending cash balance"].sum()),
               help=f"${view['Ending cash balance'].sum():,.2f}")

st.divider()

# --- tabs -------------------------------------------------------------------

table_tab, leaders_tab, races_tab = st.tabs(["📋 Table", "🏆 Top fundraisers", "🗺️ By race"])

SUMMARY_COLUMNS = [
    "Candidate", "Party", "Office", "District", "On ballot",
    "Ending cash balance", "Total receipts", "Total expenditures", "Unpaid bills and loans",
    "Report", "Filed date", "Report period through", "Report PDF",
]

column_config = {
    "Candidate": st.column_config.TextColumn(pinned=True, width="medium"),
    "Party": st.column_config.TextColumn(width="small"),
    "District": st.column_config.TextColumn(width="small"),
    "On ballot": st.column_config.CheckboxColumn("Ballot", width="small"),
    "Ending cash balance": st.column_config.NumberColumn(
        "Cash on hand", format="dollar", help="Ending cash balance on the most recent report"),
    "Total receipts": st.column_config.NumberColumn(
        "Raised", format="dollar", help="Total receipts, cash. In-kind is a separate line item."),
    "Total expenditures": st.column_config.NumberColumn("Spent", format="dollar"),
    "Unpaid bills and loans": st.column_config.NumberColumn("Debts", format="dollar"),
    "Filed date": st.column_config.DateColumn(
        "Filed", format="MMM D, YYYY", help="Date the Board received the report"),
    "Report period through": st.column_config.DateColumn(
        "Through", format="MMM D, YYYY", help="Last day of the reporting period"),
    "Report PDF": st.column_config.LinkColumn("PDF", display_text="Open ↗", width="small"),
    "Candidate page": st.column_config.LinkColumn("CFB page", display_text="View ↗", width="small"),
    "Source": st.column_config.TextColumn("Source", width="medium"),
    "Amendment": st.column_config.NumberColumn("Amd.", format="%d", width="small"),
    "Total receipts incl. in-kind": st.column_config.NumberColumn(format="dollar"),
    "Total expenditures incl. in-kind": st.column_config.NumberColumn(format="dollar"),
}
for name in DETAIL_COLUMNS:
    column_config[name] = st.column_config.NumberColumn(format="dollar")

with table_tab:
    left, right = st.columns([3, 1])
    with left:
        sort_by = st.selectbox(
            "Sort by",
            ["Ending cash balance", "Total receipts", "Total expenditures",
             "Unpaid bills and loans", "Filed date", "District", "Candidate"],
        )
    with right:
        show_detail = st.toggle("Show all line items", value=False)

    ordered = view.copy()
    if sort_by == "District":
        ordered = ordered.assign(_k=ordered["District"].map(district_sort_key)) \
                         .sort_values(["Chamber", "_k", "Candidate"]).drop(columns="_k")
    elif sort_by == "Candidate":
        ordered = ordered.sort_values("Candidate")
    else:
        ordered = ordered.sort_values(sort_by, ascending=False, na_position="last")

    extra = DETAIL_COLUMNS + ["Total receipts incl. in-kind", "Total expenditures incl. in-kind",
                              "Amendment", "Source"]
    columns = SUMMARY_COLUMNS + (extra if show_detail else []) + ["Candidate page"]
    columns = [c for c in columns if c in ordered.columns]
    st.dataframe(
        ordered[columns],
        column_config=column_config,
        hide_index=True,
        width="stretch",
        height=620,
    )

    st.download_button(
        "⬇️ Download these rows as CSV",
        ordered.to_csv(index=False).encode(),
        file_name=f"mn_campaign_finance_{year}.csv",
        mime="text/csv",
    )

with leaders_tab:
    metric = st.radio(
        "Rank by", ["Ending cash balance", "Total receipts", "Total expenditures"],
        horizontal=True, label_visibility="collapsed",
    )
    top_n = st.slider("How many", 10, 60, 25, step=5)

    top = view.dropna(subset=[metric]).nlargest(top_n, metric)
    if top.empty:
        st.info("Nothing to rank with the current filters.")
    else:
        source = top.assign(
            Label=top["Candidate"] + "  ·  " + top["Office"],
            Party=top["Party"].fillna("Other").replace("", "Other"),
        )[["Label", metric, "Party", "Office", "Report"]]
        st.altair_chart(horizontal_bars(source, metric), width="stretch")

with races_tab:
    available = [c for c in CHAMBER_ORDER if c in set(view["Chamber"])]
    # The legislative chambers are where the district view earns its keep, so start there.
    default = available.index("State House") if "State House" in available else 0
    chamber = st.selectbox("Chamber", available, index=default)
    pool = view[view["Chamber"] == chamber]

    by_race = (
        pool.groupby(["Office", "District"], as_index=False)
        .agg(Candidates=("Candidate", "size"),
             Raised=("Total receipts", "sum"),
             Spent=("Total expenditures", "sum"),
             Cash=("Ending cash balance", "sum"))
    )
    by_race = by_race.assign(_k=by_race["District"].map(district_sort_key)) \
                     .sort_values("_k").drop(columns="_k")

    st.dataframe(
        by_race,
        hide_index=True,
        width="stretch",
        height=480,
        column_config={
            "Raised": st.column_config.NumberColumn(format="dollar"),
            "Spent": st.column_config.NumberColumn(format="dollar"),
            "Cash": st.column_config.ProgressColumn(
                "Cash on hand", format="dollar",
                min_value=0, max_value=float(by_race["Cash"].max() or 1)),
        },
    )

    st.caption("Biggest-money races in this chamber, by total raised across all candidates")
    hottest = by_race.nlargest(15, "Raised").assign(Label=lambda d: d["Office"])
    st.altair_chart(
        alt.Chart(hottest)
        .mark_bar(cornerRadiusEnd=3, color="#4C78A8")
        .encode(
            x=alt.X("Raised:Q", axis=alt.Axis(format="$,.3~s", title="Total raised")),
            y=alt.Y("Label:N", sort="-x", title=None),
            tooltip=[
                alt.Tooltip("Label:N", title="Race"),
                alt.Tooltip("Candidates:Q"),
                alt.Tooltip("Raised:Q", format="$,.2f"),
                alt.Tooltip("Spent:Q", format="$,.2f"),
                alt.Tooltip("Cash:Q", title="Cash on hand", format="$,.2f"),
            ],
        )
        .properties(height=max(240, 26 * len(hottest))),
        width="stretch",
    )

st.divider()
st.caption(
    "Source: Minnesota Campaign Finance and Public Disclosure Board. Each row is read from "
    "that candidate's most recent filed report — the newest period, newest amendment — so "
    "**Filed** is the date the Board received it and **Through** is the end of the reporting "
    "period. Figures are cumulative for the election segment. **Raised** and **Spent** are the "
    "cash columns, which is what the Board reports and what makes beginning cash + raised − "
    "spent equal cash on hand; in-kind totals are separate line items under *Show all line "
    "items*. Candidates who file on paper have scanned reports with no machine-readable "
    "numbers; those rows fall back to the Board's summary table and say so in **Source**."
)
