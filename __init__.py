"""Hermes plugin: hermes-evolve — thin shell for dual symlink + pip install."""

try:
    from hermes_evolve import register  # pip-installed
except ImportError:
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from hermes_evolve import register  # symlink / directory plugin
