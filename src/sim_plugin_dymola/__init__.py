"""Dymola driver plugin for sim-cli.

This package is distributed as an out-of-tree plugin discovered by sim-cli
through Python entry points.
"""
from importlib.resources import files

from .driver import DymolaDriver

skills_dir = files(__name__) / "_skills"

plugin_info = {
    "name": "dymola",
    "summary": "Real-install alpha Dymola driver plugin for sim-cli.",
    "homepage": "https://github.com/svd-ai-lab/sim-plugin-dymola",
    "license_class": "commercial",
    "solver_name": "dymola",
    "status": "real_install_alpha",
}

__all__ = ["DymolaDriver", "skills_dir", "plugin_info"]
