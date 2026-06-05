from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class SearchRequest(BaseModel):
    log_type: Optional[Literal["dns", "deep_kernel", "standard_host"]] = None
    sus: Optional[Literal[0, 1]] = None
    evil: Optional[Literal[0, 1]] = None
    host_name: Optional[str] = None
    process_name: Optional[str] = None
    dns_query: Optional[str] = None
    timestamp_from: Optional[str] = Field(None, json_schema_extra={"example": "2021-05-16T00:00:00Z"})
    timestamp_to: Optional[str] = Field(None, json_schema_extra={"example": "2021-05-17T00:00:00Z"})
    page: int = Field(1, ge=1)
    page_size: int = Field(20, ge=1, le=500)


class Hit(BaseModel):
    id: str
    score: Optional[float]
    source: dict[str, Any]


class SearchResponse(BaseModel):
    total: int
    page: int
    page_size: int
    took_ms: int
    hits: list[Hit]
