"""Setup of Stadtwerke Amstetten: the e-mail address and password of its portal.

The portal is a mein-portal.at installation like that of E-Werk Gösting, so the
generic portal login (user name, password, then listing the Anlagen) is all the
provider needs.
"""
from __future__ import annotations

from ..const import PROVIDER_STADTWERKE_AMSTETTEN
from .base import ProviderFlow


class StadtwerkeAmstettenFlow(ProviderFlow):
    """Stadtwerke Amstetten logs in to its portal with user name and password."""

    provider = PROVIDER_STADTWERKE_AMSTETTEN
