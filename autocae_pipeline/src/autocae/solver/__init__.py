"""Solver module — Solver Adapter + CalculiX integration."""
from autocae.solver.base import BaseSolverAdapter
from autocae.solver.calculix import CalculiXAdapter
from autocae.solver.runner import SolverRunner

__all__ = ["BaseSolverAdapter", "CalculiXAdapter", "SolverRunner"]
