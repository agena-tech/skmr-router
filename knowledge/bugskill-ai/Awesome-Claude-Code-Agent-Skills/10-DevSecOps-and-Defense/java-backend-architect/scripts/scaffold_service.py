#!/usr/bin/env python3
"""
Scaffold a Spring Boot microservice with clean architecture.

Usage:
    python scaffold_service.py <service-name> --package <base-package> [--port <port>]

Examples:
    python scaffold_service.py user-service --package com.example.user
    python scaffold_service.py order-service --package com.example.order --port 8081
"""

import argparse
import os
import shutil
from pathlib import Path


SERVICE_TEMPLATES = {
    "pom.xml": """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0"
         xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
         xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 https://maven.apache.org/xsd/maven-4.0.0.xsd">
    <modelVersion>4.0.0</modelVersion>
    <parent>
        <groupId>org.springframework.boot</groupId>
        <artifactId>spring-boot-starter-parent</artifactId>
        <version>3.4.4</version>
        <relativePath/>
    </parent>
    <groupId>{package}</groupId>
    <artifactId>{service_name}</artifactId>
    <version>1.0.0-SNAPSHOT</version>
    <name>{service_name}</name>
    <properties>
        <java.version>21</java.version>
        <jjwt.version>0.12.6</jjwt.version>
    </properties>
    <dependencies>
        <dependency>
            <groupId>org.springframework.boot</groupId>
            <artifactId>spring-boot-starter-web</artifactId>
        </dependency>
        <dependency>
            <groupId>org.springframework.boot</groupId>
            <artifactId>spring-boot-starter-security</artifactId>
        </dependency>
        <dependency>
            <groupId>org.springframework.boot</groupId>
            <artifactId>spring-boot-starter-data-jpa</artifactId>
        </dependency>
        <dependency>
            <groupId>org.springframework.boot</groupId>
            <artifactId>spring-boot-starter-validation</artifactId>
        </dependency>
        <dependency>
            <groupId>org.springframework.boot</groupId>
            <artifactId>spring-boot-starter-actuator</artifactId>
        </dependency>
        <dependency>
            <groupId>org.springframework.kafka</groupId>
            <artifactId>spring-kafka</artifactId>
        </dependency>
        <dependency>
            <groupId>org.springframework.boot</groupId>
            <artifactId>spring-boot-starter-data-redis</artifactId>
        </dependency>
        <dependency>
            <groupId>org.flywaydb</groupId>
            <artifactId>flyway-core</artifactId>
        </dependency>
        <dependency>
            <groupId>org.flywaydb</groupId>
            <artifactId>flyway-mysql</artifactId>
        </dependency>
        <dependency>
            <groupId>com.mysql</groupId>
            <artifactId>mysql-connector-j</artifactId>
            <scope>runtime</scope>
        </dependency>
        <dependency>
            <groupId>io.jsonwebtoken</groupId>
            <artifactId>jjwt-api</artifactId>
            <version>${{jjwt.version}}</version>
        </dependency>
        <dependency>
            <groupId>io.jsonwebtoken</groupId>
            <artifactId>jjwt-impl</artifactId>
            <version>${{jjwt.version}}</version>
            <scope>runtime</scope>
        </dependency>
        <dependency>
            <groupId>io.jsonwebtoken</groupId>
            <artifactId>jjwt-jackson</artifactId>
            <version>${{jjwt.version}}</version>
            <scope>runtime</scope>
        </dependency>
        <dependency>
            <groupId>org.springdoc</groupId>
            <artifactId>springdoc-openapi-starter-webmvc-ui</artifactId>
            <version>2.8.6</version>
        </dependency>
        <dependency>
            <groupId>org.springframework.boot</groupId>
            <artifactId>spring-boot-starter-test</artifactId>
            <scope>test</scope>
        </dependency>
    </dependencies>
    <build>
        <plugins>
            <plugin>
                <groupId>org.springframework.boot</groupId>
                <artifactId>spring-boot-maven-plugin</artifactId>
            </plugin>
        </plugins>
    </build>
</project>
""",
    "application.yml": """server:
  port: {port}

spring:
  application:
    name: {service_name}
  datasource:
    url: ${{DB_URL:jdbc:mysql://localhost:3306/{db_name}}}
    username: ${{DB_USERNAME:root}}
    password: ${{DB_PASSWORD:password}}
    hikari:
      maximum-pool-size: 10
      minimum-idle: 5
  jpa:
    hibernate:
      ddl-auto: validate
    show-sql: false
    properties:
      hibernate:
        format_sql: true
        dialect: org.hibernate.dialect.MySQLDialect
  flyway:
    enabled: true
    locations: classpath:db/migration
  data:
    redis:
      host: ${{REDIS_HOST:localhost}}
      port: ${{REDIS_PORT:6379}}
  kafka:
    bootstrap-servers: ${{KAFKA_BOOTSTRAP_SERVERS:localhost:9092}}
    producer:
      key-serializer: org.apache.kafka.common.serialization.StringSerializer
      value-serializer: org.springframework.kafka.support.serializer.JsonSerializer
      properties:
        enable.idempotence: true
        acks: all
    consumer:
      group-id: ${{spring.application.name}}
      key-deserializer: org.apache.kafka.common.serialization.StringDeserializer
      value-deserializer: org.springframework.kafka.support.serializer.JsonDeserializer
      properties:
        spring.json.trusted.packages: '*'

app:
  jwt:
    secret: ${{JWT_SECRET:base64-encoded-256-bit-secret}}
    access-token-expiration: 900000
    refresh-token-expiration: 604800000

management:
  endpoints:
    web:
      exposure:
        include: health,info,prometheus,loggers
  endpoint:
    health:
      show-details: when-authorized

springdoc:
  api-docs:
    path: /api/v1/docs
  swagger-ui:
    path: /api/v1/swagger-ui.html
""",
    "Dockerfile": """FROM maven:3.9-eclipse-temurin-21 AS build
WORKDIR /app
COPY pom.xml .
RUN mvn dependency:go-offline -q
COPY src ./src
RUN mvn package -DskipTests

FROM eclipse-temurin:21-jre-alpine
WORKDIR /app
RUN addgroup -S app && adduser -S app -G app
USER app
COPY --from=build /app/target/*.jar app.jar
EXPOSE {port}
HEALTHCHECK --interval=30s --timeout=3s --retries=3 \\
  CMD wget -qO- http://localhost:{port}/actuator/health || exit 1
ENTRYPOINT ["java", "-jar", "app.jar"]
""",
    "docker-compose.yml": """version: '3.8'
services:
  {service_name}:
    build: .
    ports:
      - "{port}:{port}"
    environment:
      DB_URL: jdbc:mysql://mysql:3306/{db_name}
      DB_USERNAME: root
      DB_PASSWORD: password
      REDIS_HOST: redis
      KAFKA_BOOTSTRAP_SERVERS: kafka:9092
      JWT_SECRET: ${{JWT_SECRET:-base64-dev-secret-that-is-256-bits-long-for-hs256!}}
    depends_on:
      mysql:
        condition: service_healthy
      redis:
        condition: service_started
      kafka:
        condition: service_started

  mysql:
    image: mysql:8.0
    environment:
      MYSQL_ROOT_PASSWORD: password
      MYSQL_DATABASE: {db_name}
    ports:
      - "3306:3306"
    healthcheck:
      test: ["CMD", "mysqladmin", "ping", "-h", "localhost"]
      interval: 10s
      timeout: 5s
      retries: 5
    volumes:
      - mysql_data:/var/lib/mysql

  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"

  kafka:
    image: confluentinc/cp-kafka:7.8.0
    depends_on:
      - zookeeper
    environment:
      KAFKA_BROKER_ID: 1
      KAFKA_ZOOKEEPER_CONNECT: zookeeper:2181
      KAFKA_ADVERTISED_LISTENERS: PLAINTEXT://kafka:9092
      KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR: 1
    ports:
      - "9092:9092"

  zookeeper:
    image: confluentinc/cp-zookeeper:7.8.0
    environment:
      ZOOKEEPER_CLIENT_PORT: 2181
    ports:
      - "2181:2181"

volumes:
  mysql_data:
"""}


