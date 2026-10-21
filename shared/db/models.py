from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import (
    UUID as SQLUUID,
)
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import (
    ARRAY,
    INET,
    JSONB,
    TIMESTAMP,
)
from sqlalchemy.dialects.postgresql import (
    ENUM as PGEnum,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
)


class Base(DeclarativeBase):
    pass


# ============================================================
# Enums
# ============================================================


class JobStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class TaskStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    VOTING = "VOTING"
    VERIFIED = "VERIFIED"
    DISPUTED = "DISPUTED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ReplicaStatus(str, Enum):
    SCHEDULED = "SCHEDULED"
    RUNNING = "RUNNING"
    EVICTED = "EVICTED"
    LOST = "LOST"
    DONE = "DONE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class NodeState(str, Enum):
    IDLE = "IDLE"
    BUSY = "BUSY"
    OFFLINE = "OFFLINE"


class UserRole(str, Enum):
    USER = "USER"
    ADMIN = "ADMIN"


# PostgreSQL-native enum types.
job_status_enum = PGEnum(
    JobStatus,
    name="job_status",
    create_type=True,
)

task_status_enum = PGEnum(
    TaskStatus,
    name="task_status",
    create_type=True,
)

replica_status_enum = PGEnum(
    ReplicaStatus,
    name="replica_status",
    create_type=True,
)

node_state_enum = PGEnum(
    NodeState,
    name="node_state",
    create_type=True,
)

user_role_enum = PGEnum(
    UserRole,
    name="user_role",
    create_type=True,
)


# ============================================================
# Node
# ============================================================


class Node(Base):
    __tablename__ = "nodes"

    name: Mapped[str] = mapped_column(
        Text,
        primary_key=True,
    )

    trust_score: Mapped[Decimal] = mapped_column(
        Numeric(4, 3),
        nullable=False,
        server_default=text("0.5"),
    )

    state: Mapped[NodeState] = mapped_column(
        node_state_enum,
        nullable=False,
        server_default=text("'OFFLINE'"),
    )

    last_heartbeat: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=True,
    )

    total_agree: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
    )

    total_disagree: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
    )

    cpu_percent: Mapped[float | None] = mapped_column(
        Float,
        nullable=True,
    )

    replicas: Mapped[list["Replica"]] = relationship(
        back_populates="node",
    )

    events: Mapped[list["Event"]] = relationship(
        back_populates="node",
    )

    __table_args__ = (
        CheckConstraint(
            "trust_score BETWEEN 0 AND 1",
            name="nodes_trust_score_check",
        ),
    )


# ============================================================
# User
# ============================================================


class User(Base):
    __tablename__ = "users"

    id: Mapped[UUID] = mapped_column(
        SQLUUID(as_uuid=True),
        server_default=text("gen_random_uuid()"),
        primary_key=True,
    )

    email: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        unique=True,
    )

    password_hash: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    role: Mapped[UserRole] = mapped_column(
        user_role_enum,
        nullable=False,
        server_default=text("'USER'"),
    )

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )

    __table_args__ = (
        CheckConstraint(
            "email = lower(email)",
            name="users_email_check",
        ),
    )


class UserSession(Base):
    __tablename__ = "user_sessions"

    token_hash: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
    )

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey(
            "users.id",
            ondelete="CASCADE",
        ),
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )

    expires_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
    )


# ============================================================
# Job Type
# ============================================================


class JobType(Base):
    __tablename__ = "job_types"

    name: Mapped[str] = mapped_column(
        Text,
        primary_key=True,
    )

    description: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        server_default=text("''"),
    )

    image: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    command: Mapped[list[str] | None] = mapped_column(
        ARRAY(Text),
        nullable=True,
    )

    default_cpu: Mapped[int] = mapped_column(  # cores
        Integer,
        nullable=False,
    )

    default_mem: Mapped[int] = mapped_column(  # MiB
        Integer,
        nullable=False,
    )

    timeout_seconds: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("3600"),
    )

    enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("true"),
    )

    jobs: Mapped[list["Job"]] = relationship(
        back_populates="job_type_obj",
    )

    __table_args__ = (
        CheckConstraint(
            "default_cpu > 0",
            name="job_types_default_cpu_check",
        ),
        CheckConstraint(
            "default_mem > 0",
            name="job_types_default_mem_check",
        ),
    )


# ============================================================
# Job
# ============================================================


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[UUID] = mapped_column(
        SQLUUID(as_uuid=True),
        server_default=text("gen_random_uuid()"),
        primary_key=True,
    )

    owner_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id"),
        nullable=False,
    )

    job_type: Mapped[str] = mapped_column(
        ForeignKey("job_types.name"),
        nullable=False,
    )

    params: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )

    status: Mapped[JobStatus] = mapped_column(
        job_status_enum,
        nullable=False,
        server_default=text("'PENDING'"),
    )

    task_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    replicas_per_task: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("3"),
    )

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )

    finished_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=True,
    )

    job_type_obj: Mapped["JobType"] = relationship(
        back_populates="jobs",
    )

    tasks: Mapped[list["Task"]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
    )

    events: Mapped[list["Event"]] = relationship(
        back_populates="job",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        CheckConstraint(
            "task_count > 0",
            name="jobs_task_count_check",
        ),
        CheckConstraint(
            "replicas_per_task BETWEEN 1 AND 5",
            name="jobs_replicas_per_task_check",
        ),
    )


