"""Shared entity paths: slash-separated topics are hierarchies, not entity names."""
import rerun as rr


def topic_path(topic):
    if not topic.startswith("/"):
        topic = "/sensors/" + topic
    return "/" + "/".join(rr.escape_entity_path_part(part) for part in topic.strip("/").split("/"))


def field_path(topic, field):
    return f"{topic_path(topic)}/{rr.escape_entity_path_part(field)}"


def geographic_path(topic, field):
    return f"/geo/readings{field_path(topic, field)}"
