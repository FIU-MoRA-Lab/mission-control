"""Mission editing and action facade, independent of desktop widgets."""
from backend.mapping.projection import MapViewport
from boats.heron.backend.input.manual_control import ManualControlState

class MissionSession:
    """Application-level facade shared by the desktop and headless clients."""

    def __init__(self, controller, viewport=None):
        self.controller = controller
        self.viewport = viewport or MapViewport()
        self.manual = ManualControlState(controller)

    def waypoints_snapshot(self):
        with self.controller._wp_lock:
            return list(self.controller.waypoints)

    def nearest_waypoint(self, px, py, width, height, radius=20):
        nearest, distance = -1, radius
        for index, wp in enumerate(self.waypoints_snapshot()):
            x, y = self.viewport.geo_to_screen(wp["lat"], wp["lon"], width, height)
            candidate = math.hypot(x - px, y - py)
            if candidate < distance:
                nearest, distance = index, candidate
        return nearest

    def add_waypoint_at(self, px, py, width, height):
        lat, lon = self.viewport.screen_to_geo(px, py, width, height)
        self.controller.add_waypoint(lat, lon)

    def move_waypoint_to(self, index, px, py, width, height):
        lat, lon = self.viewport.screen_to_geo(px, py, width, height)
        self.controller.update_waypoint_pos(index, lat, lon)

    def remove_waypoint(self, index):
        self.controller.remove_waypoint(index)

    def upload_mission(self): self.controller.upload_mission_async()
    def start_mission(self): self.controller.start_mission_async()
    def emergency_stop(self):
        self.manual.zero(send_neutral=False)
        self.controller.emergency_stop()
    def arm(self): self.controller.set_arm(True)
    def disarm(self): self.controller.set_arm(False)
    def set_manual_mode(self): self.controller.set_mode("MANUAL")
    def clear_waypoints(self): self.controller.clear_waypoints()
    def load_waypoints(self, path): return self.controller.load_waypoints_file(path)
    def save_waypoints(self, path): return self.controller.save_waypoints_file(path)
