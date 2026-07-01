"""FAOSTATdb — a local, source-preserving DuckDB mirror of FAOSTAT bulk data.

This package downloads FAOSTAT bulk ZIP archives, validates them, and imports
each dataset into a DuckDB database as one fact table per dataset (``data_<code>``),
preserving the statistical content of FAOSTAT exactly (flags retained, values
unaltered) while recording reproducibility metadata.

See ``FAOSTATdb.md`` for the design rationale and ``PLAN.md`` for the build plan.
"""

__version__ = "0.2.0"

__all__ = ["__version__"]
