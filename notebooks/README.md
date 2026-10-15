# Recording analysis template

From the repository root:

```sh
uv sync --extra analysis
uv run --extra analysis marimo edit notebooks/recording_analysis.py
```

Choose an `.rrd` using the built-in marimo file browser. It starts in the
configured recording directory when available, shows only RRD files, and allows
navigation to other local folders. Selecting a file loads it automatically;
no test recording is preselected. Files are read directly from disk.

Duplicate `recording_analysis.py` for an experiment. It provides a searchable
annotated catalog; clock selection; time, state, text and numeric filters;
sample tables; numeric/boolean/array-element plots; statistics; comparison of
up to six scalar signals; record metadata; and filtered Parquet downloads.

The data is available as `run.fields[path]`, `run.samples(path, timeline="mission")`,
`run.catalog`, `run.metadata`, and `filtered_samples`. Each field table retains
its own native Arrow/Polars value dtype. Table displays stringify field values
so browser JavaScript cannot round large integers; calculations and Parquet
retain the exact values. Plotting intentionally converts numeric values to
floating point.

`*_ns` columns contain the exact nanosecond clock indexes stored in the RRD.
`time_s` uses a common origin across the run for the chosen clock. `mission`
and `receive_time` permit comparisons across message sources. Vehicle boot
clocks belong to the named system/component; do not mix independent clocks.
Older recordings may already have rounded clock indexes because their writer
used floating-point seconds. Their original metadata may retain exact timestamps.

No forward-fill, interpolation, resampling, or cross-message joins happen
implicitly. `state` distinguishes present values, explicit null, NaN and signed
infinity; rows absent from a field are not invented. `row_id` identifies the
field acquisition, not a common message ID shared by separate logging calls.

Typed recordings retain strings, booleans, arrays and protocol types. Old
scalar recordings and old escaped topic paths are readable, but omitted fields
cannot be recovered. Recorded annotations take precedence over installed
MAVLink dialect annotations; legacy fields use the installed dialect as a
fallback. Charts/statistics operate on finite numeric values, while quality
counts and sample inspection retain nonfinite/null values.

The loader reads one recording at a time into memory, with a default limit of
2,000,000 decoded chunk rows (including chart projections and static schemas).
For large files or a file containing multiple recording IDs, edit the load cell:

```python
run = load_rrd(path, recording_id="...", max_rows=10_000_000)
```

Restrict the load limit to available memory. Only timestamp and duration
clocks are exposed; sequence indexes are not interpreted as seconds.

Optional packages live in the `analysis` extra; they are not required by the
recorder or Mission Control app.
