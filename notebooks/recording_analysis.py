"""Reusable general analysis notebook.
Run: uv run --extra analysis marimo edit notebooks/recording_analysis.py
"""

import marimo

__generated_with = "0.25.1"
app = marimo.App(width="full")


@app.cell
def _():
    import sys
    from pathlib import Path
    import io
    import math
    import altair as alt
    import marimo as mo
    import polars as pl

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from backend.analysis.rrd import load_rrd
    from backend.paths import log_directory

    return Path, alt, io, load_rrd, log_directory, math, mo, pl


@app.cell
def _(mo):
    mo.md("""
    # Recording analysis
    Select a local **RRD**, inspect its annotated fields, and analyze exact samples.
    Tables preserve native types and nanosecond clocks. No resampling or forward-fill
    is applied. `state = null` means an explicitly recorded null; an absent sample
    means the field was not acquired at that time. NaN and infinities remain distinct.

    Duplicate this notebook for a particular experiment and add cells using
    `run`, `run.fields[path]`, and `filtered_samples`.
    """)
    return


@app.cell
def _(Path, log_directory, mo):
    _logs = log_directory()
    recording_picker = mo.ui.file_browser(
        initial_path=_logs if _logs.is_dir() else Path(__file__).resolve().parents[1],
        filetypes=[".rrd"], selection_mode="file", multiple=False,
        restrict_navigation=False, label="Choose a recording (.rrd)",
    )
    recording_picker
    return (recording_picker,)


@app.cell
def _(load_rrd, mo, recording_picker):
    mo.stop(not recording_picker.value, mo.md("Choose an RRD file in the file browser above."))
    selected_file = recording_picker.path()
    try:
        run = load_rrd(selected_file)
        load_error = None
    except Exception as _error:
        run = None
        load_error = str(_error)
    mo.stop(load_error is not None, mo.md(f"**Could not load recording:** {load_error}"))
    mo.stop(run.catalog.is_empty(), mo.md("This recording contains no acquired scalar or typed field samples."))
    mo.md(f"**{run.path.name}** · {run.catalog.height} fields · recording `{run.recording_id}`")
    return (run,)


@app.cell
def _(mo):
    catalog_search = mo.ui.text(label="Search field catalog", placeholder="field, message, unit, or description")
    catalog_search
    return (catalog_search,)


@app.cell
def _(catalog_search, mo, pl, run):
    catalog = run.catalog
    if catalog_search.value:
        _needle = catalog_search.value.lower()
        catalog = catalog.filter(pl.any_horizontal([
            pl.col(_column).str.to_lowercase().str.contains(_needle, literal=True)
            for _column in ["path", "unit", "enum", "description"]
        ]))
    mo.vstack([
        mo.md("## Field catalog\n`typed` fields retain decoded values; `legacy scalar` fields contain chart projections only."),
        mo.ui.table(catalog, selection=None, page_size=12),
    ])
    return (catalog,)


@app.cell
def _(catalog, mo, run):
    _options = catalog["path"].to_list()
    field_picker = mo.ui.dropdown(options=_options, value=_options[0] if _options else None,
                                  label="Inspect field", full_width=True)
    timeline_picker = mo.ui.dropdown(options=list(run.origins),
                                     value="mission" if "mission" in run.origins else next(iter(run.origins)),
                                     label="Clock")
    mo.hstack([field_picker, timeline_picker], widths=[3, 1])
    return field_picker, timeline_picker


@app.cell
def _(field_picker, mo, pl, run, timeline_picker):
    mo.stop(field_picker.value is None, mo.md("No fields match the catalog search."))
    selected_path = field_picker.value
    _description = run.catalog.filter(pl.col("path") == selected_path).row(0, named=True)
    _clock_column = timeline_picker.value + "_ns"
    mo.stop(_clock_column not in run.fields[selected_path].columns,
            mo.md("This field has no samples on this clock. Choose another clock."))
    samples = run.samples(selected_path, timeline=timeline_picker.value)
    mo.stop(samples.is_empty(), mo.md("This field has no samples on this clock."))
    mo.md(f"**{selected_path}** · `{_description['type']}` · {_description['unit']}\n\n"
          f"{_description['description']}\n\n"
          "Time is seconds from the recording's first field sample on the selected clock.")
    return samples, selected_path


