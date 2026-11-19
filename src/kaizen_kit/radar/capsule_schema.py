"""Pydantic v2 Service Capsule & Federation data contracts."""

from datetime import datetime, timezone
from typing import Any, Literal
from pydantic import BaseModel, Field


class CapsuleManifest(BaseModel):
    service_name: str
    commit_sha: str
    languages: list[str]
    analyzed_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    node_count: int
    edge_count: int
    smell_count: int
    boundary_count: int = 0
    clone_count: int = 0
    embedding_backend: str = "offline:structural-768d"
    llm_backend: str = "deterministic"


class BoundaryItem(BaseModel):
    service_name: str
    direction: Literal["INBOUND", "OUTBOUND"]
    protocol: Literal["HTTP", "GRPC", "KAFKA", "SQL"]
    identifier: str
    rule_id: str
    file_path: str
    line: int


class SmellItem(BaseModel):
    rule_id: str
    severity: str
    message: str
    file_path: str
    line: int
    snippet: str = ""
    engine: str = "ast-grep"


class SemanticClonePair(BaseModel):
    a_id: str
    a_name: str
    a_service: str = ""
    a_lang: str = ""
    b_id: str
    b_name: str
    b_service: str = ""
    b_lang: str = ""
    similarity: float


class RankedTarget(BaseModel):
    symbol_id: str
    language: str
    name: str
    file_path: str
    start_line: int
    end_line: int
    complexity: int
    fan_in: int
    fan_out: int
    churn_90d: int
    hotspot_score: float
    smell_count: int
    clone_count: int
    roi_score: float
    clean_body: str = ""


class FederationSummary(BaseModel):
    analyzed_services: list[str]
    total_nodes: int = 0
    total_edges: int = 0
    cross_service_polyglot_clones: list[dict[str, Any]] = Field(default_factory=list)
    shared_contracts_or_tables: dict[str, list[str]] = Field(default_factory=dict)
    service_contract_links: list[dict[str, Any]] = Field(default_factory=list)
    distributed_monolith_tickets: list[dict[str, Any]] = Field(default_factory=list)

