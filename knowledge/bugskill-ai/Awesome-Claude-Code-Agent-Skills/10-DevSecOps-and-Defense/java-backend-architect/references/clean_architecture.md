# Clean Architecture Reference

## Dependency Rule

Dependencies point INWARD. Nothing in an inner circle can know about something in an outer circle.

```
Domain (innermost)  ->  Application  ->  Infrastructure & Interfaces (outermost)
```

## Layer Details

### Domain Layer (`domain/`)

Zero framework dependencies. No Spring annotations. Pure Java/Kotlin.

```
domain/
  entity/           # Core business entities (e.g., User, Order, Product)
  vo/               # Value objects (e.g., Email, Money, Address)
  event/            # Domain events (e.g., OrderPlacedEvent)
  exception/        # Domain-specific exceptions (e.g., InsufficientFundsException)
  service/          # Domain services (stateless, pure business logic)
  specification/    # Business rule specifications
```

Example entity:
```java
public class User {
    private UserId id;
    private Email email;
    private PasswordHash passwordHash;
    private Set<Role> roles;
    private boolean active;

    public void activate() {
        this.active = true;
    }
}
```

### Application Layer (`application/`)

Contains use cases (ports + DTOs). Depends on domain layer.

```
application/
  port/
    in/              # Inbound ports (use case interfaces)
      CreateUserUseCase.java
      GetUserUseCase.java
    out/             # Outbound ports (repository/messaging interfaces)
      UserRepository.java
      TokenRepository.java
      EventPublisher.java
  service/           # Use case implementations
    CreateUserService.java
    GetUserService.java
  dto/               # Application DTOs
    CreateUserRequest.java
    UserResponse.java
  mapper/            # Mapping between domain entities and DTOs
```

Example use case:
```java
public class CreateUserService implements CreateUserUseCase {
    private final UserRepository userRepository;
    private final EventPublisher eventPublisher;

    public UserResponse execute(CreateUserRequest request) {
        User user = User.create(request.email(), request.password());
        userRepository.save(user);
        eventPublisher.publish(new UserCreatedEvent(user.getId()));
        return UserResponse.from(user);
    }
}
```

### Infrastructure Layer (`infrastructure/`)

Implements outbound ports. Contains all framework-specific code.

```
infrastructure/
  persistence/
    JpaUserRepository.java     # Spring Data JPA adapter
    UserEntity.java            # JPA entity (separate from domain entity)
    UserMapper.java            # Maps between JPA entity and domain entity
  messaging/
    KafkaEventPublisher.java   # Kafka adapter
  security/
    JwtTokenProvider.java      # JWT utility
    PasswordEncoder.java       # Password hashing
  cache/
    RedisTokenRepository.java  # Redis adapter for token blacklist
```

### Interfaces Layer (`interfaces/`)

REST controllers and request/response models.

```
interfaces/
  rest/
    UserController.java        # REST endpoints
    AuthController.java        # Authentication endpoints
    GlobalExceptionHandler.java # @ControllerAdvice
  dto/
    CreateUserRequest.java     # Incoming JSON models
    UserResponse.java           # Outgoing JSON models
```

## Configuration Layer (`config/`)

Spring @Configuration classes and beans.

```
config/
  SecurityConfig.java          # Spring Security configuration
  JwtAuthFilter.java           # JWT authentication filter
  KafkaConfig.java             # Kafka producer/consumer config
  RedisConfig.java             # Redis configuration
  CacheConfig.java             # Cache manager configuration
  OpenApiConfig.java           # OpenAPI documentation config
```

## Example: Full Flow

1. **Controller** receives HTTP request
2. **Controller** calls the **inbound port** (use case interface)
3. **Use case** orchestrates business logic using **domain entities**
4. **Use case** calls **outbound ports** (interfaces)
5. **Infrastructure adapters** implement the outbound ports
6. Response flows back through the layers
