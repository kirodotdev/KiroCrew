import pytest
from kiro_crew.security import is_sensitive_path, normalize_path_for_security

def test_normalize_path_windows_backslashes():
    raw_path = r"C:\Users\TestUser\.aws\credentials"
    normalized = normalize_path_for_security(raw_path)
    assert "\\" not in normalized
    assert "c:/users/testuser/.aws/credentials" in normalized

def test_is_sensitive_path_windows_backslashes():
    assert is_sensitive_path(r"C:\Users\Admin\.aws\credentials") is True
    assert is_sensitive_path(r"C:\Users\Admin\.ssh\id_rsa") is True
