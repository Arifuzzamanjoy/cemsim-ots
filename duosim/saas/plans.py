"""Plans, credit prices and credit packs.

One credit is the unit every billable operation is priced in.  Prices are deliberately coarse so a user
can predict a bill: they are listed on the pricing page and returned by `GET /api/billing/plans`.
Operators can override any price with DUOSIM_CREDIT_COSTS='{"generate.deep": 12, ...}'.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field

log = logging.getLogger("duosim.saas")


@dataclass(frozen=True)
class Plan:
    id: str
    name: str
    price_usd: int | None  # per month; None = contact sales
    monthly_credits: int
    seats: int
    max_sessions: int
    features: tuple[str, ...] = ()
    blurb: str = ""
    highlights: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["features"] = list(self.features)
        d["highlights"] = list(self.highlights)
        return d


# features gate parts of the product: "byok" (own model keys), "webhooks", "cad_import", "image_input",
# "agent_deep" (multi-candidate agent with visual judge), "priority" (support / queue), "sso" (sales-led)
PLANS: dict[str, Plan] = {
    p.id: p
    for p in (
        Plan(
            "free",
            "Free",
            0,
            50,
            1,
            2,
            ("image_input", "cad_import"),
            "Try the studio and the simulators.",
            ("50 credits / month", "Text, image and CAD input", "Fast and standard agent", "Community support"),
        ),
        Plan(
            "pro",
            "Pro",
            29,
            1000,
            3,
            5,
            ("image_input", "cad_import", "agent_deep", "byok", "webhooks"),
            "For engineers who design every week.",
            (
                "1,000 credits / month",
                "Deep agent with visual judge",
                "Bring your own model keys",
                "Webhooks and API keys",
                "3 seats",
            ),
        ),
        Plan(
            "team",
            "Team",
            99,
            5000,
            15,
            20,
            ("image_input", "cad_import", "agent_deep", "byok", "webhooks", "priority"),
            "For engineering teams and training centres.",
            ("5,000 credits / month", "15 seats with roles", "Priority support", "Everything in Pro"),
        ),
        Plan(
            "enterprise",
            "Enterprise",
            None,
            20000,
            1000,
            100,
            ("image_input", "cad_import", "agent_deep", "byok", "webhooks", "priority", "sso"),
            "Private deployment, SSO and custom simulators.",
            ("Custom credit volume", "On-premise or private cloud", "SSO / SAML", "Custom simulator plug-ins"),
        ),
    )
}
DEFAULT_PLAN = "free"


@dataclass(frozen=True)
class Pack:
    id: str
    credits: int
    price_usd: int

    def to_dict(self) -> dict:
        return asdict(self)


PACKS: dict[str, Pack] = {p.id: p for p in (Pack("pack_500", 500, 15), Pack("pack_2000", 2000, 50))}

# credits per operation
_DEFAULT_COSTS: dict[str, int] = {
    "generate.fast": 2,  # one model call + validator repair
    "generate.standard": 4,  # plan + retrieval + generate + geometric critique/repair
    "generate.deep": 10,  # standard x2 candidates + build-verify + visual judge + repair
    "generate.byok": 1,  # any mode when the workspace pays the model provider itself
    "image": 2,  # surcharge per request with reference images
    "build": 1,  # FreeCAD build + verification + exports
    "import": 2,  # CAD / mesh import and primitive fitting
    "session": 1,  # one simulator session
}


def credit_costs() -> dict[str, int]:
    costs = dict(_DEFAULT_COSTS)
    raw = os.environ.get("DUOSIM_CREDIT_COSTS", "").strip()
    if raw:
        try:
            for k, v in json.loads(raw).items():
                if k in costs and isinstance(v, int) and v >= 0:
                    costs[k] = v
        except (ValueError, AttributeError):
            log.warning("DUOSIM_CREDIT_COSTS is not a JSON object of integers; using defaults")
    return costs


def generate_cost(mode: str, images: int, byok: bool) -> int:
    c = credit_costs()
    base = c["generate.byok"] if byok else c.get(f"generate.{mode}", c["generate.standard"])
    return base + (c["image"] if images else 0)


def plan(plan_id: str | None) -> Plan:
    return PLANS.get(plan_id or DEFAULT_PLAN, PLANS[DEFAULT_PLAN])


def stripe_price_ids() -> dict[str, str]:
    """{plan or pack id: Stripe price id} from DUOSIM_STRIPE_PRICE_<ID> (e.g. DUOSIM_STRIPE_PRICE_PRO)."""
    out = {}
    for key in (*PLANS, *PACKS):
        v = os.environ.get(f"DUOSIM_STRIPE_PRICE_{key.upper()}", "").strip()
        if v:
            out[key] = v
    return out
