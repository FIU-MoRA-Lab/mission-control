# Custom FIU map prototype

The opt-in `FiuMap` view is registered through Rerun's public `add_view_class`
API. Standard Cargo `walkers` supplies offline tiles, Web Mercator projection,
and pan/zoom. Application code lives in `frontend/custom_map/`; it does not
import private Rerun map modules or copy Rerun's visualizers.

Run from the repository root:

```sh
MISSION_CONTROL_CUSTOM_MAP=1 uv run mission-control --boat heron --demo
MISSION_CONTROL_CUSTOM_MAP=1 uv run mission-control --boat heron --demo --recording assets/demo.mcap
```

The launcher builds the source frontend for this option, even when a release
binary exists. Remove the environment variable to use the current map.
The current map fork is retained for comparison; this prototype does not yet
remove its Cargo patch.

## Scope

- Authenticated local FIU tiles; no online tile provider.
- Existing route toolbar, click/add, drag/move, right-click/delete, and safety
  gates. Replay cannot edit the live route.
- GeoPoints and GeoLineStrings queried through Rerun's
  `query_archetype_with_history` API. The selected recording, timeline cursor,
  view content filter, and visible history range control the rendered data.
- Logged colors, transparency, and radii, including meter-sized radii.
- Stable view identity across field visibility changes, with a separate ID from
  the standard map to prevent restored blueprint state collisions; separate camera state
  for each view instance; double-click resets to the FIU center and zoom 19.

## Differences to evaluate before replacing the existing map

The prototype draws through egui rather than Rerun's GPU point/line renderers.
It currently has no entity picking/selection highlights, annotation-derived
colors, persisted blueprint zoom, automatic fit-to-data, or map zoom settings
in the selection panel. Large sensor histories may cost more CPU and memory.
The map initially centers on FIU rather than following the geographic data span.
Rerun's Rust extension interfaces are version-specific; this remains pinned to
0.38.1 and will need checking during upgrades.

Compare these interactions in demo mode before adopting the prototype:

1. Confirm imagery loads and GPS/lake/route overlays align when panning/zooming.
2. Add, drag, and delete waypoints, then start a simulated route.
3. Open the bundled replay, scrub the mission timeline, and confirm vehicle and
   sensor samples change with the cursor and visible history range.
4. Switch replay/live recordings and toggle sensor fields. Confirm editing is
   disabled in replay and each map retains its camera during layer changes.
5. Compare responsiveness with all available sensor layers and long history.

Retain the existing map until interactive behavior and performance are checked.

## Validation in this checkout

The prototype builds successfully with Cargo. All 49 Python tests passed with
`MISSION_CONTROL_CUSTOM_MAP=1`; these include recording export/verification,
loopback bridge behavior, safety gates, and the prototype blueprint checks.
The Rust pointer-event regression test verifies geographic waypoint dragging,
that a drag does not also add a point, and that replay cannot send route edits.

Native demo checks confirmed FIU imagery/geometry alignment, waypoint addition
and deletion, camera reset, replay track rendering, cursor-driven vehicle
movement, disabled replay editing, and layer toggling without a camera reset.
The demo replay ran near 60 FPS. Native drag automation did not reliably
produce a move, so drag gestures still need a manual check; the headless
pointer-event regression passes. Full-history performance and entity picking
remain evaluation work before replacing the current map.

The native debug build reports Apple's compact-unwind size warning but links
successfully. The missing `assets/demo.mcap` fixture was restored from the
original Heron synthetic FIU recording to run the existing replay tests.
