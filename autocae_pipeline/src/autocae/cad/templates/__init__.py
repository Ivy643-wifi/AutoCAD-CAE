"""Parametric CAD templates for all structural families."""
from autocae.cad.templates.base import BaseCADTemplate, CADResult
from autocae.cad.templates.flat_plate import FlatPlateTemplate
from autocae.cad.templates.open_hole_plate import OpenHolePlateTemplate
from autocae.cad.templates.cylindrical_shell import CylindricalShellTemplate
from autocae.cad.templates.laminated_beam import LaminatedBeamTemplate
from autocae.cad.templates.stringer_stiffened_panel import StringerStiffenedPanelTemplate
from autocae.cad.templates.sandwich_plate import SandwichPlateTemplate
from autocae.cad.templates.bolted_lap_joint import BoltedLapJointTemplate

__all__ = [
    "BaseCADTemplate",
    "CADResult",
    "FlatPlateTemplate",
    "OpenHolePlateTemplate",
    "CylindricalShellTemplate",
    "LaminatedBeamTemplate",
    "StringerStiffenedPanelTemplate",
    "SandwichPlateTemplate",
    "BoltedLapJointTemplate",
]
