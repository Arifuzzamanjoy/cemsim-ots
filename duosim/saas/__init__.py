"""Commercial SaaS layer: users, organisations, sessions, credits, plans, billing, audit, connectors.

Everything lives in the same SQLite database as the tenant store (`duosim.platform.store`) and uses
only the standard library plus `cryptography` (AES-GCM for workspace provider keys).
"""

from .core import SaaS, saas_for

__all__ = ["SaaS", "saas_for"]
