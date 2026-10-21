"""One strict, discriminated admission contract for public and worker batches."""
from __future__ import annotations

import json
import re
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator

from .framing import MAX_COMMAND_BYTES, MAX_FRAME_BYTES
from .inputs import ExecTimeoutSeconds, JobTimeoutSeconds, ReceiptLimit
from .outcomes import Refusal

# A wait step is bounded by the batch's own overall ceiling, not the standalone
# tool's 0..60 default-45 policy: the remaining batch deadline clamps it further
# at run time (WorkerServer.run_batch_step), matching "inside a batch: 0 up to
# the remaining batch time" from the wait decision.
BatchWaitSeconds = Annotated[float, Field(strict=True, allow_inf_nan=False, ge=0, le=103)]

BATCH_MAX_STEPS = 24
BATCH_RESULT_BYTES = 768 * 1024
BATCH_INPUT_BYTES = 768 * 1024
_ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,31}\Z")


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ReadArgs(Arguments):
    root: str
    path: str
    start_line: Annotated[int, Field(strict=True, ge=1)] = 1
    line_count: Annotated[int, Field(strict=True, ge=1, le=10000)] = 1000


Encoding = Literal["utf-8", "utf-16-le", "utf-16-be"]
Newline = Literal["lf", "crlf"]


class WriteArgs(Arguments):
    root: str
    path: str
    content: str
    expected_sha256: str | None
    encoding: Encoding | None = None
    bom: bool | None = None
    newline: Newline | None = None


class Edit(Arguments):
    old_text: str = Field(min_length=1)
    new_text: str


class PatchArgs(Arguments):
    root: str
    path: str
    expected_sha256: str
    edits: list[Edit] = Field(min_length=1, max_length=128)
    newline: Newline | None = None


class ReadBytesArgs(Arguments):
    root: str
    path: str
    offset: Annotated[int, Field(strict=True, ge=0)] = 0
    length: Annotated[int, Field(strict=True, ge=1, le=65536)] = 16384


class WriteBytesArgs(Arguments):
    root: str
    path: str
    data: str = Field(max_length=87384)
    expected_sha256: str | None
    offset: Annotated[int, Field(strict=True, ge=0)] | None = None


MAX_SEARCH_MATCHES = 100
SearchLimit = Annotated[int, Field(strict=True, ge=1, le=MAX_SEARCH_MATCHES)]


class ListArgs(Arguments):
    root: str
    path: str = "."
    limit: Annotated[int, Field(strict=True, ge=1, le=500)] = 100


class SearchArgs(ListArgs):
    query: str = Field(min_length=1, max_length=1024)
    limit: SearchLimit = MAX_SEARCH_MATCHES


class ExecArgs(Arguments):
    command: str = Field(min_length=1)
    cwd: str | None = None
    timeout_seconds: ExecTimeoutSeconds | None = None

    @field_validator("command")
    @classmethod
    def command_bytes(cls, value: str) -> str:
        if len(value.encode("utf-8")) > MAX_COMMAND_BYTES:
            raise ValueError("command exceeds byte limit")
        return value


class JobStartArgs(ExecArgs):
    timeout_seconds: JobTimeoutSeconds | None = None


class JobListArgs(Arguments):
    limit: ReceiptLimit = 20


class JobSelectorArgs(Arguments):
    job_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class JobWaitArgs(Arguments):
    job_ids: list[str] = Field(min_length=1, max_length=32)
    wait_seconds: BatchWaitSeconds = 45


class StepBase(Arguments):
    id: str

    @field_validator("id")
    @classmethod
    def identifier(cls, value: str) -> str:
        if not _ID.fullmatch(value):
            raise ValueError("invalid step ID")
        return value


class ReadStep(StepBase):
    method: Literal["file_read"]
    arguments: ReadArgs


class WriteStep(StepBase):
    method: Literal["file_write"]
    arguments: WriteArgs


class PatchStep(StepBase):
    method: Literal["file_patch"]
    arguments: PatchArgs


class ReadBytesStep(StepBase):
    method: Literal["file_read_bytes"]
    arguments: ReadBytesArgs


class WriteBytesStep(StepBase):
    method: Literal["file_write_bytes"]
    arguments: WriteBytesArgs


class ListStep(StepBase):
    method: Literal["file_list"]
    arguments: ListArgs


class SearchStep(StepBase):
    method: Literal["file_search"]
    arguments: SearchArgs


class ExecStep(StepBase):
    method: Literal["exec"]
    arguments: ExecArgs


class JobStartStep(StepBase):
    method: Literal["job_start"]
    arguments: JobStartArgs


class JobListStep(StepBase):
    method: Literal["job_list"]
    arguments: JobListArgs


class JobStatusStep(StepBase):
    method: Literal["job_status"]
    arguments: JobSelectorArgs


class JobOutputStep(StepBase):
    method: Literal["job_output"]
    arguments: JobSelectorArgs


class JobStopStep(StepBase):
    method: Literal["job_stop"]
    arguments: JobSelectorArgs


class JobWaitStep(StepBase):
    method: Literal["job_wait"]
    arguments: JobWaitArgs


class ServiceListStep(StepBase):
    method: Literal["service_list"]
    arguments: Arguments = Field(default_factory=Arguments)


BatchStep = Annotated[Union[ReadStep, WriteStep, PatchStep, ReadBytesStep, WriteBytesStep, ListStep, SearchStep,
                             ExecStep, JobStartStep, JobListStep, JobStatusStep,
                             JobOutputStep, JobStopStep, JobWaitStep, ServiceListStep],
                    Field(discriminator="method")]


class BatchRequest(Arguments):
    operations: list[BatchStep] = Field(min_length=1, max_length=BATCH_MAX_STEPS)
    on_error: Literal["stop", "continue"] = "stop"
    timeout_seconds: Annotated[float, Field(strict=True, allow_inf_nan=False, ge=0.1, le=103)] | None = None

    @field_validator("operations")
    @classmethod
    def unique_ids(cls, value: list[BatchStep]) -> list[BatchStep]:
        if len({op.id for op in value}) != len(value):
            raise ValueError("step IDs must be unique")
        return value


def validation_fields(error: ValidationError) -> list[str]:
    """Dotted names of the rejected fields -- never the rejected values."""
    fields: list[str] = []
    for item in error.errors(include_url=False, include_input=False, include_context=False):
        name = ".".join(str(part) for part in item["loc"]) or "(request)"
        if name not in fields:
            fields.append(name)
    return fields


def invalid_request(error: ValidationError, message: str) -> Refusal:
    return Refusal("invalid_request", message, fields=validation_fields(error))


def admit_batch(raw: object) -> BatchRequest:
    # Limit the fully escaped input, including repeated payloads, before dispatch.
    if len(json.dumps(raw, ensure_ascii=True, allow_nan=False).encode()) > min(BATCH_INPUT_BYTES, MAX_FRAME_BYTES):
        raise Refusal("limit", "batch input exceeds limit")
    try:
        return BatchRequest.model_validate(raw)
    except ValidationError as error:
        raise invalid_request(error, "invalid batch request") from None


_STEP_ADAPTER: TypeAdapter[BatchStep] = TypeAdapter(BatchStep)


def admit_single(method: str, arguments: dict[str, object]) -> BatchStep:
    try:
        return _STEP_ADAPTER.validate_python({"id": "direct", "method": method,
                                             "arguments": arguments})
    except ValidationError as error:
        raise invalid_request(error, f"invalid {method} request") from None
