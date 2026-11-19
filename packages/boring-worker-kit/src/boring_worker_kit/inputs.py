"""Strict admission contracts; normalization follows validation, never replaces it."""
from __future__ import annotations

from typing import Annotated
from pydantic import Field

from .validation import (MAX_JOB_SECONDS as MAX_JOB_SECONDS, table as table, text as text,
                         integer as integer, strings as strings)

ExecTimeoutSeconds = Annotated[float, Field(strict=True, allow_inf_nan=False, ge=0.1)]
ReceiptLimit = Annotated[int, Field(strict=True, ge=1, le=100)]
# 0 means "look once"; the ceiling is this Pond's standalone (non-batch) cap.
# A job_wait step inside a batch has its own bound (batch_contracts).
JobWaitSeconds = Annotated[float, Field(strict=True, allow_inf_nan=False, ge=0, le=60)]
# Every job stops by this long: operations and Pirate jobs always, a command at
# the host task service's lifetime cap if that is shorter. There is no
# "unlimited".
JobTimeoutSeconds = Annotated[
    float, Field(strict=True, allow_inf_nan=False, ge=0.1, le=MAX_JOB_SECONDS)]
