# Unit Testing Strategies & Best Practices

A comprehensive guide to writing effective unit tests for your FastAPI application.

## Table of Contents
1. [Core Principles](#core-principles)
2. [AAA Pattern](#aaa-pattern)
3. [FIRST Principles](#first-principles)
4. [Test Organization](#test-organization)
5. [Mocking Strategies](#mocking-strategies)
6. [Testing Patterns](#testing-patterns)
7. [Edge Cases & Error Handling](#edge-cases--error-handling)
8. [Code Coverage](#code-coverage)
9. [Common Anti-Patterns](#common-anti-patterns)
10. [Advanced Techniques](#advanced-techniques)

---

## 1. Core Principles

### ✅ The Golden Rule: Test ONE Thing
Each test should verify ONE behavior or outcome.

**❌ Bad - Testing multiple things:**
```python
def test_create_group(mock_service):
    result = await handler.create_group(data)
    assert result.name == "Test"
    assert result.id is not None
    assert result.members == []
    assert result.created_at is not None
    # Too many assertions! Hard to debug when it fails
```

**✅ Good - One behavior per test:**
```python
def test_create_group_returns_correct_name(mock_service):
    result = await handler.create_group(data)
    assert result.name == "Test Group"

def test_create_group_generates_id(mock_service):
    result = await handler.create_group(data)
    assert result.id is not None

def test_create_group_initializes_empty_members(mock_service):
    result = await handler.create_group(data)
    assert result.members == []
```

### ✅ Test Behavior, Not Implementation
Focus on WHAT the code does, not HOW it does it.

**❌ Bad - Testing implementation:**
```python
def test_create_group_calls_repository_method(mock_repo):
    # Tests internal implementation details
    service.create_group(data)
    assert mock_repo.insert_into_groups_table.called  # Too specific
```

**✅ Good - Testing behavior:**
```python
def test_create_group_returns_group_with_valid_data(mock_service):
    # Tests the outcome/behavior
    result = await service.create_group(data)
    assert isinstance(result, GroupInfo)
    assert result.name == data.name
```

---

## 2. AAA Pattern (Arrange-Act-Assert)

**The most important pattern for test structure.**

### Structure Every Test Like This:

```python
@pytest.mark.asyncio
async def test_example():
    # ===== ARRANGE =====
    # Set up test data, mocks, and preconditions
    mock_service = Mock()
    mock_service.get_data.return_value = expected_result
    handler = MyHandler(service=mock_service)
    
    # ===== ACT =====
    # Execute the code being tested
    result = await handler.my_method(input_data)
    
    # ===== ASSERT =====
    # Verify the outcome
    assert result == expected_result
    mock_service.get_data.assert_called_once()
```

### Real Example from Your Code:

```python
@pytest.mark.asyncio
async def test_create_group_success(
    mock_group_service,
    sample_group_create_dto,
    sample_group_info,
    mock_credential
):
    # ===== ARRANGE =====
    # Set up: mock returns expected data
    mock_group_service.create_group.return_value = sample_group_info
    handler = GroupHandler(service=mock_group_service)

    # ===== ACT =====
    # Execute: call the handler method
    result = await handler.create_group(
        group_create=sample_group_create_dto,
        credential=mock_credential
    )

    # ===== ASSERT =====
    # Verify: check result and mock calls
    assert result == sample_group_info
    assert result.name == "Test Group"
    mock_group_service.create_group.assert_called_once()
```

**Benefits:**
- 📖 Easy to read
- 🐛 Easy to debug
- 🔧 Easy to maintain
- ✅ Clear test structure

---

## 3. FIRST Principles

Your tests should be **FIRST**:

### F - Fast ⚡
```python
# ✅ Good - Runs in milliseconds
@pytest.mark.asyncio
async def test_with_mocks():
    mock_db = AsyncMock()
    result = await service.get_data(mock_db)
    assert result is not None

# ❌ Bad - Runs in seconds
def test_with_real_database():
    db = connect_to_real_database()  # Slow!
    result = service.get_data(db)
    assert result is not None
```

**Target: Tests should complete in < 100ms each**

### I - Independent 🔀
```python
# ✅ Good - Each test is independent
def test_create_group_success():
    group = create_test_group()
    assert group.name == "Test"

def test_create_another_group():
    group = create_test_group()  # Fresh data
    assert group.id is not None

# ❌ Bad - Tests depend on each other
class TestGroups:
    group = None  # Shared state!
    
    def test_create(self):
        self.group = create_group()
    
    def test_update(self):
        update_group(self.group)  # Depends on test_create!
```

**Rule: Tests should pass in ANY order**

### R - Repeatable 🔁
```python
# ✅ Good - Always produces same result
def test_calculate_discount():
    result = calculate_discount(price=100, discount=0.1)
    assert result == 90

# ❌ Bad - Non-deterministic
def test_with_random():
    result = generate_id()  # Uses random/time
    assert result == "some_id"  # Fails randomly!
```

**Fix non-deterministic tests:**
```python
# ✅ Mock random/time
@patch('uuid.uuid4')
def test_with_mocked_uuid(mock_uuid):
    mock_uuid.return_value = UUID('12345678-1234-5678-1234-567812345678')
    result = generate_id()
    assert result == '12345678-1234-5678-1234-567812345678'
```

### S - Self-Validating ✅
```python
# ✅ Good - Clear pass/fail
def test_group_name():
    result = create_group("Test")
    assert result.name == "Test"  # Clear assertion

# ❌ Bad - Requires manual verification
def test_group_output():
    result = create_group("Test")
    print(result)  # Developer must check manually!
```

### T - Timely ⏱️
```python
# ✅ Good - Write tests WHILE coding
# Write test first (TDD):
def test_validate_email():
    assert validate_email("test@example.com") == True

# Then implement:
def validate_email(email):
    return "@" in email

# ❌ Bad - Write tests later
# Code written 3 months ago without tests
# Now you forgot what it does!
```

---

## 4. Test Organization

### Strategy 1: Group by Feature

```python
class TestGroupCreation:
    """All tests related to creating groups"""
    
    def test_create_with_valid_data(self):
        pass
    
    def test_create_with_invalid_data(self):
        pass
    
    def test_create_with_duplicate_name(self):
        pass

class TestGroupUpdate:
    """All tests related to updating groups"""
    
    def test_update_name(self):
        pass
    
    def test_update_description(self):
        pass
```

### Strategy 2: Group by Scenario

```python
class TestGroupHandler:
    """Tests for GroupHandler"""
    
    class TestSuccessScenarios:
        def test_create_group_success(self):
            pass
        
        def test_create_with_members(self):
            pass
    
    class TestErrorScenarios:
        def test_create_without_name(self):
            pass
        
        def test_create_with_duplicate(self):
            pass
```

### Strategy 3: Naming Convention

Use descriptive names that explain the test:

```python
# ✅ Good - Self-documenting
def test_create_group_with_empty_name_raises_bad_request_exception():
    pass

def test_create_group_with_duplicate_name_returns_400_error():
    pass

def test_create_group_with_valid_data_returns_group_with_id():
    pass

# ❌ Bad - Unclear
def test_create_1():
    pass

def test_create_error():
    pass

def test_group():
    pass
```

**Naming Formula:**
```
test_<method_name>_<scenario>_<expected_result>
```

---

## 5. Mocking Strategies

### When to Mock?

**✅ Always Mock:**
- Database calls
- External APIs
- File system operations
- Network requests
- Email services
- Time/dates (sometimes)
- Random number generation

**❌ Don't Mock:**
- The code you're testing
- Simple data structures
- Pure functions
- Business logic

### Strategy 1: Mock External Dependencies

```python
@pytest.mark.asyncio
async def test_create_group_calls_repository(mock_repo):
    # Mock the database repository
    service = GroupService(repo=mock_repo)
    mock_repo.transaction_wrapper = AsyncMock()
    
    await service.create_group(data)
    
    # Verify repository was called
    mock_repo.transaction_wrapper.assert_called_once()
```

### Strategy 2: Mock Return Values

```python
@pytest.mark.asyncio
async def test_get_group_returns_data(mock_service):
    # Set up what the mock should return
    expected_group = GroupInfo(id=uuid4(), name="Test")
    mock_service.get_group.return_value = expected_group
    
    # Test code that uses the mock
    result = await handler.get_group(group_id)
    
    assert result == expected_group
```

### Strategy 3: Mock Exceptions

```python
@pytest.mark.asyncio
async def test_create_group_handles_database_error(mock_repo):
    # Make the mock raise an exception
    mock_repo.create_group.side_effect = DatabaseError("Connection failed")
    
    service = GroupService(repo=mock_repo)
    
    with pytest.raises(DatabaseError):
        await service.create_group(data)
```

### Strategy 4: Verify Mock Calls

```python
@pytest.mark.asyncio
async def test_create_group_calls_service_with_correct_params(mock_service):
    handler = GroupHandler(service=mock_service)
    group_data = GroupCreateDTO(name="Test", description="Desc")
    
    await handler.create_group(group_data, credential)
    
    # Verify the mock was called with correct arguments
    mock_service.create_group.assert_called_once_with(
        group_data,
        credential=credential,
        ctx=ANY  # Use ANY for dynamic values
    )
```

### Strategy 5: Partial Mocking with Patch

```python
from unittest.mock import patch

@patch('app.services.group.Logger')
async def test_create_group_logs_success(mock_logger):
    # Mock only the logger, rest is real
    service = GroupService(repo=real_repo)
    
    await service.create_group(data)
    
    # Verify logging happened
    mock_logger.info.assert_called_with(msg="Group created successfully")
```

---

## 6. Testing Patterns

### Pattern 1: Parameterized Tests

Test multiple inputs with one test:

```python
@pytest.mark.parametrize("email,expected", [
    ("test@example.com", True),
    ("invalid", False),
    ("test@", False),
    ("@example.com", False),
    ("", False),
])
def test_validate_email(email, expected):
    result = validate_email(email)
    assert result == expected
```

### Pattern 2: Fixture-Based Tests

Reuse test data:

```python
@pytest.fixture
def valid_group_data():
    return GroupCreateDTO(
        name="Test Group",
        description="Test Description"
    )

@pytest.fixture
def invalid_group_data():
    return GroupCreateDTO(
        name=None,  # Invalid!
        description="Test"
    )

def test_with_valid_data(valid_group_data):
    result = create_group(valid_group_data)
    assert result is not None

def test_with_invalid_data(invalid_group_data):
    with pytest.raises(ValidationError):
        create_group(invalid_group_data)
```

### Pattern 3: Setup/Teardown

```python
class TestGroupService:
    def setup_method(self):
        """Run before each test"""
        self.service = GroupService(repo=mock_repo)
        self.test_data = create_test_data()
    
    def teardown_method(self):
        """Run after each test"""
        cleanup_test_data()
    
    def test_something(self):
        # self.service and self.test_data are available
        pass
```

### Pattern 4: Context Managers for Exceptions

```python
# Test that an exception is raised
def test_create_group_without_name_raises_exception():
    with pytest.raises(BadRequestException) as exc_info:
        await service.create_group(GroupCreateDTO(name=None))
    
    # Verify exception details
    assert "name is required" in str(exc_info.value.message)
    assert exc_info.value.status_code == 400
```

---

## 7. Edge Cases & Error Handling

### Always Test These Scenarios:

#### ✅ Happy Path (Success Case)
```python
def test_create_group_with_valid_data_succeeds():
    result = await service.create_group(valid_data)
    assert result.name == valid_data.name
```

#### ✅ Null/None Values
```python
def test_create_group_with_none_name_raises_error():
    data = GroupCreateDTO(name=None)
    with pytest.raises(BadRequestException):
        await service.create_group(data)
```

#### ✅ Empty Values
```python
def test_create_group_with_empty_name_raises_error():
    data = GroupCreateDTO(name="")
    with pytest.raises(BadRequestException):
        await service.create_group(data)
```

#### ✅ Boundary Values
```python
@pytest.mark.parametrize("length", [0, 1, 255, 256])
def test_group_name_length_limits(length):
    name = "a" * length
    if length > 255:
        with pytest.raises(ValidationError):
            create_group(GroupCreateDTO(name=name))
    else:
        result = create_group(GroupCreateDTO(name=name))
        assert result.name == name
```

#### ✅ Duplicates
```python
def test_create_group_with_duplicate_name_raises_error():
    mock_repo.get_group.return_value = existing_group
    
    with pytest.raises(BadRequestException) as exc:
        await service.create_group(data)
    
    assert "already exists" in str(exc.value.message)
```

#### ✅ Invalid Types
```python
def test_create_group_with_invalid_type():
    with pytest.raises(ValidationError):
        GroupCreateDTO(name=123)  # Should be string
```

#### ✅ Unexpected Errors
```python
def test_create_group_handles_database_connection_error():
    mock_repo.create_group.side_effect = ConnectionError()
    
    with pytest.raises(ConnectionError):
        await service.create_group(data)
```

---

## 8. Code Coverage

### Target Coverage Levels:

```
🎯 Critical Business Logic: 90-100%
🎯 Services/Handlers:        80-90%
🎯 Utilities/Helpers:        70-80%
🎯 Overall Project:          80%+
```

### Measure Coverage:

```bash
# Run tests with coverage
pytest --cov=app --cov-report=html --cov-report=term

# View HTML report
open htmlcov/index.html
```

### What to Focus On:

**✅ Prioritize:**
- Business logic
- Error handling
- Edge cases
- Critical paths

**⚠️ Lower Priority:**
- Getters/setters
- Simple data transformations
- Configuration files
- __init__.py files

---

## 9. Common Anti-Patterns

### ❌ Anti-Pattern 1: Testing Private Methods

```python
# ❌ Bad
def test_private_method():
    result = obj._private_method()  # Don't test private methods
    assert result == expected

# ✅ Good
def test_public_interface():
    result = obj.public_method()  # Test through public interface
    assert result == expected
```

### ❌ Anti-Pattern 2: Over-Mocking

```python
# ❌ Bad - Mocking everything
def test_calculate_total():
    mock_add = Mock(return_value=10)
    mock_multiply = Mock(return_value=20)
    # You're not testing anything real!

# ✅ Good - Mock only external dependencies
def test_calculate_total():
    # Let real math functions run
    result = calculate_total(items)
    assert result == 150
```

### ❌ Anti-Pattern 3: Fragile Tests

```python
# ❌ Bad - Breaks when internal structure changes
def test_group_structure():
    result = create_group()
    assert result.__dict__.keys() == {'id', 'name', 'created_at', ...}

# ✅ Good - Tests behavior
def test_group_has_required_fields():
    result = create_group()
    assert hasattr(result, 'id')
    assert hasattr(result, 'name')
```

### ❌ Anti-Pattern 4: Slow Tests

```python
# ❌ Bad
def test_with_sleep():
    start_background_task()
    time.sleep(5)  # Waiting for task
    assert task_completed()

# ✅ Good
@patch('time.sleep')
def test_with_mock_sleep(mock_sleep):
    start_background_task()
    assert task_completed()
```

### ❌ Anti-Pattern 5: Testing Framework Code

```python
# ❌ Bad - Testing FastAPI's validation
def test_pydantic_validates_email():
    with pytest.raises(ValidationError):
        UserInfo(email="invalid")

# ✅ Good - Test YOUR business logic
def test_create_user_with_invalid_email():
    result = await service.create_user(email="invalid")
    assert result.error == "Invalid email format"
```

---

## 10. Advanced Techniques

### Technique 1: Test Doubles

```python
# Dummy - Passed but never used
dummy_credential = Mock()

# Stub - Returns canned responses
stub_service = Mock()
stub_service.get_data.return_value = fixed_data

# Mock - Verifies interactions
mock_service = Mock()
handler.process(mock_service)
mock_service.method.assert_called_once()

# Fake - Working implementation
class FakeRepository:
    def __init__(self):
        self.data = {}
    
    async def save(self, item):
        self.data[item.id] = item
    
    async def get(self, id):
        return self.data.get(id)
```

### Technique 2: Property-Based Testing

```python
from hypothesis import given, strategies as st

@given(st.text(), st.integers())
def test_create_group_with_any_name(name, count):
    # Test with random generated data
    result = create_group(GroupCreateDTO(name=name))
    assert result.name == name
```

### Technique 3: Snapshot Testing

```python
def test_group_serialization(snapshot):
    group = create_group()
    result = serialize(group)
    
    # First run: creates snapshot
    # Future runs: compares to snapshot
    assert result == snapshot
```

### Technique 4: Testing Async Code

```python
# Use pytest-asyncio
@pytest.mark.asyncio
async def test_async_function():
    result = await async_function()
    assert result is not None

# Test concurrent operations
@pytest.mark.asyncio
async def test_concurrent_operations():
    results = await asyncio.gather(
        operation1(),
        operation2(),
        operation3()
    )
    assert len(results) == 3
```

### Technique 5: Testing with Context

```python
@pytest.mark.asyncio
async def test_with_context():
    ctx = AppContext(trace_id=uuid4(), action=CREATE_GROUP)
    
    with patch('app.common.middleware.logger.Logger') as mock_logger:
        await service.create_group(data, ctx=ctx)
        
        # Verify context was used in logging
        mock_logger.info.assert_called_with(
            msg=ANY,
            context=ctx
        )
```

---

## 📚 Quick Reference

### Test Structure Checklist:
- [ ] Uses AAA pattern (Arrange-Act-Assert)
- [ ] Tests one thing only
- [ ] Has descriptive name
- [ ] Is independent of other tests
- [ ] Runs quickly (< 100ms)
- [ ] Mocks external dependencies
- [ ] Tests behavior, not implementation

### Coverage Checklist:
- [ ] Happy path (success case)
- [ ] Error cases
- [ ] Edge cases (null, empty, boundary)
- [ ] Invalid input
- [ ] Duplicates
- [ ] Unexpected errors

### Review Checklist:
- [ ] No hard-coded values
- [ ] Clear assertions
- [ ] Good failure messages
- [ ] No unnecessary mocks
- [ ] No flaky tests
- [ ] No slow tests

---

## 🎯 Summary

**The 5 Golden Rules:**

1. **Test Behavior, Not Implementation** - Focus on what, not how
2. **One Test, One Assertion** - Keep tests focused
3. **Mock External Dependencies** - Isolate your code
4. **Use Descriptive Names** - Tests are documentation
5. **Follow FIRST Principles** - Fast, Independent, Repeatable, Self-validating, Timely

**Remember:** Good tests are an investment. They save time debugging and give confidence when refactoring.
