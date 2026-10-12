class MedicalRagError(Exception):
    """Base exception for expected application-level policy failures."""


class AuthenticationRequired(MedicalRagError):
    pass


class AccessDenied(MedicalRagError):
    pass


class ConsentRequired(MedicalRagError):
    pass

