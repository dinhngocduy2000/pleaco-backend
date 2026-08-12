# FastAPI Testing Guide

This guide explains how to write and run tests for the Pleaco Backend API.

## Table of Contents
1. [Setup](#setup)
2. [Testing Strategy](#testing-strategy)
3. [Writing Tests](#writing-tests)
4. [Running Tests](#running-tests)
5. [Best Practices](#best-practices)

## Setup

### Install Testing Dependencies

```bash
pip install -r requirements-dev.txt
```

### Project Structure

```
pleaco-backend/
├── app/
│   ├── handler/
│   ├── services/
│   ├── router/
│   └── ...
├── tests/
│   ├── conftest.py           # Shared fixtures
│   ├── unit/                 # Unit tests
│   │   ├── test_group_handler.py
│   │   └── test_group_service.py
│   └── integration/          # Integration tests
│       └── test_group_endpoints.py
├── pytest.ini               # Pytest configuration
└── requirements-dev.txt     # Testing dependencies
```

## Testing Strategy

### 1. Unit Tests
Test individual components in isolation using mocks.

**What to test:**
- Handler methods
- Service methods
- Business logic
- Validation logic
- Error handling

**Example:** `tests/unit/test_group_service.py`

### 2. Integration Tests
Test the complete endpoint flow using FastAPI's TestClient.

**What to test:**
- HTTP endpoints
- Request/response format
- Status codes
- Authentication
- End-to-end flows

**Example:** `tests/integration/test_group_endpoints.py`

## Writing Tests

### Unit Test Example

```python
import pytest
from unittest.mock import AsyncMock

@pytest.mark.asyncio
async def test_create_group_success(mock_group_service, sample_group_create_dto):
    # Arrange
    mock_group_service.create_group.return_value = sample_group_info
    handler = GroupHandler(service=mock_group_service)

    # Act
    result = await handler.create_group(
        group_create=sample_group_create_dto,
        credential=mock_credential
    )

    # Assert
    assert result.name == "Test Group"
    mock_group_service.create_group.assert_called_once()
```

### Integration Test Example

```python
def test_create_group_endpoint(test_client):
    # Arrange
    request_data = {"name": "Test Group", "description": "Test"}
    
    # Act
    response = test_client.post("/groups/create", json=request_data)
    
    # Assert
    assert response.status_code == 201
    assert response.json()["name"] == "Test Group"
```

### Using Fixtures

Fixtures are defined in `tests/conftest.py`:

```python
@pytest.fixture
def sample_group_create_dto():
    return GroupCreateDTO(
        name="Test Group",
        description="Test Description"
    )
```

Use them in your tests:

```python
def test_something(sample_group_create_dto):
    # The fixture is automatically injected
    assert sample_group_create_dto.name == "Test Group"
```

## Running Tests

### Run All Tests

```bash
pytest
```

### Run Specific Test File

```bash
pytest tests/unit/test_group_handler.py
```

### Run Specific Test

```bash
pytest tests/unit/test_group_handler.py::TestGroupHandler::test_create_group_success
```

### Run Tests by Marker

```bash
# Run only unit tests
pytest -m unit

# Run only integration tests
pytest -m integration
```

### Run with Coverage

```bash
pytest --cov=app --cov-report=html
```

This generates a coverage report in `htmlcov/index.html`.

### Run in Verbose Mode

```bash
pytest -v
```

### Run and Show Print Statements

```bash
pytest -s
```

## Best Practices

### 1. Follow AAA Pattern

Structure tests with Arrange-Act-Assert:

```python
def test_example():
    # Arrange - Setup test data
    data = {"name": "Test"}
    
    # Act - Execute the code being tested
    result = some_function(data)
    
    # Assert - Verify the results
    assert result == expected_value
```

### 2. Mock External Dependencies

Always mock:
- Database calls
- External API calls
- File system operations
- Authentication middleware

```python
from unittest.mock import AsyncMock, patch

@patch("app.services.group.GroupService.create_group")
async def test_with_mock(mock_create):
    mock_create.return_value = expected_result
    # ... rest of test
```

### 3. Test Edge Cases

Test:
- Success scenarios
- Error scenarios
- Validation errors
- Missing required fields
- Duplicate data
- Unauthorized access

### 4. Use Descriptive Test Names

```python
# Good
def test_create_group_with_duplicate_name_raises_exception():
    pass

# Bad
def test_create_group_2():
    pass
```

### 5. Keep Tests Independent

Each test should:
- Be able to run independently
- Not depend on other tests
- Clean up after itself

### 6. Use Async Tests Properly

For async functions:

```python
@pytest.mark.asyncio
async def test_async_function():
    result = await some_async_function()
    assert result is not None
```

### 7. Test Error Messages

```python
with pytest.raises(BadRequestException) as exc_info:
    await service.create_group(invalid_data)

assert "Group's name is required" in str(exc_info.value.message)
```

## Common Testing Patterns

### Testing Authenticated Endpoints

```python
with patch("app.common.middleware.auth_middleware.AuthMiddleware.auth_middleware") as mock_auth:
    mock_auth.return_value = mock_credential
    response = test_client.post("/protected-endpoint", json=data)
```

### Testing Database Transactions

```python
async def mock_transaction(func):
    mock_session = AsyncMock()
    return await func(mock_session)

mock_repo.transaction_wrapper = mock_transaction
```

### Testing Validation Errors

```python
def test_invalid_input():
    response = test_client.post("/create", json={"invalid": "data"})
    assert response.status_code == 422
```

## Continuous Integration

Add to your CI/CD pipeline:

```bash
# Install dependencies
pip install -r requirements.txt
pip install -r requirements-dev.txt

# Run tests
pytest --cov=app --cov-report=xml

# Check coverage threshold
pytest --cov=app --cov-fail-under=80
```

## Useful Commands Cheatsheet

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=app

# Run specific file
pytest tests/unit/test_group_handler.py

# Run specific test
pytest tests/unit/test_group_handler.py::test_create_group_success

# Run by marker
pytest -m unit

# Verbose output
pytest -v

# Show print statements
pytest -s

# Stop on first failure
pytest -x

# Run last failed tests
pytest --lf

# Create coverage report
pytest --cov=app --cov-report=html
```

## Troubleshooting

### Import Errors

If you get import errors, make sure:
1. You're in the correct directory
2. Virtual environment is activated
3. All dependencies are installed

### Async Test Issues

If async tests aren't working:
1. Check `pytest-asyncio` is installed
2. Use `@pytest.mark.asyncio` decorator
3. Check `pytest.ini` has `asyncio_mode = auto`

### Mock Not Working

If mocks aren't working:
1. Check the import path in `@patch()` is correct
2. Ensure you're patching where it's used, not where it's defined
3. Use `return_value` for regular functions, `side_effect` for exceptions

## Resources

- [Pytest Documentation](https://docs.pytest.org/)
- [FastAPI Testing](https://fastapi.tiangolo.com/tutorial/testing/)
- [pytest-asyncio](https://pytest-asyncio.readthedocs.io/)
- [unittest.mock](https://docs.python.org/3/library/unittest.mock.html)
