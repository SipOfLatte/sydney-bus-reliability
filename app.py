"""Sydney bus reliability dashboard. Run with: streamlit run app.py"""

from __future__ import annotations

import altair as alt
import duckdb
import pandas as pd
import streamlit as st

import warehouse

REFRESH_SECONDS = 120
DEFAULT_NETWORK = "Sydney Buses Network"
SIDEBAR_WIDTH_PX = 240  # Streamlit allows 200-600

# Diverging pair: early (blue, below zero) and late (red, above zero) are opposite problems.
STATUS_COLORS = ["#2a78d6", "#e34948"]
LINE_COLOR = "#2a78d6"
ZERO_LINE_COLOR = "#8a8984"

EARLY_MIN = -warehouse.ON_TIME_EARLY_S // 60
LATE_MIN = warehouse.ON_TIME_LATE_S // 60
EARLY_LABEL = f"Early (>{EARLY_MIN} min)"
LATE_LABEL = f"Late (>{LATE_MIN} min)"
# The on-time window is deliberately uneven (an early bus is missed entirely), so early % and late %
# aren't comparable. Any direct early-vs-late comparison uses this even cut-off instead.
EVEN_CUTOFF_MIN = 3
# A route's "main issue" is only called early or late if its median is at least this far off.
MAIN_ISSUE_MIN_S = 30
DAY_FILTERS = {  # fixed options, so safe to inline as SQL
    "All days": "TRUE",
    "Weekdays": "day_type = 'Weekday'",
    "Weekends": "day_type = 'Weekend'",
}
ON_TIME_HELP = f"No more than {EARLY_MIN} minute early and no more than {LATE_MIN} minutes late"

st.set_page_config(page_title="Sydney bus reliability", page_icon=":material/directions_bus:", layout="wide",
                   initial_sidebar_state=SIDEBAR_WIDTH_PX)


# Cached per query and parameters. The TTL matches the refresh interval so new data still shows up.
@st.cache_data(ttl=REFRESH_SECONDS, show_spinner=False)
def query(sql: str, params: tuple = ()) -> pd.DataFrame:
    return warehouse.connect().execute(sql, list(params)).df()


# {{where}} survives the f-string and is filled in by each caller
SUMMARY_SQL = f"""
    SELECT count(*) AS arrivals,
           avg((status = 'On time')::INT) * 100 AS on_time_pct,
           avg((status = 'Early')::INT) * 100 AS early_pct,
           avg((status = 'Late')::INT) * 100 AS late_pct,
           median(arrival_delay_s) / 60 AS median_delay_min,
           avg((arrival_delay_s < -{EVEN_CUTOFF_MIN} * 60)::INT) * 100 AS off_early_pct,
           avg((arrival_delay_s > {EVEN_CUTOFF_MIN} * 60)::INT) * 100 AS off_late_pct
    FROM arrivals_enriched WHERE {{where}}
"""
STATUS_BY_HOUR_SQL = """
    SELECT hour(arrival_local) AS hour,
           count(*) AS arrivals,
           avg((status = 'Early')::INT) * 100 AS early_pct,
           avg((status = 'Late')::INT) * 100 AS late_pct,
           avg((status = 'On time')::INT) * 100 AS on_time_pct
    FROM arrivals_enriched WHERE {where}
    GROUP BY 1 ORDER BY 1
"""


