"""Base exception classes for CLI tools."""


class ClientError(Exception):
    """Base exception for API/service client errors."""
    pass


class ConfigError(Exception):
    """Exception for configuration and profile errors."""
    pass
