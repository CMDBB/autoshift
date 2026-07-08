#!/bin/bash
set -euo pipefail
IFS=$'\n\t'

(bench list-sites | grep dev.test.localhost) && bench drop-site dev.test.localhost
bench new-site --db-root-password 123 --admin-password admin dev.test.localhost
bench --site dev.test.localhost install-app autoshift
bench --site dev.test.localhost set-config allow_tests true
bench --site dev.test.localhost run-tests --app autoshift