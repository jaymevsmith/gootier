"""Shared Jhome Affiliates client instance for Gootier.

One module-level `AffiliatesClient` shared by the signup flow
(routes/auth_routes.py) and the Stripe webhook/checkout flow
(routes/stripe_routes.py) so both report to the same hub config without
each needing to construct their own client.

Reads JHOME_AFFILIATES_URL / JHOME_AFFILIATES_PRODUCT / JHOME_AFFILIATES_KEY
from the environment. No-ops safely if unset — see jhome_affiliates.py.
"""
from jhome_affiliates import AffiliatesClient

affiliates = AffiliatesClient()
