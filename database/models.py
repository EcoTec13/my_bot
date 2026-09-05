"""
Модели БД: User, Service, Master, Booking, MasterSchedule.
"""

from datetime import datetime, date
from typing import Optional

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.db import Base

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    max_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(255), default="")
    phone: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    bookings: Mapped[list["Booking"]] = relationship(
        back_populates="user", lazy="selectin"
    )

class Service(Base):
    __tablename__ = "services"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    price: Mapped[str] = mapped_column(String(50))
    duration_min: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    bookings: Mapped[list["Booking"]] = relationship(
        back_populates="service", lazy="selectin"
    )

class Master(Base):
    __tablename__ = "masters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    specialization: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    bookings: Mapped[list["Booking"]] = relationship(
        back_populates="master", lazy="selectin"
    )
    schedules: Mapped[list["MasterSchedule"]] = relationship(
        back_populates="master"
    )


class MasterSchedule(Base):
    """График работы мастера: работал ли он в конкретный день (X в Excel)."""

    __tablename__ = "master_schedules"
    __table_args__ = (
        Index("ix_master_schedules_master_date", "master_id", "work_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    master_id: Mapped[int] = mapped_column(ForeignKey("masters.id"))
    work_date: Mapped[date] = mapped_column(Date)
    is_working: Mapped[bool] = mapped_column(Boolean, default=False)

    master: Mapped["Master"] = relationship(back_populates="schedules")

class Booking(Base):
    __tablename__ = "bookings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id"))
    master_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("masters.id"), nullable=True
    )
    desired_date: Mapped[str] = mapped_column(String(10))  # YYYY-MM-DD
    desired_time: Mapped[str] = mapped_column(String(5))   # ЧЧ:ММ
    status: Mapped[str] = mapped_column(String(20), default="new")
    reminder_sent: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    user: Mapped["User"] = relationship(back_populates="bookings")
    service: Mapped["Service"] = relationship(back_populates="bookings")
    master: Mapped[Optional["Master"]] = relationship(back_populates="bookings")
