from datetime import datetime
from enum import Enum
from uuid import UUID

from sqlalchemy import UUID as SQLUUID
from sqlalchemy import BigInteger, ForeignKey, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import TIMESTAMP
from sqlalchemy.orm import Mapped, mapped_column

from shared.db.models import Base


class UploadStatus(str, Enum):
    SPLITTING = "SPLITTING"  # waiting for pages-ready callbacks
    SUBMITTED = "SUBMITTED"  # the coordinator owns the job now
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class Upload(Base):
    __tablename__ = "uploads"

    job_id: Mapped[UUID] = mapped_column(SQLUUID(as_uuid=True), primary_key=True)
    owner_email: Mapped[str] = mapped_column(Text, nullable=False)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'SPLITTING'")
    )
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    webhook_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    webhook_sent_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    webhook_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    # The split used when the job was submitted, so old jobs keep their page ranges
    pages_per_task: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("10")
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )


class UploadBatch(Base):
    __tablename__ = "upload_batches"

    job_id: Mapped[UUID] = mapped_column(
        ForeignKey("uploads.job_id", ondelete="CASCADE"), primary_key=True
    )
    page_start: Mapped[int] = mapped_column(Integer, primary_key=True)
    page_end: Mapped[int] = mapped_column(Integer, nullable=False)


class ApiKey(Base):
    __tablename__ = "api_keys"

    key_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_email: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")
    )