def early_late_by_hour_chart(df: pd.DataFrame) -> alt.LayerChart:
    """Diverging bars: late goes up from zero, early goes down, on time is what's left."""
    long = pd.concat([
        df.assign(status=EARLY_LABEL, pct=df.early_pct, signed_pct=-df.early_pct),
        df.assign(status=LATE_LABEL, pct=df.late_pct, signed_pct=df.late_pct),
    ])
    # Same scale above and below zero so bar heights compare directly. The 5% floor stops a
    # quiet route from stretching tiny differences across the whole chart.
    limit = max(long.pct.max(), 5) * 1.1
    bars = (
        alt.Chart(long)
        .mark_bar(cornerRadiusEnd=3)
        .encode(
            # Fixed 0-23 domain so hours without data show as gaps instead of disappearing.
            x=alt.X("hour:O", title="Hour of day (Sydney time)", scale=alt.Scale(domain=list(range(24))),
                    axis=alt.Axis(labelAngle=0)),
            y=alt.Y("signed_pct:Q", title="◀ Early   ·   Late ▶  (% of arrivals)",
                    scale=alt.Scale(domain=[-limit, limit]),
                    axis=alt.Axis(labelExpr="abs(datum.value) + '%'")),
            color=alt.Color("status:N", title=None,
                            scale=alt.Scale(domain=[EARLY_LABEL, LATE_LABEL], range=STATUS_COLORS),
                            legend=alt.Legend(orient="top")),
            tooltip=[
                alt.Tooltip("hour:O", title="Hour"),
                alt.Tooltip("status:N", title="Status"),
                alt.Tooltip("pct:Q", title="Share (%)", format=".1f"),
                alt.Tooltip("on_time_pct:Q", title="On time that hour (%)", format=".1f"),
                alt.Tooltip("arrivals:Q", title="Arrivals that hour", format=","),
            ],
        )
    )
    zero = alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(color=ZERO_LINE_COLOR).encode(y="y:Q")
    return (bars + zero).properties(height=320)


def delay_along_route_chart(df: pd.DataFrame) -> alt.LayerChart:
    line = (
        alt.Chart(df)
        .mark_line(color=LINE_COLOR, strokeWidth=2, point=alt.OverlayMarkDef(color=LINE_COLOR, size=60))
        .encode(
            x=alt.X("stop_sequence:Q", title="Stop number along the route"),
            y=alt.Y("median_delay_min:Q", title="Median delay (min, negative = early)"),
            tooltip=[
                alt.Tooltip("stop_sequence:Q", title="Stop #"),
                alt.Tooltip("stop_name:N", title="Stop"),
                alt.Tooltip("median_delay_min:Q", title="Median delay (min)", format=".1f"),
                alt.Tooltip("early_pct:Q", title="Early (%)", format=".0f"),
                alt.Tooltip("late_pct:Q", title="Late (%)", format=".0f"),
                alt.Tooltip("arrivals:Q", title="Arrivals", format=","),
            ],
        )
    )
    zero = alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(color=ZERO_LINE_COLOR, strokeDash=[4, 4]).encode(y="y:Q")
    return (zero + line).properties(height=320)


def kpi_row(s: pd.Series) -> None:
    with st.container(horizontal=True):
        st.metric("On time", f"{s.on_time_pct:.1f}%", help=ON_TIME_HELP, border=True)
        st.metric(EARLY_LABEL, f"{s.early_pct:.1f}%", help=f"More than {EARLY_MIN} minute before the timetable", border=True)
        st.metric(LATE_LABEL, f"{s.late_pct:.1f}%", help=f"More than {LATE_MIN} minutes after the timetable", border=True)
        st.metric("Median delay", f"{s.median_delay_min:+.1f} min", help="Negative means early", border=True)
        st.metric("Arrivals observed", f"{int(s.arrivals):,}", border=True)


def early_vs_late_insight(s: pd.Series) -> None:
    """Compare early and late with the same cut-off on both sides, so neither is favoured."""
    early, late = s.off_early_pct, s.off_late_pct
    if not early or not late:
        return
    ratio = max(early, late) / min(early, late)
    more, less = ("late", "early") if late >= early else ("early", "late")
    if ratio < 1.2:  # within 20% counts as about the same
        text = f"about as often early as late ({early:.1f}% vs {late:.1f}%)"
    else:
        text = f"**{more} {ratio:.1f}× as often as {less}** ({max(early, late):.1f}% vs {min(early, late):.1f}%)"
    st.info(f"Using an even {EVEN_CUTOFF_MIN}-minute cut-off both ways, buses were {text}.",
            icon=":material/lightbulb:")


