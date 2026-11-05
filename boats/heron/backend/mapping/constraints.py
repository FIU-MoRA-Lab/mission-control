"""Local metric geometry and continuous segment clearance checks (no sampling)."""
import hashlib
import json
import math
from boats.heron.backend.mapping.lake import LakeSafetyGeometry
from boats.heron.backend.control.planning import Waypoint


def xy(point, origin):
    return LakeSafetyGeometry._local_xy(point.lat, point.lon, origin.lat, origin.lon)


def waypoint(point, origin):
    return Waypoint(*LakeSafetyGeometry._from_local_xy(*point, origin.lat, origin.lon))


def projection(point, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    length2 = dx * dx + dy * dy
    u = ((point[0] - a[0]) * dx + (point[1] - a[1]) * dy) / length2 if length2 else 0
    u = max(0, min(1, u))
    closest = a[0] + u * dx, a[1] + u * dy
    return u, math.dist(point, closest)


def cross(a, b, c):
    return (b[0]-a[0])*(c[1]-a[1]) - (b[1]-a[1])*(c[0]-a[0])


def intersects(a, b, c, d):
    ab_c, ab_d, cd_a, cd_b = cross(a,b,c), cross(a,b,d), cross(c,d,a), cross(c,d,b)
    if ab_c * ab_d < 0 and cd_a * cd_b < 0:
        return True
    return any(abs(area) < 1e-8 and projection(p, x, y)[1] < 1e-7
               for area, p, x, y in ((ab_c,c,a,b),(ab_d,d,a,b),(cd_a,a,c,d),(cd_b,b,c,d)))


def segment_distance(a, b, c, d):
    if intersects(a,b,c,d):
        return 0
    return min(projection(a,c,d)[1], projection(b,c,d)[1], projection(c,a,b)[1], projection(d,a,b)[1])


class OperatingArea:
    """Source shoreline inset by clearance, with circular fountain exclusions.

    Validate against source edges rather than the map's approximate offset polygon.
    The lake padding is total clearance; callers configure it for footprint/uncertainty.
    """
    def __init__(self, geojson):
        self.geojson = json.loads(json.dumps(geojson, allow_nan=False))
        self.revision = hashlib.sha256(json.dumps(self.geojson, sort_keys=True).encode()).hexdigest()[:16]
        self.padding = float(geojson.get("padding_m", 0))
        if not math.isfinite(self.padding) or self.padding < 0:
            raise ValueError("Invalid operating-area padding")
        rings, centers = [], []
        for feature in geojson.get("features", []):
            role = feature.get("properties", {}).get("role")
            geom = feature["geometry"]
            if role == "shoreline" and geom["type"] == "Polygon":
                rings.append([Waypoint(lat=p[1], lon=p[0]) for p in geom["coordinates"][0]])
            elif role == "fountain_center" and geom["type"] == "Point":
                centers.append(Waypoint(lat=geom["coordinates"][1], lon=geom["coordinates"][0]))
        if len(rings) != 1 or len(rings[0]) < 4:
            raise ValueError("Lake shoreline geometry is unavailable")
        self.origin = rings[0][0]
        self.ring = [xy(p, self.origin) for p in rings[0]]
        if self.ring[-1] == self.ring[0]:
            self.ring.pop()
        self.edges = list(zip(self.ring, self.ring[1:] + self.ring[:1]))
        if any(math.dist(a,b) < 1e-6 for a,b in self.edges):
            raise ValueError("Lake shoreline has duplicate vertices")
        if abs(sum(a[0]*b[1]-b[0]*a[1] for a,b in self.edges)) < 1e-4:
            raise ValueError("Lake shoreline is degenerate")
        for i, (a,b) in enumerate(self.edges):
            for j, (c,d) in enumerate(self.edges):
                if j > i + 1 and not (i == 0 and j == len(self.edges)-1) and intersects(a,b,c,d):
                    raise ValueError("Lake shoreline intersects itself")
        self.centers = [xy(p, self.origin) for p in centers]

    def inside(self, point):
        x,y = point
        result = False
        for (ax,ay),(bx,by) in self.edges:
            if (ay > y) != (by > y) and x < (bx-ax)*(y-ay)/(by-ay)+ax:
                result = not result
        return result

    def segment_issue(self, start, end):
        a,b = xy(start,self.origin), xy(end,self.origin)
        if not self.inside(a) or not self.inside(b):
            return "outside_lake"
        if any(segment_distance(a,b,c,d) <= self.padding + 1e-7 for c,d in self.edges):
            return "shoreline_clearance"
        if any(projection(center,a,b)[1] <= self.padding + 1e-7 for center in self.centers):
            return "fountain_exclusion"
        return None

    def validate(self, plan):
        issues = []
        for index, leg in enumerate(plan.legs):
            if math.dist(xy(leg.start,self.origin), xy(leg.end,self.origin)) <= 2 * plan.arrival_radius_m:
                issues.append({"leg": index, "reason": "leg_too_short_for_arrival_radius"})
            reason = self.segment_issue(leg.start, leg.end)
            if reason:
                issues.append({"leg": index, "reason": reason})
        return {"valid": not issues, "issues": issues, "area_revision": self.revision}