@app.cell
def _(mo, samples):
    _minimum, _maximum = samples["time_s"].min(), samples["time_s"].max()
    time_window = mo.ui.range_slider(start=_minimum, stop=max(_maximum, _minimum + .001),
                                     step=max((_maximum - _minimum) / 1000, .000001),
                                     value=[_minimum, max(_maximum, _minimum + .001)],
                                     label="Time window (seconds)", full_width=True)
    state_filter = mo.ui.multiselect(options=["present", "null", "nan", "+inf", "-inf"],
                                      value=["present", "null", "nan", "+inf", "-inf"], label="Value states")
    text_filter = mo.ui.text(label="Text/value contains", placeholder="Optional literal match")
    lower_bound = mo.ui.text(label="Numeric minimum", placeholder="Optional")
    upper_bound = mo.ui.text(label="Numeric maximum", placeholder="Optional")
    array_index = mo.ui.number(start=0, step=1, value=0, label="Array element for plotting")
    mo.vstack([time_window, state_filter, text_filter,
               mo.hstack([lower_bound, upper_bound, array_index])])
    return (
        array_index,
        lower_bound,
        state_filter,
        text_filter,
        time_window,
        upper_bound,
    )


@app.cell
def _(
    lower_bound,
    math,
    mo,
    pl,
    samples,
    state_filter,
    text_filter,
    time_window,
    upper_bound,
):
    filtered_samples = samples.filter(
        pl.col("time_s").is_between(*time_window.value) & pl.col("state").is_in(state_filter.value)
    )
    if text_filter.value:
        _match = [text_filter.value.lower() in str(_value).lower() for _value in filtered_samples["value"].to_list()]
        filtered_samples = filtered_samples.filter(pl.Series(_match, dtype=pl.Boolean))
    _bounds = []
    try:
        for _text in [lower_bound.value, upper_bound.value]:
            _number = float(_text) if _text.strip() else None
            if _number is not None and not math.isfinite(_number):
                raise ValueError("Use finite bounds")
            _bounds.append(_number)
        filter_error = None
    except ValueError:
        filter_error = "Enter finite numbers for the numeric bounds."
    mo.stop(filter_error is not None, mo.md(filter_error or ""))
    if any(_bound is not None for _bound in _bounds):
        mo.stop(not filtered_samples["value"].dtype.is_numeric(),
                mo.md("Numeric bounds apply to scalar numeric fields. Clear them to inspect this field."))
        _predicate = pl.col("value").is_finite()
        if _bounds[0] is not None:
            _predicate &= pl.col("value") >= _bounds[0]
        if _bounds[1] is not None:
            _predicate &= pl.col("value") <= _bounds[1]
        filtered_samples = filtered_samples.filter(_predicate)
    return (filtered_samples,)


@app.cell
def _(filtered_samples, io, mo, pl, selected_path):
    _counts = filtered_samples.group_by("state").len().sort("state")
    _buffer = io.BytesIO()
    filtered_samples.write_parquet(_buffer)
    # Display strings to avoid JavaScript rounding uint64 values. The dataframe
    # and Parquet download retain their native value type.
    _display = filtered_samples.with_columns(
        pl.Series("value", [repr(_value) for _value in filtered_samples["value"].to_list()], dtype=pl.String)
    )
    mo.vstack([
        mo.md(f"## Samples\n**{filtered_samples.height:,}** samples after filtering."),
        mo.ui.table(_counts, selection=None),
        mo.ui.table(_display, selection=None, page_size=20),
        mo.download(_buffer.getvalue(), filename=selected_path.strip("/").replace("/", "_") + ".parquet",
                    label="Download filtered samples (Parquet)"),
    ])
    return


