"""Multi-tenancy seam.

`current_household()` is the FastAPI dependency every child-scoped query filters on. As of Phase
2a it resolves from the logged-in parent's session (tellybear.core.auth) instead of the
single seeded row — so household isolation is enforced automatically. Re-exported here so the
many routes that `from tellybear.core.deps import current_household` keep working unchanged.
"""
from __future__ import annotations

from tellybear.core.auth import (  # noqa: F401  (re-exported for routes)
    current_household,
    current_parent,
    require_admin,
)
