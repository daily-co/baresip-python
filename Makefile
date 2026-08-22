# baresip-python development targets. Everything runs inside the uv-managed venv.
#
#   native  - build the static libre/libbaresip libraries
#   ext     - compile the cffi extension in place (fast C iteration)
#   test    - run the unit test suite
#   check   - audit built artifacts' linkage (advisory here; CI enforces on release)
#   format  - apply formatting to all Python and C sources
#
# Bench (local FreeSWITCH in docker, see bench/README.md):
#   bench-up / bench-logs / bench-down

.PHONY: native ext test check format bench-up bench-logs bench-down

format:
	uv run ruff format src scripts tests
	uv run clang-format -i src/_native/*.c src/_native/*.h

native:
	uv run python scripts/build_native.py --prefix .native

ext:
	uv run python src/_native/build_ffi.py

test:
	uv run pytest tests/unit

check:
	uv run python scripts/check_linkage.py

bench-up:
	docker compose -f bench/docker-compose.yml up -d --wait

# The image runs freeswitch -nc (no console): container stdout is empty,
# the real log is the file mod_logfile writes.
bench-logs:
	docker exec baresip-bench-freeswitch tail -F /var/log/freeswitch/freeswitch.log

bench-down:
	docker compose -f bench/docker-compose.yml down
