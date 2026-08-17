"""
Resolves the {org_id} placeholder in communication.request_id_format.

Why this lives in the compiler and not in the wizard (new_org.py), nor
is left to whoever writes org.yaml by hand: if the resolution only
happened in one place (e.g. an f-string in new_org.py substituting
{org_id} when creating the file), any org.yaml written or edited by hand
through any other path would keep the unresolved placeholder — which is
exactly the real bug that appeared when comparing the org.yaml of
Aquaponics United / United Capital Group (written by hand) against what
`po new-org` generates.

With the resolution centralized here, it doesn't matter how the org.yaml
originated: the generated SOUL.md always carries the real org_id
substituted.

{yyyymmdd} and {seq4} are deliberately left literal: they are not
something PhantomOrg should resolve at compile time, they are the
format instruction that the agent/runtime itself uses to generate a real
Request-ID during operation (the date and the sequence number do not
exist yet at build time).
"""

from __future__ import annotations

from ..spec.model import OrgSpec


def resolve_request_id_format(spec: OrgSpec) -> str:
    return spec.communication.request_id_format.replace(
        "{org_id}", spec.organization.id
    )
