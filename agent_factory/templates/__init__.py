from .base import ProjectTemplate
from .calculator import CALCULATOR_TEMPLATE

TEMPLATES: dict[str, ProjectTemplate] = {CALCULATOR_TEMPLATE.name: CALCULATOR_TEMPLATE}

__all__ = ["CALCULATOR_TEMPLATE", "TEMPLATES", "ProjectTemplate"]
