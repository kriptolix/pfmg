"""
pfmg.learn — standalone learning and import system.

Completely decoupled from the resolver pipeline.
Mines Flathub, imports shared-modules, probes SDKs, and writes
results directly into recipes/ and data/ — no knowledge graph.
"""
from pfmg.learn.analyzer import ManifestAnalyzer, ManifestAnalysis
from pfmg.learn.importer import ModulesImporter, ImportReport
from pfmg.learn.inspector import Prober, ProbeResult
from pfmg.learn.exporter import Exporter, ExportReport

__all__ = [
    "ManifestAnalyzer", "ManifestAnalysis",
    "ModulesImporter", "ImportReport",
    "SDKProber", "ProbeResult",
    "Exporter", "ExportReport",
]