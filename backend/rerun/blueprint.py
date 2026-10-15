"""Checkbox-driven field visibility, with simultaneous charts and explicit map layers."""
import uuid
import os
import rerun.blueprint as rrb
from backend.rerun.schema import default_fields, field_catalog
from backend.rerun.presets import PRESETS, canonical_path
import rerun as rr


def stable(view, key):
    # Keep view identity across visibility changes, including the map camera.
    view.id = uuid.uuid5(uuid.NAMESPACE_URL, f"mission-control/{key}")
    return view


def blueprint(*, catalog=None, visible_fields=None, preset="fields"):
    if preset not in PRESETS:
        raise ValueError("Choose a chart preset")
    catalog = field_catalog({}) if catalog is None else catalog
    selected = set(default_fields(catalog) if visible_fields is None else visible_fields)
    enabled = [field for field in catalog if field["id"] in selected]
    map_contents = ["/geo/vehicle/**", "/geo/track/**", "/geo/lake/**", "/geo/route/**"]
    map_contents.extend(f"{field['map_path']}/**" for field in enabled if field.get("map_path"))
    views = []
    grouped = set()
    for title, paths in PRESETS[preset][1]:
        members = [field for field in enabled if canonical_path(field) in paths and field.get("data_type") not in {"bool", "state"}]
        if not members:
            continue
        grouped.update(field["id"] for field in members)
        overrides = {}
        for field in members:
            label = field.get("display_label", field["label"])
            if field.get("direction"):
                label = f"{field['direction']} · {label}"
            colors = ([100, 190, 250], [255, 180, 70], [100, 220, 160], [210, 130, 250], [255, 100, 120], [240, 220, 100], [140, 220, 240], [230, 150, 200])
            overrides[field["path"]] = rr.SeriesLines(names=label, colors=colors[paths.index(canonical_path(field)) % len(colors)], interpolation_mode="StepAfter")
        views.append(stable(rrb.TimeSeriesView(origin="/", contents=[field["path"] for field in members], name=title,
                                               overrides=overrides), "preset/" + title))
    for field in enabled:
        if field["id"] in grouped:
            continue
        if field.get("data_type") in {"bool", "state"}:
            continue
        if preset in {"states", "activity"} and field.get("data_type") in {"bool", "text"}:
            continue
        title = field.get("display_label", field["label"]) + (f" · {field['unit']}" if field["unit"] else "")
        if field["kind"] != "telemetry":
            title = f"{field['group'].removeprefix('Raw · ').removeprefix('Sensors · ')} · {title}"
        view_class = rrb.DataframeView if field.get("data_type") == "text" else rrb.TimeSeriesView
        views.append(stable(view_class(origin="/", contents=[field["path"]], name=title), field["id"]))
    if os.environ.get("MISSION_CONTROL_CUSTOM_MAP") == "1":
        map_view = stable(rrb.View(class_identifier="FiuMap", origin="/geo",
                                   contents=map_contents, name="FIU · custom map prototype"), "custom-map")
    else:
        map_view = stable(rrb.MapView(origin="/geo", contents=map_contents,
                                     name="FIU · offline satellite", zoom=19), "map")
    activity = stable(rrb.TextLogView(origin="/events", contents=["/events/status", "/events/messages", "/events/state_changes/**"], name="Activity & state changes"), "activity")
    boolean_paths = [field["path"] for field in enabled if field.get("data_type") in {"bool", "state"}]
    if boolean_paths and preset != "state_changes":
        views.append(stable(rrb.StateTimelineView(origin="/", contents=boolean_paths, name="Boolean & categorical states"), "booleans"))
    # Presets group related signals; other selected fields retain individual charts.
    charts = [rrb.Horizontal(*views[i:i+3]) for i in range(0, len(views), 3)]
    parts = [map_view, *charts, activity]
    shares = [4, *([2] * len(charts)), 1.5 if preset in {"activity", "states"} else .7]
    if preset == "state_changes":
        parts.insert(1, stable(rrb.StateTimelineView(origin="/", contents=["/state_changes/**", *boolean_paths], name="State changes"), "state-timeline"))
        shares.insert(1, 4)
    if preset in {"states", "activity"}:
        parts.append(stable(rrb.TextLogView(origin="/events/errors", name="Warnings & errors"), "errors"))
        shares.append(1.5)
        message_paths = [field["path"] for field in enabled if field.get("data_type") in {"bool", "text"}]
        if message_paths:
            parts.append(stable(rrb.DataframeView(origin="/", contents=message_paths, name="Boolean & text message data"), "message-data"))
            shares.append(1.5)
    return rrb.Blueprint(rrb.Vertical(*parts, row_shares=shares),
                         collapse_panels=True, auto_views=False, auto_layout=False)
