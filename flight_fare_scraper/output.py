import logging
from pathlib import Path
from typing import List

import pandas as pd

from .models import FlightResult

logger = logging.getLogger(__name__)


def to_dataframe(results: List[FlightResult]) -> pd.DataFrame:
    return pd.DataFrame([vars(r) for r in results])


def write_results(results: List[FlightResult], path: str) -> None:
    df = to_dataframe(results)
    if not len(df):
        logger.warning("no results to write to %s", path)
        return

    df = df.sort_values("price")
    suffix = Path(path).suffix.lower()

    if suffix == ".csv":
        df.to_csv(path, index=False)
    elif suffix == ".json":
        df.to_json(path, orient="records", indent=2)
    elif suffix in (".xlsx", ".xls"):
        df.to_excel(path, index=False)
    else:
        raise ValueError(f"Unsupported output extension '{suffix}' (use .csv, .json, or .xlsx)")

    logger.info("wrote %d results to %s", len(df), path)
