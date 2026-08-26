#!/bin/bash
set -euo pipefail
IFS=$'\n\t'

# Color codes
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# Defaults
RESET=false
INTERACTIVE=false
RUN_BUILD=false
RUN_INTEGRATION_TESTS=true
RUN_UNIT_TESTS=true
RUN_LINT=true
VERBOSE=false
SITE_NAME="dev.test.localhost"

# Check for pytest recovery mode (if no args and previous failure exists)
RECOVERY_MODE=false
LASTFAILED_PATH=".pytest_cache/v/cache/lastfailed"
if [ $# -eq 0 ] && [ -f $LASTFAILED_PATH ] && [ "$(cat $LASTFAILED_PATH)" != "{}" ]; then
    RECOVERY_MODE=true
    RUN_INTEGRATION_TESTS=false
    RUN_LINT=false
fi

show_usage() {
    cat << EOF
Usage: $(basename "$0") [OPTIONS]

Options:
  -r, --reset               Reset the test site (drop and recreate)
  --site SITENAME           Target site (default: dev.test.localhost)
                            Non-default sites will be backed up before reset
  -i, --interactive         Interactive mode (prompt for each task)
  --build                   Build assets (default: false)
  --no-build                Skip building assets
  --integration-tests       Run integration tests (default: true)
  --no-integration-tests    Skip integration tests
  --unit-tests              Run unit tests (default: true, stepwise)
  --no-unit-tests           Skip unit tests
  --lint                    Run lint and semgrep (default: true)
  --no-lint                 Skip lint and semgrep
  -v, --verbose             Show all command output
  -h, --help                Show this message

Examples:
  # Reset default test site and run all tests
  $(basename "$0") --reset

  # Reset a custom site with backup
  $(basename "$0") --reset --site my-dev-site

  # Run only unit tests (stepwise)
  $(basename "$0") --no-build --no-integration-tests --no-lint

  # Interactive selection
  $(basename "$0") --interactive

Recovery mode:
  If unit tests fail, running the script with no arguments will default to
  running only unit tests in stepwise mode (continuing from the last failure).
  Pass any explicit argument to override this behavior.
EOF
}

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        -r|--reset)
            RESET=true
            shift
            ;;
        --site)
            if [[ $# -lt 2 ]]; then
                echo -e "${RED}Error: --site requires a site name${NC}" >&2
                show_usage >&2
                exit 1
            fi
            SITE_NAME="$2"
            shift 2
            ;;
        -i|--interactive)
            INTERACTIVE=true
            shift
            ;;
        --build)
            RUN_BUILD=!$RESET
            shift
            ;;
        --no-build)
            RUN_BUILD=false
            shift
            ;;
        --integration-tests)
            RUN_INTEGRATION_TESTS=true
            shift
            ;;
        --no-integration-tests)
            RUN_INTEGRATION_TESTS=false
            shift
            ;;
        --unit-tests)
            RUN_UNIT_TESTS=true
            shift
            ;;
        --no-unit-tests)
            RUN_UNIT_TESTS=false
            shift
            ;;
        --lint)
            RUN_LINT=true
            shift
            ;;
        --no-lint)
            RUN_LINT=false
            shift
            ;;
        -v|--verbose)
            VERBOSE=true
            shift
            ;;
        -h|--help)
            show_usage
            exit 0
            ;;
        *)
            echo -e "${RED}Error: Unknown option '$1'${NC}" >&2
            show_usage >&2
            exit 1
            ;;
    esac
done

# Interactive mode
if [ "$INTERACTIVE" = true ]; then
    echo -e "${YELLOW}Interactive mode${NC}\n"

    read -p "Reset test site? (y/n) [n]: " -r RESET_CHOICE
    [[ $RESET_CHOICE =~ ^[Yy]$ ]] && RESET=true

    if [ "$RESET_CHOICE" != true ]; then
        # if reset is true, build happens anyway
        read -p "Build assets? (y/n) [y]: " -r BUILD_CHOICE
        [[ $BUILD_CHOICE =~ ^[Nn]$ ]] && RUN_BUILD=false
    fi

    read -p "Run integration tests? (y/n) [y]: " -r INTEGRATION_CHOICE
    [[ $INTEGRATION_CHOICE =~ ^[Nn]$ ]] && RUN_INTEGRATION_TESTS=false

    read -p "Run unit tests? (y/n) [y]: " -r UNIT_CHOICE
    [[ $UNIT_CHOICE =~ ^[Nn]$ ]] && RUN_UNIT_TESTS=false

    read -p "Run lint and semgrep? (y/n) [y]: " -r LINT_CHOICE
    [[ $LINT_CHOICE =~ ^[Nn]$ ]] && RUN_LINT=false

    echo
fi

# Set verbose mode
if [ "$VERBOSE" = true ]; then
    set -x
fi

