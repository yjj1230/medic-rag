class MedicalRagError(Exception):
    """应用级预期策略失败的基类异常。"""


class AuthenticationRequired(MedicalRagError):
    pass


class AccessDenied(MedicalRagError):
    pass


class ConsentRequired(MedicalRagError):
    pass

