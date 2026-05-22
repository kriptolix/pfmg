"""
pfmg.learn — standalone learning and import system.

Completely decoupled from the resolver pipeline.
Mines Flathub, imports shared-modules, probes SDKs, and writes
results directly into recipes/ and data/ — no knowledge graph.
"""

from pfmg.learn.importer import ModulesImporter, ImportReport
from pfmg.learn.inspector import Prober, ProbeResult


__all__ = [
    
    "ModulesImporter", "ImportReport",
    "Prober", "ProbeResult",    
]