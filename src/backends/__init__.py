from .base import RouterBackend
from .tr064 import TR064Backend

BACKENDS = {
    "tr064": TR064Backend,
}

__all__ = ["RouterBackend", "TR064Backend", "BACKENDS"]
