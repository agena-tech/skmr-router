# Production Deployment Checklist

## Configuration & Environment

- [ ] Externalize all configuration via `application-{profile}.yml` and environment variables
- [ ] Use `@ConfigurationProperties` for type-safe configuration binding
- [ ] Never hardcode secrets, API keys, or passwords
- [ ] Validate required properties on startup using `@PostConstruct` or `@Validated`

## Security

- [ ] Enable HTTPS with TLS 1.3
- [ ] Configure CORS properly for production origins
- [ ] Use RSA key pair for JWT signing (not HMAC secrets in multi-service setups)
- [ ] Set short token expiry (access: 15 min, refresh: 7 days)
- [ ] Implement rate limiting (Resilience4j or bucket4j)
- [ ] Sanitize all user inputs
- [ ] Use parameterized queries / JPA to prevent SQL injection

## Resilience & Reliability

- [ ] Configure connection pools (HikariCP) with proper max size
- [ ] Set up circuit breakers for external service calls (Resilience4j)
- [ ] Implement retry with exponential backoff for transient failures
- [ ] Configure graceful shutdown (`server.shutdown=graceful`)
- [ ] Set configurable `spring.lifecycle.timeout-per-shutdown-phase`
- [ ] Add health indicators for all downstream dependencies

## Observability

- [ ] Expose health endpoint: `/actuator/health`
- [ ] Expose metrics: `/actuator/prometheus` with Micrometer
- [ ] Expose info endpoint: `/actuator/info` with build info
- [ ] Configure structured logging (JSON format) for log aggregation
- [ ] Add distributed tracing (Micrometer Tracing + Zipkin)
- [ ] Set up log levels management via `/actuator/loggers`

## Docker & Orchestration

- [ ] Use multi-stage Docker builds for small images
- [ ] Add HEALTHCHECK instruction in Dockerfile
- [ ] Set memory limits (`-Xmx` and `-Xms` JVM flags)
- [ ] Configure Docker CPU/memory limits in compose/K8s
- [ ] Add liveness and readiness probes
- [ ] Never run containers as root (use `USER` directive)

## Database

- [ ] Enable Flyway baseline for existing databases
- [ ] Never use `ddl-auto=update` in production
- [ ] Configure read replica for read-heavy workloads
- [ ] Set up connection timeout and max lifetime
- [ ] Enable slow query logging

## Messaging (Kafka)

- [ ] Configure idempotent producer (`enable.idempotence=true`)
- [ ] Set `acks=all` for producer
- [ ] Configure retries with `delivery.timeout.ms`
- [ ] Implement dead letter topic for failed messages
- [ ] Set appropriate `max.poll.records` for consumers
- [ ] Use schema registry in production

## Caching (Redis)

- [ ] Set TTL on all cache entries
- [ ] Configure Redis connection pool
- [ ] Use Redis Sentinel or Cluster for high availability
- [ ] Monitor cache hit/miss ratios

## CI/CD

- [ ] Run tests (unit + integration) in CI pipeline
- [ ] Run static code analysis (SonarQube / Checkstyle)
- [ ] Build and scan Docker images for vulnerabilities
- [ ] Use semantic versioning for service versions
- [ ] Automate database migrations as part of deployment
