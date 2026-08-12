# Testing Examples: Good vs Bad

Side-by-side comparisons showing testing best practices and anti-patterns.

## 📚 Table of Contents
- [Test Structure](#test-structure)
- [Test Naming](#test-naming)
- [Mocking](#mocking)
- [Assertions](#assertions)
- [Independence](#independence)
- [Speed](#speed)
- [Edge Cases](#edge-cases)

---

## Test Structure

### ❌ Bad: No Clear Structure
```python
def test_create_group():
    handler = GroupHandler(Mock())
    result = handler.create_group(GroupCreateDTO(name="Test"))
    assert result
```

**Problems:**
- Hard to understand what's being tested
- Missing setup context
- Unclear what "assert result" means
- No mock configuration

### ✅ Good: AAA Pattern
```python
@pytest.mark.asyncio
async def test_create_group_with_valid_data_returns_group_info(
    mock_group_service,
    sample_group_create_dto,
    sample_group_info,
    mock_credential
):
    # ===== ARRANGE =====
    # Set up: configure mock to return expected data
    mock_group_service.create_group.return_value = sample_group_info
    handler = GroupHandler(service=mock_group_service)

    # ===== ACT =====
    # Execute: call the handler method
    result = await handler.create_group(
        group_create=sample_group_create_dto,
        credential=mock_credential
    )

    # ===== ASSERT =====
    # Verify: check result matches expectations
    assert result == sample_group_info
    assert result.name == "Test Group"
    mock_group_service.create_group.assert_called_once()
```

**Benefits:**
- Clear test structure
- Easy to understand
- Easy to debug
- Self-documenting

---

## Test Naming

### ❌ Bad: Unclear Names
```python
def test_1():
    pass

def test_create():
    pass

def test_error():
    pass

def test_group():
    pass
```

**Problems:**
- Can't tell what's being tested
- Not helpful in test reports
- Hard to find specific tests
- Doesn't explain expected behavior

### ✅ Good: Descriptive Names
```python
def test_create_group_with_valid_data_returns_group_info():
    pass

def test_create_group_with_duplicate_name_raises_bad_request_exception():
    pass

def test_create_group_with_empty_name_raises_validation_error():
    pass

def test_create_group_calls_service_with_correct_parameters():
    pass
```

**Benefits:**
- Self-documenting
- Clear intent
- Easy to find in reports
- Explains behavior

**Formula:**
```
test_<method>_<scenario>_<expected_result>
```

---

## Mocking

### ❌ Bad: Over-Mocking
```python
def test_calculate_total():
    # Mocking everything, including simple logic
    mock_add = Mock(return_value=10)
    mock_multiply = Mock(return_value=20)
    mock_subtract = Mock(return_value=5)
    
    result = calculate_total(mock_add, mock_multiply, mock_subtract)
    assert result == 5
```

**Problems:**
- Not testing real logic
- Defeats purpose of tests
- Fragile and brittle

### ✅ Good: Mock External Dependencies Only
```python
@pytest.mark.asyncio
async def test_create_group_with_repository_error_handles_gracefully(mock_repo):
    # Mock only external dependencies (database)
    mock_repo.create_group.side_effect = DatabaseError("Connection failed")
    service = GroupService(repo=mock_repo)
    
    # Real business logic runs
    with pytest.raises(DatabaseError):
        await service.create_group(data)
```

**Benefits:**
- Tests real logic
- Isolates external dependencies
- More confidence

---

### ❌ Bad: Not Verifying Mock Calls
```python
@pytest.mark.asyncio
async def test_create_group(mock_service):
    mock_service.create_group.return_value = group_info
    handler = GroupHandler(service=mock_service)
    
    result = await handler.create_group(data)
    
    # Missing verification that service was called!
    assert result is not None
```

**Problems:**
- Doesn't verify integration
- Mock might not be called
- False positive

### ✅ Good: Verify Mock Interactions
```python
@pytest.mark.asyncio
async def test_create_group_calls_service_correctly(
    mock_service,
    sample_data,
    mock_credential
):
    mock_service.create_group.return_value = group_info
    handler = GroupHandler(service=mock_service)
    
    result = await handler.create_group(sample_data, mock_credential)
    
    # Verify the service was called
    mock_service.create_group.assert_called_once()
    
    # Verify it was called with correct arguments
    call_args = mock_service.create_group.call_args
    assert call_args.args[0] == sample_data
    assert call_args.kwargs['credential'] == mock_credential
```

**Benefits:**
- Verifies integration
- Catches wrong parameters
- More thorough

---

## Assertions

### ❌ Bad: Multiple Unrelated Assertions
```python
def test_create_group():
    result = create_group("Test")
    
    # Testing too many things
    assert result.id is not None
    assert result.name == "Test"
    assert result.description is None
    assert result.created_at < datetime.now()
    assert len(result.members) == 0
    assert result.owner_id is not None
```

**Problems:**
- Hard to debug failures
- Tests multiple behaviors
- Not clear what failed

### ✅ Good: Focused Assertions
```python
def test_create_group_generates_id():
    result = create_group("Test")
    assert result.id is not None

def test_create_group_sets_name_correctly():
    result = create_group("Test")
    assert result.name == "Test"

def test_create_group_initializes_empty_members():
    result = create_group("Test")
    assert len(result.members) == 0
```

**Benefits:**
- Clear failure messages
- One behavior per test
- Easy to debug

---

### ❌ Bad: Weak Assertions
```python
def test_create_group():
    result = create_group("Test")
    
    # Too generic
    assert result
    assert result.name
```

**Problems:**
- Doesn't verify actual values
- Can pass with wrong data
- Not specific enough

### ✅ Good: Specific Assertions
```python
def test_create_group_returns_correct_name():
    result = create_group("Test Group")
    
    # Verify exact value
    assert result.name == "Test Group"

def test_create_group_generates_uuid():
    result = create_group("Test")
    
    # Verify type and format
    assert isinstance(result.id, UUID)
    assert str(result.id).count('-') == 4  # Valid UUID format
```

**Benefits:**
- Catches wrong values
- More specific
- Better documentation

---

## Independence

### ❌ Bad: Tests Depend on Each Other
```python
class TestGroupLifecycle:
    created_group = None  # Shared state!
    
    def test_1_create_group(self):
        self.created_group = create_group("Test")
        assert self.created_group.id is not None
    
    def test_2_update_group(self):
        # Depends on test_1 running first!
        update_group(self.created_group, name="Updated")
        assert self.created_group.name == "Updated"
    
    def test_3_delete_group(self):
        # Depends on test_1 and test_2!
        delete_group(self.created_group)
```

**Problems:**
- Tests must run in order
- One failure breaks all
- Hard to debug
- Can't run tests in parallel

### ✅ Good: Independent Tests
```python
class TestGroupOperations:
    
    def test_create_group_succeeds(self):
        # Fresh setup
        group = create_group("Test")
        assert group.id is not None
    
    def test_update_group_changes_name(self):
        # Fresh setup (independent)
        group = create_group("Original")
        
        update_group(group, name="Updated")
        
        assert group.name == "Updated"
    
    def test_delete_group_removes_from_database(self):
        # Fresh setup (independent)
        group = create_group("ToDelete")
        
        delete_group(group)
        
        assert get_group(group.id) is None
```

**Benefits:**
- Tests run in any order
- Can run in parallel
- Easy to debug
- Isolated failures

---

## Speed

### ❌ Bad: Slow Tests
```python
def test_send_email():
    # Actually sends email!
    send_email("test@example.com", "Subject", "Body")
    time.sleep(5)  # Wait for email
    assert email_was_sent()

def test_process_large_file():
    # Processes real 100MB file
    result = process_file("large_file.dat")
    assert result is not None

def test_api_integration():
    # Makes real HTTP call
    response = requests.get("https://api.example.com/data")
    assert response.status_code == 200
```

**Problems:**
- Takes seconds/minutes
- Depends on external services
- Flaky (network issues)
- Expensive

**Test run time: 15+ seconds per test** ⏱️

### ✅ Good: Fast Tests with Mocks
```python
@patch('app.services.email.send_email')
def test_send_email_calls_service(mock_send):
    # Mocked - instant
    send_email("test@example.com", "Subject", "Body")
    
    mock_send.assert_called_once()

@pytest.mark.asyncio
async def test_process_file_handles_large_files(mock_file_system):
    # Mocked file system - instant
    mock_file_system.read.return_value = b"test data"
    
    result = await process_file("large_file.dat")
    
    assert result is not None

@patch('requests.get')
def test_api_integration_handles_response(mock_get):
    # Mocked HTTP call - instant
    mock_get.return_value = Mock(status_code=200, json=lambda: {"data": "test"})
    
    result = fetch_data()
    
    assert result["data"] == "test"
```

**Benefits:**
- Milliseconds per test ⚡
- No external dependencies
- Reliable
- Free

**Test run time: < 10ms per test** 🚀

---

## Edge Cases

### ❌ Bad: Only Testing Happy Path
```python
def test_create_group():
    # Only tests success case
    result = create_group("Test Group")
    assert result.name == "Test Group"
```

**Problems:**
- Misses error cases
- Incomplete coverage
- False confidence

### ✅ Good: Comprehensive Test Coverage
```python
# Happy path
def test_create_group_with_valid_data_succeeds():
    result = create_group("Test Group")
    assert result.name == "Test Group"

# Null cases
def test_create_group_with_none_name_raises_error():
    with pytest.raises(ValidationError):
        create_group(None)

# Empty cases
def test_create_group_with_empty_name_raises_error():
    with pytest.raises(ValidationError):
        create_group("")

# Boundary cases
@pytest.mark.parametrize("length", [0, 1, 255, 256])
def test_create_group_respects_name_length_limit(length):
    name = "a" * length
    if length > 255:
        with pytest.raises(ValidationError):
            create_group(name)
    else:
        result = create_group(name)
        assert len(result.name) == length

# Duplicate cases
def test_create_group_with_duplicate_name_raises_error():
    create_group("Duplicate")
    
    with pytest.raises(BadRequestException) as exc:
        create_group("Duplicate")
    
    assert "already exists" in str(exc.value.message)

# Invalid type cases
def test_create_group_with_invalid_type_raises_error():
    with pytest.raises(TypeError):
        create_group(123)  # Should be string

# Special characters
@pytest.mark.parametrize("name", [
    "Group with spaces",
    "Group-with-dashes",
    "Group_with_underscores",
    "Group.with.dots",
    "Grüppe",  # Unicode
    "组群",  # Chinese characters
])
def test_create_group_handles_various_name_formats(name):
    result = create_group(name)
    assert result.name == name
```

**Benefits:**
- Comprehensive coverage
- Catches edge cases
- Production-ready
- Real confidence

---

## Parametrized Tests

### ❌ Bad: Repetitive Tests
```python
def test_validate_email_valid():
    assert validate_email("test@example.com") == True

def test_validate_email_invalid_no_at():
    assert validate_email("invalid") == False

def test_validate_email_invalid_no_domain():
    assert validate_email("test@") == False

def test_validate_email_invalid_empty():
    assert validate_email("") == False

# ... 20 more similar tests
```

**Problems:**
- Lots of duplication
- Hard to maintain
- Verbose

### ✅ Good: Parametrized Tests
```python
@pytest.mark.parametrize("email,expected", [
    ("test@example.com", True),
    ("user@domain.co.uk", True),
    ("user+tag@example.com", True),
    ("invalid", False),
    ("test@", False),
    ("@example.com", False),
    ("", False),
    (None, False),
    ("test @example.com", False),
    ("test@example", False),
])
def test_validate_email(email, expected):
    result = validate_email(email)
    assert result == expected
```

**Benefits:**
- Concise
- Easy to add cases
- Single source of truth
- Maintainable

---

## Async Testing

### ❌ Bad: Not Awaiting Async Functions
```python
def test_create_group():
    # Missing await!
    result = handler.create_group(data)
    assert result  # This is a coroutine, not the result!
```

**Problems:**
- Test doesn't actually run
- False positive
- Coroutine never executes

### ✅ Good: Proper Async Testing
```python
@pytest.mark.asyncio
async def test_create_group_async():
    # Properly await the coroutine
    result = await handler.create_group(data)
    assert result.name == "Test Group"
```

**Benefits:**
- Actually runs the code
- Tests real async behavior
- Catches async bugs

---

## Error Testing

### ❌ Bad: Not Testing Error Messages
```python
def test_create_group_with_duplicate_raises_error():
    with pytest.raises(BadRequestException):
        create_group("Duplicate")
```

**Problems:**
- Doesn't verify error message
- Could raise exception for wrong reason
- Incomplete test

### ✅ Good: Verify Error Details
```python
def test_create_group_with_duplicate_name_raises_specific_error():
    # Create first group
    create_group("Duplicate")
    
    # Attempt to create duplicate
    with pytest.raises(BadRequestException) as exc_info:
        create_group("Duplicate")
    
    # Verify error details
    assert "already exists" in str(exc_info.value.message)
    assert exc_info.value.status_code == 400
    assert "Duplicate" in str(exc_info.value.message)
```

**Benefits:**
- Verifies exact error
- Catches wrong errors
- Better documentation

---

## Summary Checklist

When writing tests, ask yourself:

- [ ] Does it follow AAA pattern?
- [ ] Is the test name descriptive?
- [ ] Is it testing ONE thing?
- [ ] Are external dependencies mocked?
- [ ] Does it run in < 100ms?
- [ ] Is it independent of other tests?
- [ ] Are assertions specific?
- [ ] Does it test edge cases?
- [ ] Are error cases covered?
- [ ] Is it easy to understand?

**Remember: Good tests = Confident code!** 🚀
