# Sydney bus reliability

How often do Sydney buses turn up on time, and where and when do they fall behind?

This project polls Transport for NSW's live bus feed every two minutes, reduces the arrival predictions to one row per bus arrival, and shows the results in a Streamlit dashboard.

<!-- TODO: dashboard link + screenshot once deployed -->

## Findings

The data covers almost six days, from 23:34 on Friday 9 October to 14:35 on Thursday 15 October 2026: 7.5 million bus arrivals across the NSW networks.

About 69% of arrivals are on time, meaning no more than 1 minute early and no more than 5 minutes late. The median bus arrives 1.2 minutes behind the timetable.

Late buses are far more common than early ones. With the same 3-minute cut-off on both sides, buses are late 4.5 times as often as they are early (27% against 6%). The official window suggests the two are close, 17% early against 14% late, because it counts a bus as early after 1 minute but late only after 5. [Why the on-time window is uneven](#why-the-on-time-window-is-uneven) explains the choice.

Sydney's metropolitan network is the least punctual of the large networks, with 68% of arrivals on time. The Hunter, Central Coast, Illawarra and Blue Mountains networks all sit between 78% and 83%. School buses do worse still at 60%, and a quarter of their arrivals are more than a minute early.

Weekday peaks are where reliability drops. At 9am on weekdays, 20% of arrivals are more than 5 minutes late. The weekday afternoon is the least punctual time overall, mostly because buses run early: at 3pm only 57% of arrivals are on time and a quarter are early, and at 4pm 59% are on time. Weekends are barely different from weekdays (70% on time against 69%).

Among routes with at least 2,000 observed arrivals:

- 738 (Mount Druitt to Eastern Creek) and 889 (Macarthur to Menangle) were late about half the time, with median delays of 5.2 and 4.4 minutes.
- 410 (Macquarie Park to Hurstville), the ninth busiest route in the data with 52,000 arrivals, was on time only 52% of the time.
- 440 (Bondi Junction to Rozelle) has the opposite problem: 40% of its arrivals were more than a minute early.

## How it works

```mermaid
flowchart LR
    API[TfNSW GTFS-Realtime API] -->|every 120s| P[poller.py]
    P --> RAW[(raw snapshots<br/>.pb.gz, never modified)]
    RAW --> T[transform.py]
    T --> PQ[(Parquet stop events<br/>one file per snapshot)]
    S[download_static.py] --> TT[(static timetable<br/>routes, trips, stops)]
    PQ --> W[warehouse.py<br/>DuckDB views]
    TT --> W
    W --> APP[app.py<br/>Streamlit dashboard]
    W --> Q[quality.py<br/>data-quality report]
    W --> E[export.py] --> X[(arrivals.parquet)] --> APP
```

| Step | What it does |
|---|---|
| `poller.py` | Fetches trip updates and vehicle positions every 2 minutes, gzips each response and saves it untouched. Skips snapshots identical to the last one and backs off when the API errors. |
| `transform.py` | Reads each raw snapshot and keeps only the stops a bus will reach within 150 seconds. Writes one Parquet file per snapshot, so reruns only process new data. |
| `warehouse.py` | DuckDB views over the Parquet files: keeps the latest prediction for each trip and stop, then joins route, direction and stop names from the static timetable. |
| `app.py` | Streamlit dashboard with an overview and a route explorer. Refreshes every 2 minutes while data is still coming in. |
| `quality.py` | Checks for collection gaps and how far the arrival times can be trusted (below). |
| `export.py` | Writes the finished results to one compressed Parquet file, which the deployed dashboard reads instead of the live pipeline. |

Raw snapshots are kept exactly as received, so the whole dataset can be rebuilt if the transform logic changes.

### Measuring an arrival from predictions

The live feed doesn't say "bus X arrived at stop Y at 8:03". It only gives predictions of when each bus will reach each upcoming stop. The pipeline uses the last prediction made shortly before the bus reached a stop as its arrival time:

1. In each snapshot, keep stops the bus is predicted to reach within the next 150 seconds (one poll interval plus some slack).
2. Across snapshots, keep the most recent prediction for each trip, service date and stop.
3. Compare that time with the timetable to get the delay.

Cancelled trips, stops marked `NO_DATA` and predictions with a zero timestamp are dropped.

## Why the on-time window is uneven

A bus counts as on time if it is no more than 1 minute early and no more than 5 minutes late. A bus that leaves two minutes early strands a passenger who arrived on time, while a bus two minutes late only means a slightly longer wait, so the window allows much less earliness than lateness.

With an uneven window, early % and late % can't be compared directly. The dashboard uses an even 3-minute cut-off whenever it compares early against late, and labels a route's main issue from its median delay.

How much the cut-off matters:

| Window | Early | On time | Late | Late ÷ early |
|---|---|---|---|---|
| Official (1 min early / 5 min late) | 17.4% | 69.0% | 13.6% | 0.8× |
| Even ±1 min | 17.4% | 30.0% | 52.5% | 3.0× |
| Even ±2 min | 10.1% | 51.7% | 38.2% | 3.8× |
| Even ±3 min | 6.0% | 66.9% | 27.1% | 4.5× |
| Even ±5 min | 2.3% | 84.1% | 13.6% | 6.0× |

Late buses outnumber early ones under every even cut-off, and the ratio grows with the cut-off.

## Data quality

From `python quality.py`:

The poller saved 3,966 snapshots of each feed. After continuous collection began, it missed about 3 hours in total, all on the first weekend:

- Saturday 10 October, 13:47 to 14:17 (29 minutes)
- Saturday 10 October, 16:19 to 16:37 (19 minutes)
- Saturday 10 October, 21:22 to 23:27 (2 hours). The poller crashed when a download stalled halfway through. It now logs the error and retries.
- Sunday 11 October, 00:51 to 00:57 (6 minutes, trip updates only)

The report also shows a gap on Friday afternoon, between a one-off test fetch at 16:42 and the start of the poller at 23:34.

The last prediction for a stop is made a median of 42 seconds before the bus gets there, and 90% are made within 102 seconds.

46% of arrivals appear in two or more snapshots. For those, the predicted time moved by a median of 29 seconds between the first and last snapshot, and 22% moved by more than a minute.

For 9% of arrivals, the last prediction was already in the past when the snapshot was taken. Most likely the feed was reporting the actual arrival time for those.

### Limitations

- Arrival times come from predictions, so they can be off by tens of seconds.
- Weekend means Saturday and Sunday by Sydney time. Public holidays are not handled.
- TfNSW labels some routes "Unknown Description"; the dashboard shows these as "Unclassified".
- Collection covers six days in October 2026, so the results describe that week rather than long-term reliability.
- About 7,900 arrivals (0.1%) belong to routes missing from the static timetable. They have no route name or network, so the dashboard leaves them out.

## Running it yourself

Developed on Python 3.14. You need a free API key from the [TfNSW Open Data Hub](https://opendata.transport.nsw.gov.au/).

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export TFNSW_API_KEY=...

python download_static.py                  # static timetable
caffeinate -i python poller.py --hours 0   # collect (macOS: caffeinate stops the Mac sleeping)
python transform.py --watch 120             # process snapshots as they arrive
streamlit run app.py                        # dashboard on http://localhost:8501
python quality.py                           # data-quality report
python -m pytest                            # tests, no API key or data needed
```

### Deploying

Streamlit Community Cloud can't run the poller, so the deployed dashboard reads a fixed copy of the results.

1. `python export.py` writes `data/export/arrivals.parquet` and `data/export/snapshots.txt`.
2. Commit both files and push.
3. On [share.streamlit.io](https://share.streamlit.io), create an app from this repo with `app.py` as the entry point.

When there's no processed data, `warehouse.py` reads the export automatically. Set `BUS_DATA=export` to try that mode locally.

On python.org builds of Python for macOS, HTTPS requests can fail with `CERTIFICATE_VERIFY_FAILED`. `truststore`, included in the requirements, makes Python use the system certificates and fixes this.

## Built with

Python, GTFS-Realtime bindings, PyArrow / Parquet, DuckDB, pandas, Streamlit.

Data comes from [Transport for NSW Open Data](https://opendata.transport.nsw.gov.au/) and is used under its licence terms.
