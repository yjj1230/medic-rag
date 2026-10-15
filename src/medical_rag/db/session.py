"""数据库引擎与按请求的会话接线。

本模块由两条规则驱动：

1. **每进程一个引擎。** ``create_engine`` 会构建连接池；按请求调用它会丢弃该连接池，
   并为每次查询新开一条连接。因此引擎在启动时（``create_app`` 的 lifespan）只创建
   一次，并通过 ``app.state`` 交给请求依赖。按请求调用 ``create_session_factory()``
   会让连接池失去意义，并把连接建立的次数乘以每个请求上解析会话的依赖数量。

2. **不静默回落到新引擎。** ``get_session`` fail closed 是正确的：用一个没人配置过的
   引擎服务请求，意味着在对一个非预期的数据库操作。测试通过用自己的会话来覆盖该依赖
   来显式选择加入。
"""
from collections.abc import Generator

from fastapi import HTTPException, Request, status
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from medical_rag.core.config import Settings

# 存放进程级会话工厂的 ``app.state`` 属性。导出它是为了
# 让组合根与请求依赖不会彼此漂移。
SESSION_FACTORY_STATE = "db_session_factory"


def create_db_engine(settings: Settings) -> Engine:
    """惰性构建引擎；创建它并不会连接数据库。"""
    return create_engine(settings.database_url, pool_pre_ping=True)


def create_session_factory(settings: Settings) -> sessionmaker[Session]:
    engine = create_db_engine(settings)
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_session(request: Request) -> Generator[Session, None, None]:
    """来自进程级工厂的按请求作用域会话。

    工厂缺失属于组合缺陷（进程在没有配置好数据库的情况下服务请求），
    因此它会 fail closed，而不是去连默认 DSN。
    """
    factory = getattr(request.app.state, SESSION_FACTORY_STATE, None)
    if factory is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="数据库会话工厂未初始化",
        )
    session: Session = factory()
    try:
        yield session
    finally:
        session.close()


__all__ = [
    "SESSION_FACTORY_STATE",
    "create_db_engine",
    "create_session_factory",
    "get_session",
]
