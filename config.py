"""Central web configuration.

Change the defaults for local use or provide environment variables with the
same names. Keep production passwords and session secrets outside Git.
"""

import os


def _admin_path(value: str) -> str:
    path = value.strip()
    if not path.startswith("/") or path == "/" or any(char in path for char in "?# "):
        raise ValueError(
            "ADMIN_BASE_PATH must start with '/', must not be '/', and cannot contain spaces, '?' or '#'."
        )
    return path.rstrip("/")


APP_ENV = os.getenv("APP_ENV", "development").lower()
ADMIN_BASE_PATH = _admin_path(os.getenv("ADMIN_BASE_PATH", "/admin"))
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "change-me-for-local-development")
SESSION_SECRET_KEY = os.getenv(
    "SESSION_SECRET_KEY", "development-only-session-secret"
)

if APP_ENV == "production" and (
    ADMIN_PASSWORD == "change-me-for-local-development"
    or SESSION_SECRET_KEY == "development-only-session-secret"
):
    raise RuntimeError(
        "Set a unique ADMIN_PASSWORD and SESSION_SECRET_KEY before production startup."
    )
