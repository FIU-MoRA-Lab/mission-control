"""Shared geographic view defaults, independent of boat fields and layouts."""
import os
import uuid
import rerun.blueprint as rrb


def stable(view, key):
    # Keep view identity across visibility changes, including the map camera.
    view.id = uuid.uuid5(uuid.NAMESPACE_URL, f"mission-control/{key}")
    return view


def map_view(contents):
    if os.environ.get("MISSION_CONTROL_CUSTOM_MAP") == "1":
        return stable(rrb.View(class_identifier="FiuMap", origin="/geo",
                               contents=contents, name="FIU · custom map prototype"), "custom-map")
    return stable(rrb.MapView(origin="/geo", contents=contents,
                             name="FIU · offline satellite", zoom=19), "map")
