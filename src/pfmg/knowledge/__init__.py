"""
pfmg.learn — standalone learning and import system.

Mines Flathub, imports shared-modules, probes SDKs, and writes
results directly into data.
"""
from src.pfmg.knowledge.importer import ModulesImporter, ImportReport
from src.pfmg.knowledge.inspector import RuntimeInspector, InspectionResult

__all__ = [
    "ModulesImporter",
    "ImportReport",
    "RuntimeInspector",
    "InspectionResult",
]