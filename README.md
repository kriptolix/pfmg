# pfmg — Python Flatpak Module Generator

**pfmg** (Python Flatpak Module Generator) is a CLI tool for analyzing, exploring, and generating Flatpak build modules for Python packages.

The goal of the project is to automate and unify the process of dependency discovery, Flatpak runtime analysis, and generation of reusable “recipes” for installing Python packages inside Flatpak sandboxes.

The project is still under active development, but already provides functional commands for basic usage.

---

## Overview

pfmg operates as an analysis and generation pipeline divided into stages:

- inspection of runtimes and extensions  
- execution of tests in an isolated environment  
- dependency resolution  
- generation of reusable Flatpak modules  
- cataloging of recipes for future reuse  

These stages can be executed individually via CLI or combined into automated workflows.

---

## Key features

- Generate Flatpak modules from Python packages (`generate`)
- Import and catalog information from existing manifests and modules (`import`)
- Inspect available Flatpak SDKs and extensions (`inspect`)
- Search the local data catalog (`search`)
- Run packages in an isolated sandbox environment to analyze dependencies (`ingest`)
- Resolve dependencies based on errors and cataloged information (`resolve`)

---

## Architecture

The project is organized into independent components:

- **importer** — collects and processes information from manifests and modules  
- **inspector** — analyzes available Flatpak SDKs and extensions  
- **sandbox** — executes commands in an isolated environment to observe dependencies  
- **resolver** — interprets errors and maps required dependencies  
- **generator** — builds Flatpak modules from collected data  
- **data/** — local catalog of SDKs, extensions, and reusable recipes  

---

## General workflow

A typical pfmg workflow follows these steps:

1. Import or ingest packages  
2. Run them in a sandbox to analyze dependencies  
3. Inspect available SDKs and extensions  
4. Resolve missing requirements  
5. Generate the final Flatpak module  

---

## Project status

pfmg is still under active development.

- Core commands are already functional  
- Module generation (`generate`) is operational and in use  
- Data catalog and enrichment layer are under active development  

---

## Goal

The goal of pfmg is to reduce the manual effort required to create Flatpak manifests for Python projects by automating dependency analysis and making the process more predictable and reproducible.

---

## Name

The name **pfmg** stands for:

> Python Flatpak Module Generator  

---

## License

Licensed under GNU GPLv3