"""Moved to app.core.chat_store (the API must not depend on the UI package); kept as an alias for old imports."""
import sys

from app.core import chat_store as _chat_store

sys.modules[__name__] = _chat_store
