"""Haldex flasher; adapter drivers are imported only when opened.

The shared protocol package is physically nested here for this standalone
flasher distribution.  Publish its canonical import name before any flasher
module loads so the source-identical Hudiy modules can keep importing
``vag_protocols``.
"""
import sys

from . import vag_protocols as _vag_protocols
from .vag_protocols import kwp as _kwp
from .vag_protocols import tp2 as _tp2

sys.modules.setdefault("vag_protocols", _vag_protocols)
sys.modules.setdefault("vag_protocols.kwp", _kwp)
sys.modules.setdefault("vag_protocols.tp2", _tp2)