def no_data(summary: pd.Series, days: str) -> bool:
    if summary.arrivals:
        return False
    st.info(f"No arrivals for **{days.lower()}** yet. Try another day filter, or wait for more data.")
    return True


def overview(network: str, days: str, min_arrivals: int) -> None:
    scope = f"network = ? AND {DAY_FILTERS[days]}"
    summary = query(SUMMARY_SQL.format(where=scope), (network,)).iloc[0]
    if no_data(summary, days):
        return
    kpi_row(summary)
    early_vs_late_insight(summary)

    with st.container(border=True):
        st.subheader("When are buses least reliable?")
        st.altair_chart(early_late_by_hour_chart(query(STATUS_BY_HOUR_SQL.format(where=scope), (network,))),
                        alt="Diverging bars of early (below zero) and late (above zero) arrivals by hour of day")

    with st.container(border=True):
        st.subheader("Least reliable routes")
        st.caption(f"Routes with at least {min_arrivals} observed arrivals, lowest on-time rate first. "
                   f"**Main issue** is based on the route's median delay: at least {MAIN_ISSUE_MIN_S} seconds early "
                   f"or late, otherwise *Mixed*.")
        routes = query(f"""
            SELECT route_label AS "Route",
                   CASE WHEN median(arrival_delay_s) <= -{MAIN_ISSUE_MIN_S} THEN 'Runs early'
                        WHEN median(arrival_delay_s) >= {MAIN_ISSUE_MIN_S} THEN 'Runs late'
                        ELSE 'Mixed' END AS "Main issue",
                   avg((status = 'On time')::INT) * 100 AS "On time (%)",
                   avg((status = 'Early')::INT) * 100 AS "Early (%)",
                   avg((status = 'Late')::INT) * 100 AS "Late (%)",
                   median(arrival_delay_s) / 60 AS "Median delay (min)",
                   count(*) AS "Arrivals"
            FROM arrivals_enriched WHERE {scope}
            GROUP BY route_label HAVING count(*) >= ?
            ORDER BY "On time (%)", "Median delay (min)" DESC
        """, (network, min_arrivals))
        st.dataframe(
            routes,
            hide_index=True,
            alt="Routes ranked by on-time rate, with early and late percentages",
            column_config={
                "On time (%)": st.column_config.ProgressColumn(format="%.1f%%", min_value=0, max_value=100),
                "Early (%)": st.column_config.NumberColumn(format="%.1f%%"),
                "Late (%)": st.column_config.NumberColumn(format="%.1f%%"),
                "Median delay (min)": st.column_config.NumberColumn(format="%+.1f"),
                "Arrivals": st.column_config.NumberColumn(format="localized"),
            },
        )