# ============================================================
# Task
# ============================================================


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
    )

    job_id: Mapped[UUID] = mapped_column(
        ForeignKey(
            "jobs.id",
            ondelete="CASCADE",
        ),
        nullable=False,
    )

    task_index: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    input: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )

    status: Mapped[TaskStatus] = mapped_column(
        task_status_enum,
        nullable=False,
        server_default=text("'PENDING'"),
    )

    cpu_request: Mapped[int] = mapped_column(  # cores
        Integer,
        nullable=False,
    )

    mem_request: Mapped[int] = mapped_column(  # MiB
        Integer,
        nullable=False,
    )

    final_hash: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )

    job: Mapped["Job"] = relationship(
        back_populates="tasks",
    )

    replicas: Mapped[list["Replica"]] = relationship(
        back_populates="task",
        cascade="all, delete-orphan",
    )

    events: Mapped[list["Event"]] = relationship(
        back_populates="task",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        CheckConstraint(
            "task_index >= 0",
            name="tasks_task_index_check",
        ),
        CheckConstraint(
            "cpu_request > 0",
            name="tasks_cpu_request_check",
        ),
        CheckConstraint(
            "mem_request > 0",
            name="tasks_mem_request_check",
        ),
        UniqueConstraint(
            "job_id",
            "task_index",
            name="tasks_job_id_task_index_key",
        ),
        Index(
            "tasks_status_idx",
            "status",
        ),
    )


# ============================================================
# Replica
# ============================================================


class Replica(Base):
    __tablename__ = "replicas"

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
    )

    task_id: Mapped[int] = mapped_column(
        ForeignKey(
            "tasks.id",
            ondelete="CASCADE",
        ),
        nullable=False,
    )

    node_name: Mapped[str | None] = mapped_column(
        ForeignKey("nodes.name"),
        nullable=True,
    )

    status: Mapped[ReplicaStatus] = mapped_column(
        replica_status_enum,
        nullable=False,
        server_default=text("'SCHEDULED'"),
    )

    attempt: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("1"),
    )

    progress: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
    )

    result_hash: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )

    exit_code: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )

    token_hash: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )

    started_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=True,
    )

    ended_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=True,
    )

    task: Mapped["Task"] = relationship(
        back_populates="replicas",
    )

    node: Mapped["Node | None"] = relationship(
        back_populates="replicas",
    )

    __table_args__ = (
        CheckConstraint(
            "attempt >= 1",
            name="replicas_attempt_check",
        ),
        CheckConstraint(
            "progress BETWEEN 0 AND 100",
            name="replicas_progress_check",
        ),
        Index(
            "replicas_task_idx",
            "task_id",
        ),
        Index(
            "replicas_status_idx",
            "status",
        ),
        Index(
            "replicas_node_idx",
            "node_name",
            postgresql_where=text("status = 'RUNNING'"),
        ),
    )


# ============================================================
# Event
# ============================================================


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
    )

    time: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )

    type: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    job_id: Mapped[UUID | None] = mapped_column(
        ForeignKey(
            "jobs.id",
            ondelete="CASCADE",
        ),
        nullable=True,
    )

    task_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "tasks.id",
            ondelete="CASCADE",
        ),
        nullable=True,
    )

    node_name: Mapped[str | None] = mapped_column(
        ForeignKey(
            "nodes.name",
            ondelete="SET NULL",
        ),
        nullable=True,
    )

    detail: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB,
        nullable=True,
    )

    job: Mapped["Job | None"] = relationship(
        back_populates="events",
    )

    task: Mapped["Task | None"] = relationship(
        back_populates="events",
    )

    node: Mapped["Node | None"] = relationship(
        back_populates="events",
    )

    __table_args__ = (
        Index(
            "events_job_time_idx",
            "job_id",
            "time",
        ),
    )


# ============================================================
# Burst Slot
# ============================================================


class BurstSlot(Base):
    __tablename__ = "burst_slots"

    ip: Mapped[str] = mapped_column(
        INET,
        primary_key=True,
    )

    node_name: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    instance_id: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    claimed_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=True,
    )


class ReplicaView(BaseModel):
    id: int
    node_name: str | None
    status: ReplicaStatus
    attempt: int
    progress: int
    result_hash: str | None


class TaskView(BaseModel):
    id: int
    task_index: int
    status: TaskStatus
    final_hash: str | None
    replicas: list[ReplicaView]


class UserView(BaseModel):
    id: UUID
    email: str
    role: UserRole
    created_at: datetime


class NodeView(BaseModel):
    name: str
    state: NodeState
    trust_score: float
    cpu_percent: float | None
    last_heartbeat: datetime | None
    total_agree: int
    total_disagree: int


class JobSummary(BaseModel):
    id: UUID
    owner_id: UUID
    job_type: str
    status: JobStatus
    task_count: int
    created_at: datetime
    finished_at: datetime | None


class JobView(BaseModel):
    id: UUID
    job_type: str
    status: JobStatus
    progress: int = 0  # 0-100, average over tasks
    tasks: list[TaskView]
