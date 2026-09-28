from __future__ import annotations


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


class NotFoundError(ApiError):
    def __init__(self, message: str):
        super().__init__(404, "not_found", message)


class ConflictError(ApiError):
    def __init__(self, message: str):
        super().__init__(409, "conflict", message)


class ValidationError(ApiError):
    def __init__(self, message: str):
        super().__init__(422, "validation_error", message)