@app.cell
def _(alt, array_index, filtered_samples, math, mo, pl):
    _values = filtered_samples["value"].to_list()
    _numeric = []
    for _value in _values:
        if isinstance(_value, list):
            _index = int(array_index.value)
            _value = _value[_index] if _index < len(_value) else None
        _numeric.append(float(_value) if isinstance(_value, (int, float)) and math.isfinite(_value) else None)
    numeric_samples = filtered_samples.select("time_s", "state").with_columns(
        pl.Series("numeric_value", _numeric, dtype=pl.Float64))
    _finite = numeric_samples.filter(pl.col("numeric_value").is_not_null())
    if _finite.is_empty():
        _output = mo.md("## Plot\nThis selection has no finite numeric or boolean values. Inspect the sample table above.")
    else:
        # Points show actual acquisitions and do not draw across missing events.
        _chart = alt.Chart(_finite).mark_point(size=18).encode(
            x=alt.X("time_s:Q", title="Time (s)"), y=alt.Y("numeric_value:Q", title="Value (recorded units)"),
            tooltip=["time_s:Q", "numeric_value:Q", "state:N"],
        ).properties(height=300).interactive()
        _output = mo.vstack([mo.md("## Plot\nPlot values use floating point; use the typed table for exact large integers."),
                   mo.ui.altair_chart(_chart),
                   mo.ui.table(_finite.select(pl.col("numeric_value").min().alias("min"),
                                             pl.col("numeric_value").max().alias("max"),
                                             pl.col("numeric_value").mean().alias("mean"),
                                             pl.col("numeric_value").std().alias("std")), selection=None)])
    _output
    return


@app.cell
def _(mo, run):
    comparison_picker = mo.ui.multiselect(options=run.catalog["path"].to_list(), value=[],
                                           max_selections=6, label="Compare up to six signals", full_width=True)
    comparison_picker
    return (comparison_picker,)


@app.cell
def _(alt, comparison_picker, math, mo, pl, run, time_window, timeline_picker):
    _points = []
    for _path in comparison_picker.value:
        if timeline_picker.value + "_ns" not in run.fields[_path].columns:
            continue
        _frame = run.samples(_path, timeline=timeline_picker.value).filter(pl.col("time_s").is_between(*time_window.value))
        for _row in _frame.iter_rows(named=True):
            _value = _row["value"]
            if isinstance(_value, (int, float)) and math.isfinite(_value):
                _points.append(dict(time_s=_row["time_s"], value=float(_value), field=_path))
    if _points:
        _chart = alt.Chart(pl.DataFrame(_points)).mark_point(size=12).encode(
            x=alt.X("time_s:Q", title="Shared clock (s)"), y="value:Q", tooltip=["field:N", "time_s:Q", "value:Q"],
        ).properties(height=120, width=900).facet(row="field:N").resolve_scale(y="independent")
        _output = mo.vstack([mo.md("## Signal comparison\nExact events on a common clock; each field has its own value axis."),
                   mo.ui.altair_chart(_chart)])
    else:
        _output = mo.md("Select numeric fields to compare. Fields without samples on the selected clock are omitted.")
    _output
    return


@app.cell
def _(mo, run):
    metadata_picker = mo.ui.dropdown(options=list(run.metadata),
                                     value=next(iter(run.metadata), None), label="Message metadata")
    metadata_picker
    return (metadata_picker,)


@app.cell
def _(metadata_picker, mo, run):
    if metadata_picker.value:
        _output = mo.vstack([mo.md("## Message metadata\nOriginal direction, source identity, clock, and timestamps where recorded."),
                   mo.ui.table(run.metadata[metadata_picker.value], selection=None, page_size=12)])
    else:
        _output = mo.md("No message metadata was recorded in this file.")
    _output
    return


@app.cell
def _(mo):
    mo.md("""
    ## Your analysis
    Add cells below. Useful starting points:

    ```python
    # A typed table, including exact timestamps and explicit value state:
    servo = run.samples("/mavlink/SERVO_OUTPUT_RAW/servo3_raw", timeline="mission")

    # Keep real finite measurements only when appropriate for the calculation:
    finite = servo.filter((pl.col("state") == "present") & pl.col("value").is_finite())

    # Count events per second without inventing values between messages:
    rates = servo.group_by((pl.col("time_s").floor()).alias("second")).len().sort("second")
    ```

    For cross-message joins, choose a clock, an as-of direction, and a tolerance
    explicitly. Never interpret a sent command as an acknowledgment from the vehicle.
    For exact joins, use the integer `*_ns` columns rather than floating-point seconds.
    """)
    return


@app.cell
def _():
    return


if __name__ == "__main__":
    app.run()