def route_explorer(network: str, days: str, min_arrivals: int) -> None:
    routes = query(f"""
        SELECT route_label, route_short_name FROM arrivals_enriched WHERE network = ? AND {DAY_FILTERS[days]}
        GROUP BY ALL HAVING count(*) >= ? ORDER BY route_short_name
    """, (network, min_arrivals))
    if routes.empty:
        st.info("No routes have enough arrivals yet. Lower the minimum in the sidebar or wait for more data.")
        return

    route = st.selectbox("Route", routes.route_label, key="route")
    directions = query("""
        SELECT route_direction FROM arrivals_enriched
        WHERE route_label = ? AND route_direction IS NOT NULL
        GROUP BY 1 ORDER BY count(*) DESC
    """, (route,)).route_direction.tolist()
    if not directions:
        st.info("No direction information for this route.")
        return
    direction = st.segmented_control("Direction", directions, default=directions[0], required=True,
                                     key=f"direction_{route}")  # new key per route resets the choice

    where, params = f"route_label = ? AND route_direction = ? AND {DAY_FILTERS[days]}", (route, direction)
    summary = query(SUMMARY_SQL.format(where=where), params).iloc[0]
    if no_data(summary, days):
        return
    kpi_row(summary)

    with st.container(border=True):
        st.subheader("Where along the route do buses gain or lose time?")
        st.caption("Median delay at each stop. A rising line means the bus is losing time on that stretch; "
                   "below the dashed line means it's running early.")
        along = query(f"""
            SELECT stop_sequence, any_value(stop_name) AS stop_name, count(*) AS arrivals,
                   median(arrival_delay_s) / 60 AS median_delay_min,
                   avg((status = 'Early')::INT) * 100 AS early_pct,
                   avg((status = 'Late')::INT) * 100 AS late_pct
            FROM arrivals_enriched WHERE {where}
            GROUP BY 1 HAVING count(*) >= 3 ORDER BY 1  -- a median of fewer than 3 arrivals is noise
        """, params)
        if along.empty:
            st.info("Not enough arrivals per stop yet for this direction.")
        else:
            st.altair_chart(delay_along_route_chart(along), alt="Median delay at each stop along the route")

    with st.container(border=True):
        st.subheader("Reliability by hour")
        st.altair_chart(early_late_by_hour_chart(query(STATUS_BY_HOUR_SQL.format(where=where), params)),
                        alt="Diverging bars of early (below zero) and late (above zero) arrivals by hour for this route")


# Only refresh while new data is still arriving; the exported copy never changes.
@st.fragment(run_every=REFRESH_SECONDS if warehouse.is_live() else None)
def dashboard(network: str, days: str, min_arrivals: int) -> None:
    window = query("""
        SELECT min(arrival_local) AS first, max(arrival_local) AS last,
               (SELECT snapshots FROM collection) AS snapshots
        FROM arrivals
    """).iloc[0]
    refresh = f" · refreshes every {REFRESH_SECONDS // 60} minutes" if warehouse.is_live() else ""
    st.caption(
        f"Data from {window['first']:%a %d %b %H:%M} to {window['last']:%a %d %b %H:%M} (Sydney time) · "
        f"{window.snapshots:,} snapshots{refresh}"
    )
    # on_change="rerun" makes .open available, so only the visible tab runs its queries.
    tab_overview, tab_route = st.tabs([":material/dashboard: Overview", ":material/route: Route explorer"],
                                      key="tab", on_change="rerun")
    if tab_overview.open:
        with tab_overview:
            overview(network, days, min_arrivals)
    if tab_route.open:
        with tab_route:
            route_explorer(network, days, min_arrivals)


st.title(":material/directions_bus: Sydney bus reliability")
st.markdown("How reliable are NSW buses, and where and when do they run early or late? "
            "Built from Transport for NSW live GTFS-Realtime data.")

try:
    networks = query("SELECT DISTINCT network FROM arrivals_enriched WHERE network IS NOT NULL "
                     "ORDER BY 1").network.tolist()
except duckdb.Error:  # no Parquet files yet, so the views have nothing to read
    st.info("No processed data yet. Start poller.py and transform.py, then refresh this page.")
    st.stop()

with st.sidebar:
    st.header("Filters")
    network = st.selectbox("Network", networks,
                           index=networks.index(DEFAULT_NETWORK) if DEFAULT_NETWORK in networks else 0)
    days = st.segmented_control("Days", list(DAY_FILTERS), default="All days", required=True,
                                help="Weekday peaks and weekend traffic behave very differently.")
    min_arrivals = st.slider("Minimum arrivals per route", 5, 200, 30, step=5,
                             help="Routes with fewer observed arrivals are hidden, so a few buses can't skew the rankings.")
    st.caption(f"**On time** = {ON_TIME_HELP.lower()}. **Early** and **late** are anything outside that window.")

dashboard(network, days, min_arrivals)