class FileGenerator:
    def __init__(self, base_dir, package, service_name, port):
        self.base_dir = Path(base_dir)
        self.package = package
        self.package_path = package.replace(".", "/")
        self.service_name = service_name
        self.port = port
        self.db_name = service_name.replace("-", "_")

    def write_file(self, path, content):
        full_path = self.base_dir / path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        formatted = content.format(
            package=self.package,
            service_name=self.service_name,
            port=self.port,
            db_name=self.db_name,
        )
        full_path.write_text(formatted)
        print(f"  Created: {full_path.relative_to(self.base_dir)}")

    def create_main_class(self):
        parts = self.service_name.replace("-", "_").split("_")
        class_name = "".join(p.capitalize() for p in parts) + "Application"
        content = f"""package {self.package};

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

@SpringBootApplication
public class {class_name} {{
    public static void main(String[] args) {{
        SpringApplication.run({class_name}.class, args);
    }}
}}
"""
        self.write_file(
            f"src/main/java/{self.package_path}/{class_name}.java", content
        )

    def create_application_files(self):
        self.write_file("pom.xml", SERVICE_TEMPLATES["pom.xml"])
        self.write_file(
            f"src/main/resources/application.yml", SERVICE_TEMPLATES["application.yml"]
        )
        self.write_file("Dockerfile", SERVICE_TEMPLATES["Dockerfile"])
        self.write_file("docker-compose.yml", SERVICE_TEMPLATES["docker-compose.yml"])

    def create_domain_layer(self):
        files = {
            "domain/entity/Sample.java": f"""package {self.package}.domain.entity;

public class Sample {{
    private Long id;
    private String name;

    public Sample() {{}}

    public Sample(Long id, String name) {{
        this.id = id;
        this.name = name;
    }}

    public Long getId() {{ return id; }}
    public String getName() {{ return name; }}
}}
""",
        }
        for path, content in files.items():
            self.write_file(f"src/main/java/{self.package_path}/{path}", content)

    def create_application_layer(self):
        files = {
            "application/port/out/SampleRepository.java": f"""package {self.package}.application.port.out;

import {self.package}.domain.entity.Sample;
import java.util.Optional;

public interface SampleRepository {{
    Sample save(Sample sample);
    Optional<Sample> findById(Long id);
}}
""",
        }
        for path, content in files.items():
            self.write_file(f"src/main/java/{self.package_path}/{path}", content)

    def create_database_migration(self):
        content = """CREATE TABLE sample (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(255) NOT NULL
);
"""
        self.write_file("src/main/resources/db/migration/V1__create_sample_table.sql", content)

    def create_gitignore(self):
        content = """target/
*.iml
.idea/
*.class
*.jar
*.log
"""
        self.write_file(".gitignore", content)

    def run(self):
        print(f"Scaffolding service: {self.service_name}")
        print(f"  Package: {self.package}")
        print(f"  Port: {self.port}")
        print()

        self.create_application_files()
        self.create_main_class()
        self.create_domain_layer()
        self.create_application_layer()
        self.create_database_migration()
        self.create_gitignore()

        print()
        print(f"Done! Service scaffolded at: {self.base_dir}")
        print(f"Next steps:")
        print(f"  cd {self.service_name}")
        print(f"  mvn spring-boot:run")


def main():
    parser = argparse.ArgumentParser(
        description="Scaffold a Spring Boot microservice with clean architecture"
    )
    parser.add_argument("service_name", help="Name of the service (e.g., user-service)")
    parser.add_argument(
        "--package",
        required=True,
        help="Base package (e.g., com.example.user)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Service port (default: 8080)",
    )
    parser.add_argument(
        "--output",
        default=".",
        help="Output directory (default: current directory)",
    )

    args = parser.parse_args()
    output_dir = Path(args.output) / args.service_name

    if output_dir.exists():
        print(f"Error: Directory already exists: {output_dir}")
        return

    generator = FileGenerator(
        base_dir=output_dir,
        package=args.package,
        service_name=args.service_name,
        port=args.port,
    )
    generator.run()


if __name__ == "__main__":
    main()
