"""Tidesight: self-hosted web analytics that keeps every raw hit.

The collector modules (collect, storage, config) use the standard library
only; enrichment jobs may use optional libraries and degrade gracefully
when they are absent.
"""

__version__ = "0.1.0"
# Where Tidesight is published: the dashboard links to it, and updates.py looks there for new releases.
__repository__ = "https://github.com/mayerwin/Tidesight"
