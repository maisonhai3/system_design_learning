import uuid


def spool_write(request_id: uuid.UUID, payload: dict) -> None:
    """Write payload to a local spool file named after request_id (e.g. {SPOOL_DIR}/{request_id}.json)."""
    raise NotImplementedError


def spool_read_all() -> list[tuple[uuid.UUID, dict]]:
    """Return (request_id, payload) for every file currently in the spool directory."""
    raise NotImplementedError


def spool_remove(request_id: uuid.UUID) -> None:
    """Delete the spool file for request_id, e.g. after it's been successfully published."""
    raise NotImplementedError