# RESET TEST SITE
if [ "$RESET" = true ]; then
    echo -e "${YELLOW}→ Resetting test site ($SITE_NAME)...${NC}"
    bench setup requirements --dev

    # Create backup for non-default sites
    if [ "$SITE_NAME" != "dev.test.localhost" ]; then
        backup_file="$SITE_NAME-backup-$(date +%s).sql.gz"
        echo -e "${YELLOW}→ Creating compressed backup...${NC}"
        bench --site "$SITE_NAME" backup --with-files --backup-path /tmp

        if [ -f "/tmp/$SITE_NAME-*" ]; then
            latest_backup=$(ls -t /tmp/$SITE_NAME-* 2>/dev/null | head -n1)
            if [ -n "$latest_backup" ]; then
                gzip -f "$latest_backup" 2>/dev/null
                backup_file="${latest_backup}.gz"
            fi
        fi

        if [ -f "$backup_file" ]; then
            backup_size=$(du -h "$backup_file" | cut -f1)
            echo -e "${GREEN}✓ Backup created${NC}: $backup_file (${backup_size})"
            echo -e "${YELLOW}⚠ About to reset site '$SITE_NAME'${NC}"
            read -p "Continue with reset? (y/N) " -n 1 -r
            echo
            if [[ ! $REPLY =~ ^[Yy]$ ]]; then
                echo -e "${YELLOW}Reset cancelled${NC}"
                exit 0
            fi
        fi
    fi

    bench drop-site "$SITE_NAME" --no-backup --db-root-username root --db-root-password 123 --force || echo "Error occurred during drop site"
    bench new-site --db-root-username root --db-root-password 123 --admin-password admin "$SITE_NAME"
    bench --site "$SITE_NAME" install-app autoshift
    bench --site "$SITE_NAME" set-config allow_tests true
    bench build
    echo -e "${GREEN}✓ Test site reset${NC}\n"
fi

# BUILD
if [ "$RUN_BUILD" = true ]; then
    echo -e "${YELLOW}→ Building autoshift assets...${NC}"
    bench build --app autoshift
    echo -e "${GREEN}✓ Build complete${NC}\n"
fi

# Helper to start concurrent tasks
start_concurrent_task() {
    local name="$1"
    local temp_file
    temp_file=$(mktemp)
    shift

    "$@" > "$temp_file" 2>&1 &

    pids+=($!)
    names+=("$name")
    temp_files+=("$temp_file")
}

# Start concurrent tasks
declare -a pids=()
declare -a names=()
declare -a temp_files=()

if [ "$RUN_INTEGRATION_TESTS" = true ]; then
    start_concurrent_task "Integration Tests" \
        bench --site "$SITE_NAME" run-tests --app autoshift
fi

if [ "$RUN_UNIT_TESTS" = true ]; then
    if [ "$RECOVERY_MODE" = true ]; then
        start_concurrent_task "Unit Tests (recovery)" \
            uv run pytest tests/ --sw
    else
        start_concurrent_task "Unit Tests" \
            uv run pytest tests/ --sw
    fi
fi

if [ "$RUN_LINT" = true ]; then
    start_concurrent_task "Lint" \
        bash -c 'pre-commit run -a && pre-commit run -a'

    start_concurrent_task "Semgrep" \
        uv run semgrep scan \
        --config ./frappe-semgrep-rules/rules \
        --config r/python.lang.correctness \
        -q --error
fi

# Wait for all concurrent tasks and show progress
total=${#pids[@]}
if [ $total -gt 0 ]; then
    spinner='===ea@@@//(((|||]]]\\\\\\\\\\N~See='
    spinner_len=${#spinner}
    spinner_idx=0

    completed=0
    declare -a finished
    while [ $completed -lt $total ]; do
        for i in "${!pids[@]}"; do
            if ! [ -z "${finished[$i]-}" ] || kill -0 "${pids[$i]}" 2>/dev/null; then
                continue
            fi

            exit_code=$( set +e; wait "${pids[$i]}"; echo $? )
            finished[$i]=1
            !((completed++))

            echo -ne "\033[1K\r"
            echo -e "${YELLOW}=== ${names[$i]} ===${NC}"
            if [[ $exit_code -eq 0 ]]; then
                echo ...
                tail -n 3 "${temp_files[$i]}"
            else
                cat "${temp_files[$i]}"
            fi
            echo

            status=$([[ $exit_code -eq 0 ]] && echo "✓" || echo "✗")
            printf "\r${GREEN}[%d/%d]${NC} %s %s" "$completed" "$total" "$status" "${names[$i]}"
            echo
        done

        if [ $completed -lt $total ]; then
            printf "\r${YELLOW}%s Running %d tasks...${NC}" "${spinner:((spinner_idx % spinner_len)):1}" "$((total - completed))"
            !((spinner_idx++))
        fi
        sleep 0.05
    done

    # Cleanup temp files
    for temp_file in "${temp_files[@]}"; do
        rm -f "$temp_file"
    done
fi

echo -e "${GREEN}All done!${NC}"
