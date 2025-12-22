"""API package.

Define `bp` here and import submodules to register routes.
"""
from flask import Blueprint

bp = Blueprint('api', __name__, url_prefix='/api')

# Import submodules to register routes
from . import jobs  # noqa: F401

__all__ = ["bp"]
