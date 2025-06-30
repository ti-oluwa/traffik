#!/bin/bash

# Traffik Docker Testing Script
# This script provides easy commands to test the traffik library using Docker

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Print colored output
print_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Help function
show_help() {
    cat <<EOF
Traffik Docker Testing Script

Usage: $0 [COMMAND]

Available commands:
  build              Build all Docker images
  test               Run full test suite
  test-fast          Run fast tests
  test-native        Run native tests
  test-matrix        Run tests across all Python versions
  test-py38          Run tests on Python 3.8
  test-py39          Run tests on Python 3.9
  test-py310         Run tests on Python 3.10
  test-py311         Run tests on Python 3.11
  test-py312         Run tests on Python 3.12
  dev                Start development environment with shell
  redis              Start Redis service only
  quality            Run code quality checks
  coverage           Run tests with coverage report
  ci                 Run CI-like suite
  clean              Clean up Docker resources
  logs               Show logs from all services
  shell              Open interactive shell in development container
  watch              Start test watch mode for development
  help               Show this help message

Examples:
  $0 test            # Run full test suite
  $0 test-fast       # Quick test
  $0 dev             # Start development environment
  $0 test-matrix     # Test across Python versions
  $0 coverage        # Generate coverage report

Environment variables:
  PYTHON_VERSION     Python version for testing (default: 3.11)
  COMPOSE_FILE       Custom compose file to use

EOF
}

# Build images
build_images() {
    print_info "Building Docker images..."
    docker compose build
    print_success "Images built successfully"
}

# Run full test suite
run_tests() {
    print_info "Starting full test suite..."
    docker compose up -f docker-compose.yml --build --abort-on-container-exit test
    print_success "Full test suite completed"
}

# Run fast tests
run_tests_fast() {
    print_info "Running fast tests..."
    docker compose up --build --abort-on-container-exit test-fast
    print_success "Fast tests completed"
}

# Run native tests
run_native_tests() {
    print_info "Running native tests..."
    docker compose up --build --abort-on-container-exit test-native
    print_success "Native tests completed"
}

# Run tests across all Python versions
run_tests_matrix() {
    print_info "Running tests across Python versions..."
    docker compose -f docker-compose.yml up --build --abort-on-container-exit \
        test-py38 test-py39 test-py310 test-py311 test-py312
    print_success "Matrix tests completed"
}

# Run Python 3.8 tests
run_py38_tests() {
    print_info "Running tests on Python 3.8..."
    docker compose -f docker-compose.yml up --build --abort-on-container-exit test-py38
    print_success "Python 3.8 tests completed"
}

run_py39_tests() {
    print_info "Running tests on Python 3.9..."
    docker compose -f docker-compose.yml up --build --abort-on-container-exit test-py39
    print_success "Python 3.9 tests completed"
}
run_py310_tests() {
    print_info "Running tests on Python 3.10..."
    docker compose -f docker-compose.yml up --build --abort-on-container-exit test-py310
    print_success "Python 3.10 tests completed"
}
run_py311_tests() {
    print_info "Running tests on Python 3.11..."
    docker compose -f docker-compose.yml up --build --abort-on-container-exit test-py311
    print_success "Python 3.11 tests completed"
}

# Run Python 3.12 tests
run_py312_tests() {
    print_info "Running tests on Python 3.12..."
    docker compose -f docker-compose.yml up --build --abort-on-container-exit test-py312
    print_success "Python 3.12 tests completed"
}

# Start development environment
start_dev() {
    print_info "Starting development environment..."
    docker compose -f docker-compose.yml up --build -d redis
    docker compose -f docker-compose.yml run --rm shell
}

# Start Redis only
start_redis() {
    print_info "Starting Redis service..."
    docker compose up -d redis
    print_info "Redis is running on localhost:6379"
}

# Run quality checks
run_quality() {
    print_info "Running code quality checks..."
    docker compose up --build --abort-on-container-exit quality
    print_success "Quality checks completed"
}

# Run coverage
run_coverage() {
    print_info "Running tests with coverage..."
    docker compose -f docker-compose.yml up --build --abort-on-container-exit coverage
    print_info "Coverage report generated in htmlcov/"
}

# Run CI suite
run_ci() {
    print_info "Running CI..."
    docker compose -f docker-compose.yml up --build --abort-on-container-exit ci
    print_success "CI suite completed successfully!"
}

# Clean up
cleanup() {
    print_info "Cleaning up Docker resources..."
    docker compose -f docker-compose.yml down -v --remove-orphans
    docker system prune -f
    print_success "Cleanup completed"
}

# Show logs
show_logs() {
    docker compose logs -f
}

# Open shell
open_shell() {
    print_info "Opening development shell..."
    docker compose -f docker-compose.yml up -d redis
    docker compose -f docker-compose.yml run --rm shell
}

# Watch mode
watch_tests() {
    print_info "Starting test watch mode..."
    docker compose -f docker-compose.yml up --build test-watch
}

# Main command handling
case "${1:-help}" in
build)
    build_images
    ;;
test)
    run_tests
    ;;
test-native)
    run_native_tests
    ;;
test-fast)
    run_tests_fast
    ;;
test-matrix)
    run_tests_matrix
    ;;
test-py38)
    run_py38_tests
    ;;
test-py39)
    run_py39_tests
    ;;
test-py310)
    run_py310_tests
    ;;
test-py311)
    run_py311_tests
    ;;
test-py312)
    run_py312_tests
    ;;
dev)
    start_dev
    ;;
redis)
    start_redis
    ;;
quality)
    run_quality
    ;;
coverage)
    run_coverage
    ;;
ci)
    run_ci
    ;;
clean)
    cleanup
    ;;
logs)
    show_logs
    ;;
shell)
    open_shell
    ;;
watch)
    watch_tests
    ;;
help | --help | -h)
    show_help
    ;;
*)
    print_error "Unknown command: $1"
    echo
    show_help
    exit 1
    ;;
esac
