from datetime import datetime

from sqlalchemy import DateTime, Identity, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from user.database import Base


# LE-11. login хранится в нижнем регистре (приводит приложение), поэтому обычного
# UNIQUE достаточно. Внешних ключей нет: связь 1:1 с LE-12 живёт в другой базе.
class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    login: Mapped[str] = mapped_column(Text, unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
